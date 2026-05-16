from __future__ import annotations

"""Audio generation feedback persistence and prompt tracking."""

from datetime import datetime, timezone
import re
from typing import Any, Mapping

from google.cloud import firestore

from src.backend.firebase_app import get_firestore_client

FEEDBACK_RATING_FIELDS = (
    "voiceQuality",
    "pronunciation",
    "timingRhythm",
    "lyricsAlignment",
    "partSplittingAccuracy",
)
MAX_FEEDBACK_COMMENT_CHARS = 4000
_UNSAFE_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FeedbackError(ValueError):
    """Expected feedback request validation failure."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_feedback_comment(value: Any) -> str:
    """Validate and normalize user-provided free-form feedback text."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FeedbackError("Feedback comment must be text.")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _UNSAFE_CONTROL_CHARS.search(normalized):
        raise FeedbackError("Feedback comment contains unsupported control characters.")
    if len(normalized) > MAX_FEEDBACK_COMMENT_CHARS:
        raise FeedbackError(
            f"Feedback comment must be {MAX_FEEDBACK_COMMENT_CHARS} characters or less."
        )
    return normalized


def validate_feedback_ratings(value: Any) -> dict[str, int]:
    """Validate feedback ratings and return normalized integer values."""
    if not isinstance(value, Mapping):
        raise FeedbackError("Feedback ratings are required.")
    ratings: dict[str, int] = {}
    for field in FEEDBACK_RATING_FIELDS:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise FeedbackError(f"{field} rating must be an integer from 1 to 5.")
        if raw < 1 or raw > 5:
            raise FeedbackError(f"{field} rating must be an integer from 1 to 5.")
        ratings[field] = raw
    return ratings


def mark_feedback_prompted(*, uid: str, job_id: str, trigger: str) -> dict[str, str]:
    """Mark that the feedback prompt was actually shown for a candidate job."""
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    job_ref = db.collection("jobs").document(job_id)
    normalized_trigger = trigger if trigger in {"audio_played", "audio_downloaded"} else "unknown"

    @firestore.transactional
    def _transactional_mark(transaction):
        job_snapshot = job_ref.get(transaction=transaction)
        if not job_snapshot.exists:
            raise FeedbackError("Job not found.", status_code=404)
        job = job_snapshot.to_dict() or {}
        if job.get("userId") != uid:
            raise FeedbackError("Job not found.", status_code=404)
        feedback = job.get("feedback") if isinstance(job.get("feedback"), dict) else {}
        if not feedback.get("promptCandidate"):
            raise FeedbackError("Job is not a feedback prompt candidate.", status_code=409)
        if feedback.get("submitted"):
            return {"status": "submitted"}
        if feedback.get("prompted"):
            return {"status": "prompted"}

        now = datetime.now(timezone.utc)
        transaction.set(
            job_ref,
            {
                "feedback": {
                    "prompted": True,
                    "promptedAt": now,
                    "promptTrigger": normalized_trigger,
                },
                "updatedAt": now,
            },
            merge=True,
        )
        transaction.set(
            user_ref,
            {
                "feedback": {
                    "lastPromptAt": now,
                    "lastPromptJobId": job_id,
                    "successfulGenerationsSinceLastPrompt": 0,
                }
            },
            merge=True,
        )
        return {"status": "prompted"}

    transaction = db.transaction()
    return _transactional_mark(transaction)


def submit_audio_feedback(
    *,
    uid: str,
    job_id: str,
    ratings: Mapping[str, Any],
    comment: Any,
    client: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Persist one idempotent feedback document for a completed synthesis job."""
    normalized_ratings = validate_feedback_ratings(ratings)
    normalized_comment = normalize_feedback_comment(comment)
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    job_ref = db.collection("jobs").document(job_id)
    feedback_ref = db.collection("audio_feedback").document(job_id)

    @firestore.transactional
    def _transactional_submit(transaction):
        job_snapshot = job_ref.get(transaction=transaction)
        if not job_snapshot.exists:
            raise FeedbackError("Job not found.", status_code=404)
        job = job_snapshot.to_dict() or {}
        if job.get("userId") != uid:
            raise FeedbackError("Job not found.", status_code=404)
        if job.get("status") != "completed":
            raise FeedbackError("Job is not completed.", status_code=409)

        existing_feedback = feedback_ref.get(transaction=transaction)
        if existing_feedback.exists:
            return {"status": "submitted", "feedbackId": job_id}

        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "feedbackId": job_id,
            "userId": uid,
            "jobId": job_id,
            "sessionId": job.get("sessionId"),
            "ratings": normalized_ratings,
            "comment": normalized_comment,
            "commentLength": len(normalized_comment),
            "createdAt": now,
        }
        if client:
            payload["client"] = {
                key: str(value)[:500]
                for key, value in client.items()
                if key in {"appVersion", "userAgent"} and value is not None
            }
        transaction.set(feedback_ref, payload)
        transaction.set(
            job_ref,
            {
                "feedback": {
                    "submitted": True,
                    "feedbackId": job_id,
                    "submittedAt": now,
                },
                "updatedAt": now,
            },
            merge=True,
        )
        transaction.set(
            user_ref,
            {
                "feedback": {
                    "lastSubmittedAt": now,
                    "lastSubmittedJobId": job_id,
                }
            },
            merge=True,
        )
        return {"status": "submitted", "feedbackId": job_id}

    transaction = db.transaction()
    return _transactional_submit(transaction)
