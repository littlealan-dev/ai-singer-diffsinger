from __future__ import annotations

"""Firestore-backed job tracking helpers."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from firebase_admin import firestore

from src.backend.firebase_app import get_firestore_client


@dataclass
class JobStore:
    """CRUD helpers for Firestore job documents."""
    collection: str = "jobs"
    _client: Optional[firestore.Client] = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> None:
        """Lazily initialize the Firestore client."""
        if self._client is None:
            self._client = get_firestore_client()

    def create_job(
        self,
        *,
        job_id: str,
        user_id: str,
        session_id: str,
        status: str,
        input_path: Optional[str] = None,
        render_type: Optional[str] = None,
    ) -> None:
        """Create a new job record with initial metadata."""
        payload: Dict[str, Any] = {
            "userId": user_id,
            "sessionId": session_id,
            "status": status,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if input_path:
            payload["inputPath"] = input_path
        if render_type:
            payload["renderType"] = render_type
        self._ensure_client()
        self._client.collection(self.collection).document(job_id).set(payload)

    def update_job(self, job_id: str, **fields: Any) -> None:
        """Update a job record with new fields and a fresh timestamp."""
        payload = dict(fields)
        payload["updatedAt"] = firestore.SERVER_TIMESTAMP
        self._ensure_client()
        self._client.collection(self.collection).document(job_id).set(payload, merge=True)

    def get_latest_job_by_session(
        self, *, user_id: str, session_id: str
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Return the most recent job for a user/session pair."""
        self._ensure_client()
        query = (
            self._client.collection(self.collection)
            .where("userId", "==", user_id)
            .where("sessionId", "==", session_id)
            .order_by("updatedAt", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        docs = list(query.stream())
        if not docs:
            return None
        doc = docs[0]
        data = doc.to_dict() or {}
        return doc.id, data


def build_progress_payload(job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize job data into a progress payload for clients."""
    status = data.get("status", "idle")
    if status == "completed":
        status = "done"
    elif status in {"failed", "cancelled"}:
        status = "error"
    payload: Dict[str, Any] = {
        "status": status,
        "step": data.get("step"),
        "message": data.get("message"),
        "progress": data.get("progress"),
        "audio_url": data.get("audioUrl"),
        "error": data.get("errorMessage"),
        "job_id": job_id,
        "job_kind": data.get("jobKind"),
        "review_required": data.get("reviewRequired"),
        "updated_at": data.get("updatedAt"),
    }
    return {key: value for key, value in payload.items() if value is not None}
