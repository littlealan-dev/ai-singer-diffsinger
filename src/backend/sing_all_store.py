"""Firestore records for Sing All quotes and per-Song LLM answers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from src.backend.credits import DEFAULT_RESERVATION_TTL_SECONDS


def _answer_ref(db: Any, session_id: str, kind: str) -> Any:
    return db.collection("sessions").document(session_id).collection("singAll").document(kind)


def read_judgment(db: Any, session_id: str, input_hash: str) -> dict[str, Any] | None:
    """Find an answer among the last five for this Song."""
    record = _answer_ref(db, session_id, "judgment").get().to_dict() or {}
    return next((entry for entry in record.get("entries") or []
                 if entry.get("inputHash") == input_hash), None)


def latest_judgment(db: Any, session_id: str) -> dict[str, Any] | None:
    record = _answer_ref(db, session_id, "judgment").get().to_dict() or {}
    return next(iter(record.get("entries") or []), None)


def save_judgment(db: Any, session_id: str, answer: Mapping[str, Any]) -> None:
    ref = _answer_ref(db, session_id, "judgment")
    old = ref.get().to_dict() or {}
    entries = [dict(answer)] + [
        item for item in old.get("entries") or []
        if item.get("inputHash") != answer["inputHash"]
    ]
    ref.set({"entries": entries[:5]})


def read_style(db: Any, session_id: str) -> dict[str, Any] | None:
    return _answer_ref(db, session_id, "style").get().to_dict()


def save_style(db: Any, session_id: str, answer: Mapping[str, Any]) -> None:
    _answer_ref(db, session_id, "style").set(dict(answer))


def save_quote(db: Any, quote: Mapping[str, Any], *,
               now: datetime | None = None) -> dict[str, Any]:
    """Save a one-hour quote without touching the Song or credits."""
    instant = now or datetime.now(timezone.utc)
    quote_id = uuid4().hex
    record = {**quote, "quoteId": quote_id, "createdAt": instant,
              "expiresAt": instant + timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS)}
    db.collection("sing_all_quotes").document(quote_id).set(record)
    return record


def read_quote(db: Any, quote_id: str, *, uid: str) -> dict[str, Any] | None:
    record = db.collection("sing_all_quotes").document(quote_id).get().to_dict()
    if not record or record.get("uid") != uid:
        return None
    expiry = record.get("expiresAt")
    if isinstance(expiry, datetime) and expiry <= datetime.now(timezone.utc):
        return None
    return record
