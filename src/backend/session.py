from __future__ import annotations

"""Session storage for scores, history, and audio outputs."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import json
import shutil
import uuid

from firebase_admin import firestore

from src.backend.firebase_app import get_firestore_client
from src.backend.storage_client import download_bytes, upload_bytes

def _utcnow() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


@dataclass
class SessionState:
    """In-memory representation of a user session."""
    id: str
    user_id: Optional[str]
    created_at: datetime
    last_active_at: datetime
    history: List[Dict[str, str]] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)
    original_score: Optional[Dict[str, Any]] = None
    original_score_path: Optional[str] = None
    preprocess_plan_history: List[Dict[str, Any]] = field(default_factory=list)
    preprocess_attempt_history: List[Dict[str, Any]] = field(default_factory=list)
    last_preprocess_plan: Optional[Dict[str, Any]] = None
    current_score: Optional[Dict[str, Any]] = None
    current_score_path: Optional[str] = None
    current_score_version: int = 0
    score_summary: Optional[Dict[str, Any]] = None
    current_audio: Optional[Dict[str, Any]] = None

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of session state."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
            "history": list(self.history),
            "files": dict(self.files),
            "original_score": dict(self.original_score) if self.original_score else None,
            "preprocess_plan_history": list(self.preprocess_plan_history),
            "preprocess_attempt_history": list(self.preprocess_attempt_history),
            "last_preprocess_plan": (
                dict(self.last_preprocess_plan)
                if self.last_preprocess_plan
                else None
            ),
            "current_score": self._score_snapshot(),
            "score_summary": dict(self.score_summary) if self.score_summary else None,
            "current_audio": dict(self.current_audio) if self.current_audio else None,
        }

    def _score_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return score payload with version metadata."""
        if self.current_score is None:
            return None
        return {"score": self.current_score, "version": self.current_score_version}


class SessionStore:
    """Filesystem-backed in-memory session store."""
    def __init__(
        self,
        project_root: Path,
        sessions_dir: Path,
        ttl_seconds: int,
        max_sessions: int,
        *,
        backend_use_storage: bool = False,
        storage_bucket: str = "",
    ) -> None:
        """Initialize the store with TTL and storage paths."""
        self._project_root = project_root
        self._sessions_dir = sessions_dir
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
        self._backend_use_storage = backend_use_storage
        self._storage_bucket = storage_bucket
        self._sessions: Dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    def session_dir(self, session_id: str) -> Path:
        """Return the session directory for a session ID."""
        return (self._sessions_dir / session_id).resolve()

    def progress_path(self, session_id: str) -> Path:
        """Return the progress.json path for a session."""
        return self.session_dir(session_id) / "progress.json"

    def _relative_path(self, path: Path) -> str:
        """Return a project-relative path string."""
        return str(path.relative_to(self._project_root))

    async def create_session(self, user_id: Optional[str]) -> SessionState:
        """Create and persist a new session record."""
        async with self._lock:
            self._evict_expired_locked()
            self._evict_overflow_locked()
            session_id = uuid.uuid4().hex
            now = _utcnow()
            state = SessionState(
                id=session_id,
                user_id=user_id,
                created_at=now,
                last_active_at=now,
            )
            self._sessions[session_id] = state
            session_dir = self.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            return state

    async def get_session(self, session_id: str, user_id: Optional[str]) -> SessionState:
        """Fetch a session, enforcing ownership and TTL."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            if user_id and state.user_id and state.user_id != user_id:
                raise PermissionError(session_id)
            if self._is_expired(state):
                self._remove_session_locked(session_id)
                raise KeyError(session_id)
            state.last_active_at = _utcnow()
            return state

    async def get_snapshot(self, session_id: str, user_id: Optional[str]) -> Dict[str, Any]:
        """Return a snapshot of a session for API responses."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            if user_id and state.user_id and state.user_id != user_id:
                raise PermissionError(session_id)
            if self._is_expired(state):
                self._remove_session_locked(session_id)
                raise KeyError(session_id)
            if self._backend_use_storage:
                self._hydrate_scores_locked(state)
            state.last_active_at = _utcnow()
            return state.snapshot()

    async def append_history(self, session_id: str, role: str, content: str) -> None:
        """Append a chat message to the session history."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.history.append({"role": role, "content": content})
            state.last_active_at = _utcnow()

    async def set_file(self, session_id: str, key: str, path: Path) -> None:
        """Associate a file path with the session."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.files[key] = self._relative_path(path)
            state.last_active_at = _utcnow()

    async def set_metadata(self, session_id: str, key: str, value: str) -> None:
        """Store arbitrary metadata in the session file map."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.files[key] = value
            state.last_active_at = _utcnow()

    async def set_score(self, session_id: str, score: Dict[str, Any]) -> int:
        """Update the current score and increment its version."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.current_score_version += 1
            if self._backend_use_storage:
                storage_path = _current_score_storage_path(
                    state.user_id,
                    session_id,
                    state.current_score_version,
                )
                _store_score_to_storage(self._storage_bucket, storage_path, score)
                state.current_score_path = storage_path
            state.current_score = score
            state.last_active_at = _utcnow()
            return state.current_score_version

    async def set_original_score(self, session_id: str, score: Dict[str, Any]) -> None:
        """Persist the original parsed score baseline for future replanning."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            if self._backend_use_storage:
                storage_path = _original_score_storage_path(state.user_id, session_id)
                _store_score_to_storage(self._storage_bucket, storage_path, score)
                state.original_score_path = storage_path
            state.original_score = score
            state.last_active_at = _utcnow()

    async def set_score_summary(self, session_id: str, summary: Optional[Dict[str, Any]]) -> None:
        """Attach a score summary to the session."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.score_summary = summary
            state.last_active_at = _utcnow()

    async def append_preprocess_plan(self, session_id: str, entry: Dict[str, Any]) -> None:
        """Append a generated preprocess plan entry for debugging."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.preprocess_plan_history.append(entry)
            state.last_active_at = _utcnow()

    async def append_preprocess_attempt_summary(
        self, session_id: str, entry: Dict[str, Any]
    ) -> None:
        """Append a lightweight preprocess attempt summary for debugging."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.preprocess_attempt_history.append(entry)
            state.last_active_at = _utcnow()

    async def set_last_preprocess_plan(
        self, session_id: str, plan: Optional[Dict[str, Any]]
    ) -> None:
        """Persist the latest preprocess plan for prompt context."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.last_preprocess_plan = plan
            state.last_active_at = _utcnow()

    async def set_audio(
        self,
        session_id: str,
        path: Path,
        duration_s: float,
        storage_path: Optional[str] = None,
    ) -> None:
        """Store audio output metadata for the session."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.current_audio = {
                "path": self._relative_path(path),
                "duration_s": duration_s,
            }
            if storage_path:
                state.current_audio["storage_path"] = storage_path
            state.last_active_at = _utcnow()

    async def reset_for_new_upload(self, session_id: str) -> None:
        """Clear score-specific session state and remove prior derived artifacts."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.history = []
            state.files = {}
            state.original_score = None
            state.original_score_path = None
            state.preprocess_plan_history = []
            state.preprocess_attempt_history = []
            state.last_preprocess_plan = None
            state.current_score = None
            state.current_score_path = None
            state.current_score_version = 0
            state.score_summary = None
            state.current_audio = None
            state.last_active_at = _utcnow()
            session_dir = self.session_dir(session_id)
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            session_dir.mkdir(parents=True, exist_ok=True)

    async def evict_expired(self) -> None:
        """Evict any sessions that have expired in memory."""
        async with self._lock:
            self._evict_expired_locked()

    async def cleanup_expired_on_disk(self) -> int:
        """Remove expired session folders from disk."""
        removed = 0
        ttl_seconds = int(self._ttl.total_seconds())
        now_ts = _utcnow().timestamp()
        if not self._sessions_dir.exists():
            return removed
        for entry in self._sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            latest_mtime = self._latest_mtime(entry)
            if latest_mtime is None:
                continue
            age_seconds = now_ts - latest_mtime
            if age_seconds <= ttl_seconds:
                continue
            async with self._lock:
                self._remove_session_locked(entry.name)
            removed += 1
        return removed

    def _is_expired(self, state: SessionState) -> bool:
        """Return True if a session is past its TTL."""
        return _utcnow() - state.last_active_at > self._ttl

    def _evict_expired_locked(self) -> None:
        """Evict expired sessions (caller must hold lock)."""
        expired = [sid for sid, s in self._sessions.items() if self._is_expired(s)]
        for sid in expired:
            self._remove_session_locked(sid)

    def _evict_overflow_locked(self) -> None:
        """Evict oldest sessions until under max_sessions."""
        if self._max_sessions <= 0:
            return
        if len(self._sessions) <= self._max_sessions:
            return
        ordered = sorted(self._sessions.values(), key=lambda s: s.last_active_at)
        while len(self._sessions) > self._max_sessions and ordered:
            self._remove_session_locked(ordered.pop(0).id)

    def _remove_session_locked(self, session_id: str) -> None:
        """Remove session data and delete its directory (caller holds lock)."""
        self._sessions.pop(session_id, None)
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def _latest_mtime(self, session_dir: Path) -> Optional[float]:
        """Return the most recent mtime under a session directory."""
        try:
            latest = session_dir.stat().st_mtime
        except FileNotFoundError:
            return None
        for path in session_dir.rglob("*"):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime > latest:
                latest = mtime
        return latest

    def _hydrate_scores_locked(self, state: SessionState) -> None:
        """Hydrate score payloads from object storage when pointer paths exist."""
        if state.original_score_path and state.original_score is None:
            state.original_score = _load_score_from_storage(
                self._storage_bucket, state.original_score_path
            )
        if state.current_score_path and state.current_score is None:
            state.current_score = _load_score_from_storage(
                self._storage_bucket, state.current_score_path
            )


class FirestoreSessionStore:
    """Firestore-backed session store."""
    def __init__(
        self,
        project_root: Path,
        sessions_dir: Path,
        ttl_seconds: int,
        max_sessions: int,
        *,
        backend_use_storage: bool = False,
        storage_bucket: str = "",
    ) -> None:
        """Initialize Firestore-backed sessions."""
        self._project_root = project_root
        self._sessions_dir = sessions_dir
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
        self._backend_use_storage = backend_use_storage
        self._storage_bucket = storage_bucket
        self._collection = "sessions"
        self._client = get_firestore_client()
        self._lock = asyncio.Lock()

    def session_dir(self, session_id: str) -> Path:
        """Return the session directory for a session ID."""
        return (self._sessions_dir / session_id).resolve()

    def progress_path(self, session_id: str) -> Path:
        """Return the progress.json path for a session."""
        return self.session_dir(session_id) / "progress.json"

    def _relative_path(self, path: Path) -> str:
        """Return a project-relative path string."""
        return str(path.relative_to(self._project_root))

    def _doc_ref(self, session_id: str):
        """Return the Firestore document reference for a session."""
        return self._client.collection(self._collection).document(session_id)

    def _state_from_doc(self, session_id: str, data: Dict[str, Any]) -> SessionState:
        """Convert Firestore document data into SessionState."""
        created_at = data.get("createdAt")
        if not isinstance(created_at, datetime):
            created_at = _utcnow()
        last_active = data.get("lastActiveAt")
        if not isinstance(last_active, datetime):
            last_active = created_at
        return SessionState(
            id=session_id,
            user_id=data.get("userId"),
            created_at=created_at,
            last_active_at=last_active,
            history=list(data.get("history") or []),
            files=dict(data.get("files") or {}),
            original_score=(
                data.get("originalScore") if not self._backend_use_storage else None
            ),
            original_score_path=data.get("originalScorePath"),
            preprocess_plan_history=list(data.get("preprocessPlanHistory") or []),
            preprocess_attempt_history=list(data.get("preprocessAttemptHistory") or []),
            last_preprocess_plan=(
                data.get("lastPreprocessPlan")
                if data.get("lastPreprocessPlan") is not None
                else data.get("lastSuccessfulPreprocessPlan")
            ),
            current_score=(
                data.get("currentScore") if not self._backend_use_storage else None
            ),
            current_score_path=data.get("currentScorePath"),
            current_score_version=int(data.get("currentScoreVersion") or 0),
            score_summary=data.get("scoreSummary"),
            current_audio=data.get("currentAudio"),
        )

    async def create_session(self, user_id: Optional[str]) -> SessionState:
        """Create a new session document and local directory."""
        async with self._lock:
            session_id = uuid.uuid4().hex
            now = _utcnow()
            payload = {
                "userId": user_id,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "lastActiveAt": firestore.SERVER_TIMESTAMP,
                "history": [],
                "files": {},
                "originalScore": None,
                "originalScorePath": None,
                "preprocessPlanHistory": [],
                "preprocessAttemptHistory": [],
                "lastPreprocessPlan": None,
                "currentScore": None,
                "currentScorePath": None,
                "currentScoreVersion": 0,
                "scoreSummary": None,
                "currentAudio": None,
            }
            self._doc_ref(session_id).set(payload)
            session_dir = self.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            return SessionState(
                id=session_id,
                user_id=user_id,
                created_at=now,
                last_active_at=now,
            )

    async def get_session(self, session_id: str, user_id: Optional[str]) -> SessionState:
        """Fetch a session by ID, enforcing ownership and TTL."""
        async with self._lock:
            doc = self._doc_ref(session_id).get()
            if not doc.exists:
                raise KeyError(session_id)
            data = doc.to_dict() or {}
            if user_id and data.get("userId") and data.get("userId") != user_id:
                raise PermissionError(session_id)
            self._doc_ref(session_id).update({"lastActiveAt": firestore.SERVER_TIMESTAMP})
            return self._state_from_doc(session_id, data)

    async def get_snapshot(self, session_id: str, user_id: Optional[str]) -> Dict[str, Any]:
        """Return a snapshot of a session for API responses."""
        async with self._lock:
            doc = self._doc_ref(session_id).get()
            if not doc.exists:
                raise KeyError(session_id)
            data = doc.to_dict() or {}
            if user_id and data.get("userId") and data.get("userId") != user_id:
                raise PermissionError(session_id)
            self._doc_ref(session_id).update({"lastActiveAt": firestore.SERVER_TIMESTAMP})
            state = self._state_from_doc(session_id, data)
            if self._backend_use_storage:
                if state.original_score_path:
                    state.original_score = _load_score_from_storage(
                        self._storage_bucket, state.original_score_path
                    )
                if state.current_score_path:
                    state.current_score = _load_score_from_storage(
                        self._storage_bucket, state.current_score_path
                    )
            return state.snapshot()

    async def append_history(self, session_id: str, role: str, content: str) -> None:
        """Append a chat entry to Firestore history."""
        async with self._lock:
            entry = {"role": role, "content": content}
            self._doc_ref(session_id).update(
                {
                    "history": firestore.ArrayUnion([entry]),
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )

    async def set_file(self, session_id: str, key: str, path: Path) -> None:
        """Associate a file path with the session in Firestore."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {
                    f"files.{key}": self._relative_path(path),
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )

    async def set_metadata(self, session_id: str, key: str, value: str) -> None:
        """Store metadata in the session files map."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {f"files.{key}": value, "lastActiveAt": firestore.SERVER_TIMESTAMP}
            )

    async def set_score(self, session_id: str, score: Dict[str, Any]) -> int:
        """Update the score and increment its version in Firestore."""
        async with self._lock:
            doc_ref = self._doc_ref(session_id)
            if self._backend_use_storage:
                version = self._reserve_next_score_version(doc_ref)
                user_id = self._require_user_id(doc_ref.get().to_dict() or {}, session_id)
                storage_path = _current_score_storage_path(user_id, session_id, version)
                _store_score_to_storage(self._storage_bucket, storage_path, score)
                doc_ref.update(
                    {
                        "currentScorePath": storage_path,
                        "currentScoreStorage": "gcs",
                        "currentScoreByteSize": len(_serialize_score(score)),
                        "currentScore": firestore.DELETE_FIELD,
                        "lastActiveAt": firestore.SERVER_TIMESTAMP,
                    }
                )
            else:
                doc = doc_ref.get()
                if not doc.exists:
                    raise KeyError(session_id)
                data = doc.to_dict() or {}
                version = int(data.get("currentScoreVersion") or 0) + 1
                doc_ref.update(
                    {
                        "currentScore": score,
                        "currentScoreVersion": version,
                        "lastActiveAt": firestore.SERVER_TIMESTAMP,
                    }
                )
            return version

    async def set_original_score(self, session_id: str, score: Dict[str, Any]) -> None:
        """Persist the original parsed score baseline in Firestore."""
        async with self._lock:
            doc_ref = self._doc_ref(session_id)
            if self._backend_use_storage:
                data = doc_ref.get().to_dict() or {}
                user_id = self._require_user_id(data, session_id)
                storage_path = _original_score_storage_path(user_id, session_id)
                _store_score_to_storage(self._storage_bucket, storage_path, score)
                doc_ref.update(
                    {
                        "originalScorePath": storage_path,
                        "originalScoreStorage": "gcs",
                        "originalScoreByteSize": len(_serialize_score(score)),
                        "originalScore": firestore.DELETE_FIELD,
                        "lastActiveAt": firestore.SERVER_TIMESTAMP,
                    }
                )
            else:
                doc_ref.update(
                    {"originalScore": score, "lastActiveAt": firestore.SERVER_TIMESTAMP}
                )

    async def set_score_summary(self, session_id: str, summary: Optional[Dict[str, Any]]) -> None:
        """Update the score summary in Firestore."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {"scoreSummary": summary, "lastActiveAt": firestore.SERVER_TIMESTAMP}
            )

    async def append_preprocess_plan(self, session_id: str, entry: Dict[str, Any]) -> None:
        """Append a generated preprocess plan entry in Firestore for debugging."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {
                    "preprocessPlanHistory": firestore.ArrayUnion([entry]),
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )

    async def append_preprocess_attempt_summary(
        self, session_id: str, entry: Dict[str, Any]
    ) -> None:
        """Append a preprocess attempt summary in Firestore for debugging."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {
                    "preprocessAttemptHistory": firestore.ArrayUnion([entry]),
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )

    async def set_last_preprocess_plan(
        self, session_id: str, plan: Optional[Dict[str, Any]]
    ) -> None:
        """Persist the latest preprocess plan in Firestore."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {
                    "lastPreprocessPlan": plan,
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )

    async def set_audio(
        self,
        session_id: str,
        path: Path,
        duration_s: float,
        storage_path: Optional[str] = None,
    ) -> None:
        """Store audio output metadata for the session."""
        async with self._lock:
            payload = {
                "path": self._relative_path(path),
                "duration_s": duration_s,
            }
            if storage_path:
                payload["storage_path"] = storage_path
            self._doc_ref(session_id).update(
                {"currentAudio": payload, "lastActiveAt": firestore.SERVER_TIMESTAMP}
            )

    async def reset_for_new_upload(self, session_id: str) -> None:
        """Clear score-specific Firestore session state and local derived artifacts."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {
                    "history": [],
                    "files": {},
                    "originalScore": None,
                    "originalScorePath": None,
                    "preprocessPlanHistory": [],
                    "preprocessAttemptHistory": [],
                    "lastPreprocessPlan": None,
                    "lastSuccessfulPreprocessPlan": None,
                    "currentScore": None,
                    "currentScorePath": None,
                    "currentScoreVersion": 0,
                    "scoreSummary": None,
                    "currentAudio": None,
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                }
            )
            session_dir = self.session_dir(session_id)
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            session_dir.mkdir(parents=True, exist_ok=True)

    async def evict_expired(self) -> None:
        """Firestore-backed sessions rely on TTL policies; no-op here."""
        return

    async def cleanup_expired_on_disk(self) -> int:
        """No-op for Firestore-backed sessions."""
        return 0

    def _reserve_next_score_version(self, doc_ref) -> int:
        """Atomically reserve the next current-score version."""
        transaction = self._client.transaction()

        @firestore.transactional
        def _reserve(txn):
            snapshot = doc_ref.get(transaction=txn)
            if not snapshot.exists:
                raise KeyError(doc_ref.id)
            data = snapshot.to_dict() or {}
            version = int(data.get("currentScoreVersion") or 0) + 1
            txn.update(
                doc_ref,
                {
                    "currentScoreVersion": version,
                    "lastActiveAt": firestore.SERVER_TIMESTAMP,
                },
            )
            return version

        return _reserve(transaction)

    def _require_user_id(self, data: Dict[str, Any], session_id: str) -> str:
        """Return the session user id or fail loudly."""
        user_id = data.get("userId")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError(f"Missing userId for storage-backed session {session_id}.")
        return user_id


def _serialize_score(score: Dict[str, Any]) -> bytes:
    """Serialize a score payload to UTF-8 JSON bytes."""
    return json.dumps(score, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _load_score_from_storage(bucket_name: str, path: str) -> Dict[str, Any]:
    """Load and deserialize a score payload from object storage."""
    raw = download_bytes(bucket_name, path)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Stored score payload is not a JSON object: {path}")
    return data


def _store_score_to_storage(bucket_name: str, path: str, score: Dict[str, Any]) -> int:
    """Serialize and upload a score payload to object storage."""
    payload = _serialize_score(score)
    upload_bytes(bucket_name, payload, path, "application/json")
    return len(payload)


def _original_score_storage_path(user_id: Optional[str], session_id: str) -> str:
    """Build the storage path for the original parsed score."""
    if not user_id:
        raise ValueError(f"Missing userId for storage-backed session {session_id}.")
    return f"sessions/{user_id}/{session_id}/scores/original.json"


def _current_score_storage_path(user_id: Optional[str], session_id: str, version: int) -> str:
    """Build the storage path for the current score version."""
    if not user_id:
        raise ValueError(f"Missing userId for storage-backed session {session_id}.")
    return f"sessions/{user_id}/{session_id}/scores/current.v{version}.json"
