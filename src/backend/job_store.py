from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from firebase_admin import firestore

from src.backend.firebase_app import get_firestore_client


@dataclass
class JobStore:
    collection: str = "jobs"
    _client: Optional[firestore.Client] = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> None:
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
        payload = dict(fields)
        payload["updatedAt"] = firestore.SERVER_TIMESTAMP
        self._ensure_client()
        self._client.collection(self.collection).document(job_id).set(payload, merge=True)
