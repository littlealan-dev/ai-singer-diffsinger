from __future__ import annotations

"""Session storage for scores, history, and audio outputs."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import shutil
import uuid

from firebase_admin import firestore

from src.backend.firebase_app import get_firestore_client

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
    current_score: Optional[Dict[str, Any]] = None
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
    ) -> None:
        """Initialize the store with TTL and storage paths."""
        self._project_root = project_root
        self._sessions_dir = sessions_dir
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
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
            state.current_score = score
            state.current_score_version += 1
            state.last_active_at = _utcnow()
            return state.current_score_version

    async def set_score_summary(self, session_id: str, summary: Optional[Dict[str, Any]]) -> None:
        """Attach a score summary to the session."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.score_summary = summary
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


class FirestoreSessionStore:
    """Firestore-backed session store."""
    def __init__(
        self,
        project_root: Path,
        sessions_dir: Path,
        ttl_seconds: int,
        max_sessions: int,
    ) -> None:
        """Initialize Firestore-backed sessions."""
        self._project_root = project_root
        self._sessions_dir = sessions_dir
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
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
            current_score=data.get("currentScore"),
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
                "currentScore": None,
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
            return self._state_from_doc(session_id, data).snapshot()

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

    async def set_score_summary(self, session_id: str, summary: Optional[Dict[str, Any]]) -> None:
        """Update the score summary in Firestore."""
        async with self._lock:
            self._doc_ref(session_id).update(
                {"scoreSummary": summary, "lastActiveAt": firestore.SERVER_TIMESTAMP}
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

    async def evict_expired(self) -> None:
        """Firestore-backed sessions rely on TTL policies; no-op here."""
        return

    async def cleanup_expired_on_disk(self) -> int:
        """No-op for Firestore-backed sessions."""
        return 0
