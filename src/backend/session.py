from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import shutil
import uuid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionState:
    id: str
    created_at: datetime
    last_active_at: datetime
    history: List[Dict[str, str]] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)
    current_score: Optional[Dict[str, Any]] = None
    current_score_version: int = 0
    score_summary: Optional[Dict[str, Any]] = None
    current_audio: Optional[Dict[str, Any]] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
            "history": list(self.history),
            "files": dict(self.files),
            "current_score": self._score_snapshot(),
            "score_summary": dict(self.score_summary) if self.score_summary else None,
            "current_audio": dict(self.current_audio) if self.current_audio else None,
        }

    def _score_snapshot(self) -> Optional[Dict[str, Any]]:
        if self.current_score is None:
            return None
        return {"score": self.current_score, "version": self.current_score_version}


class SessionStore:
    def __init__(
        self,
        project_root: Path,
        sessions_dir: Path,
        ttl_seconds: int,
        max_sessions: int,
    ) -> None:
        self._project_root = project_root
        self._sessions_dir = sessions_dir
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
        self._sessions: Dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    def session_dir(self, session_id: str) -> Path:
        return (self._sessions_dir / session_id).resolve()

    def progress_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "progress.json"

    def _relative_path(self, path: Path) -> str:
        return str(path.relative_to(self._project_root))

    async def create_session(self) -> SessionState:
        async with self._lock:
            self._evict_expired_locked()
            self._evict_overflow_locked()
            session_id = uuid.uuid4().hex
            now = _utcnow()
            state = SessionState(id=session_id, created_at=now, last_active_at=now)
            self._sessions[session_id] = state
            session_dir = self.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            return state

    async def get_session(self, session_id: str) -> SessionState:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            if self._is_expired(state):
                self._remove_session_locked(session_id)
                raise KeyError(session_id)
            state.last_active_at = _utcnow()
            return state

    async def get_snapshot(self, session_id: str) -> Dict[str, Any]:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            if self._is_expired(state):
                self._remove_session_locked(session_id)
                raise KeyError(session_id)
            state.last_active_at = _utcnow()
            return state.snapshot()

    async def append_history(self, session_id: str, role: str, content: str) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.history.append({"role": role, "content": content})
            state.last_active_at = _utcnow()

    async def set_file(self, session_id: str, key: str, path: Path) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.files[key] = self._relative_path(path)
            state.last_active_at = _utcnow()

    async def set_metadata(self, session_id: str, key: str, value: str) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.files[key] = value
            state.last_active_at = _utcnow()

    async def set_score(self, session_id: str, score: Dict[str, Any]) -> int:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.current_score = score
            state.current_score_version += 1
            state.last_active_at = _utcnow()
            return state.current_score_version

    async def set_score_summary(self, session_id: str, summary: Optional[Dict[str, Any]]) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.score_summary = summary
            state.last_active_at = _utcnow()

    async def set_audio(self, session_id: str, path: Path, duration_s: float) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise KeyError(session_id)
            state.current_audio = {
                "path": self._relative_path(path),
                "duration_s": duration_s,
            }
            state.last_active_at = _utcnow()

    async def evict_expired(self) -> None:
        async with self._lock:
            self._evict_expired_locked()

    async def cleanup_expired_on_disk(self) -> int:
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
        return _utcnow() - state.last_active_at > self._ttl

    def _evict_expired_locked(self) -> None:
        expired = [sid for sid, s in self._sessions.items() if self._is_expired(s)]
        for sid in expired:
            self._remove_session_locked(sid)

    def _evict_overflow_locked(self) -> None:
        if self._max_sessions <= 0:
            return
        if len(self._sessions) <= self._max_sessions:
            return
        ordered = sorted(self._sessions.values(), key=lambda s: s.last_active_at)
        while len(self._sessions) > self._max_sessions and ordered:
            self._remove_session_locked(ordered.pop(0).id)

    def _remove_session_locked(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def _latest_mtime(self, session_dir: Path) -> Optional[float]:
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
