"""Firestore records for Sing All quotes and per-Song LLM answers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from google.api_core.exceptions import Aborted
from google.cloud import firestore

from src.backend.credits import (
    DEFAULT_RESERVATION_TTL_SECONDS, ReservationItem, ReserveCreditsResult,
    reserve_credits_for_items,
)
from src.backend.firebase_app import get_firestore_client
from src.backend.message_catalog import backend_message
from src.backend.session import _new_history_entry


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


class _JobReservationGuard:
    """Create the parent and set the one-active pointer in the reservation transaction."""

    def __init__(self, db: Any, job: Mapping[str, Any]) -> None:
        self._db = db
        self._job = dict(job)
        self._job_ref = db.collection("singAllJobs").document(job["jobId"])
        self._user_ref = db.collection("users").document(job["uid"])

    def read(self, transaction: Any) -> str | None:
        if self._job_ref.get(transaction=transaction).exists:
            return "sing_all_already_running"
        user = self._user_ref.get(transaction=transaction).to_dict() or {}
        active = user.get("activeSingAllJobId")
        if active:
            active_ref = self._db.collection("singAllJobs").document(active)
            active_job = active_ref.get(transaction=transaction).to_dict() or {}
            if active_job.get("status") == "running":
                return "sing_all_already_running"
        return None

    def write(self, transaction: Any) -> None:
        transaction.set(self._job_ref, self._job)
        transaction.update(self._user_ref, {"activeSingAllJobId": self._job["jobId"]})
        takes = sum(item["type"] == "take" for item in self._job.get("items") or [])
        message = backend_message(
            "sing_all.started_history", takes=takes,
            credits=int((self._job.get("quote") or {}).get("total") or 0),
        )
        entry = _new_history_entry("assistant", message,
                                   sing_all_job_id=self._job["jobId"])
        session_ref = self._db.collection("sessions").document(self._job["sessionId"])
        transaction.update(session_ref, {"history": firestore.ArrayUnion([entry])})


def create_job_with_reservations(
    job: Mapping[str, Any], *, instrumental_scope: str | None = None,
) -> ReserveCreditsResult:
    """Create a parent and reserve every priced item in one credit transaction."""
    reservations: list[ReservationItem] = []
    for item in job.get("items") or []:
        kind = item.get("type")
        if kind == "mixes":
            reservations.append(ReservationItem(
                job_id=item["reservationId"], estimated_credits=int(item["creditsReserved"]),
                billing_components=("mixes",), job_kind="sing_all_mixes",
            ))
            continue
        if kind not in {"take", "instrumental"}:
            continue
        instrumental = kind == "instrumental"
        reservations.append(ReservationItem(
            job_id=item["reservationId"] if instrumental else item["childJobId"],
            estimated_credits=int(item["creditsReserved"]),
            billing_components=("instrumental",) if instrumental else ("vocal",),
            job_kind="sing_all_instrumental" if instrumental else "synthesis",
            pricing_unit_seconds=item.get("unitSeconds"),
            billable_duration_seconds=item.get("estimatedSeconds"),
            instrumental_charge_scope_value=instrumental_scope if instrumental else None,
        ))
    return reserve_credits_for_items(
        job["uid"], reservations,
        guard=_JobReservationGuard(get_firestore_client(), job),
        session_id=job["sessionId"], score_id=job["scoreId"],
        score_version_no=job["quote"]["scoreVersion"],
        parent_job_id=job["jobId"],
    )


def load(job_id: str) -> dict[str, Any] | None:
    return get_firestore_client().collection("singAllJobs").document(job_id).get().to_dict()


def running_jobs() -> list[str]:
    """List parent ids for the scheduler's lost-notification sweep."""
    docs = (get_firestore_client().collection("singAllJobs")
            .where("status", "==", "running").stream())
    return [doc.id for doc in docs]


def settled_credits(reservation_id: str) -> int | None:
    """Read a charge that won the race against a parent release."""
    record = (get_firestore_client().collection("credit_reservations")
              .document(reservation_id).get().to_dict() or {})
    if record.get("status") != "settled":
        return None
    return int(record.get("actualCredits") or 0)


def active_job_for_song(uid: str, session_id: str) -> dict[str, Any] | None:
    """Return this Song's running parent, ignoring a stale active pointer."""
    db = get_firestore_client()
    user = db.collection("users").document(uid).get().to_dict() or {}
    job_id = user.get("activeSingAllJobId")
    if not job_id:
        return None
    job = db.collection("singAllJobs").document(job_id).get().to_dict() or {}
    return job if job.get("status") == "running" and job.get("sessionId") == session_id else None


def child_allowed(parent_job_id: str, item_id: str, child_job_id: str) -> bool:
    """Workers accept a child only while its parent still lists and runs it."""
    parent = load(parent_job_id) or {}
    return parent.get("status") == "running" and any(
        item.get("itemId") == item_id and item.get("childJobId") == child_job_id
        for item in parent.get("items") or []
    )


def apply(
    job_id: str, fields: Mapping[str, Any], *,
    expected_stage: str | None = None, expected_revision: int | None = None,
) -> bool:
    """Apply one reconciler decision only against its observed job revision."""
    db = get_firestore_client()
    ref = db.collection("singAllJobs").document(job_id)

    @firestore.transactional
    def _apply(transaction: Any) -> bool:
        current = ref.get(transaction=transaction).to_dict() or {}
        if current.get("status") != "running":
            return False
        if expected_stage is not None and current.get("stage") != expected_stage:
            return False
        revision = int(current.get("revision", 0))
        if expected_revision is not None and revision != expected_revision:
            return False
        transaction.update(ref, {**fields, "revision": revision + 1,
                                 "updatedAt": datetime.now(timezone.utc)})
        return True

    try:
        return _apply(db.transaction())
    except ValueError as exc:
        if isinstance(exc.__cause__, Aborted):
            # The Firestore transaction exhausted its contention retries. The
            # runner reloads the parent and attempts its decision again.
            return False
        raise


def finish(
    job_id: str, fields: Mapping[str, Any], *,
    expected_stage: str, expected_revision: int, queue_email: bool = False,
) -> bool:
    """End the parent, clear its guard and append its receipt in one transaction.

    With queue_email, the same transaction writes the ended email's outbox document.
    """
    from src.backend import sing_all_mail

    if fields.get("status") not in {"completed", "partial", "failed", "cancelled"}:
        raise ValueError("A terminal status is required.")
    db = get_firestore_client()
    job_ref = db.collection("singAllJobs").document(job_id)
    entry_id = uuid4().hex

    @firestore.transactional
    def _finish(transaction: Any) -> bool:
        current = job_ref.get(transaction=transaction).to_dict() or {}
        if (current.get("status") != "running"
                or current.get("stage") != expected_stage
                or int(current.get("revision", 0)) != expected_revision):
            return False
        uid, session_id = current["uid"], current["sessionId"]
        user_ref = db.collection("users").document(uid)
        session_ref = db.collection("sessions").document(session_id)
        user = user_ref.get(transaction=transaction).to_dict() or {}
        takes = [item for item in fields["items"] if item["type"] == "take"]
        message = backend_message(
            "sing_all.ended_history", status=fields["status"],
            done=sum(item["status"] == "done" for item in takes),
            takes=len(takes),
            credits=sum(int(item.get("creditsCharged") or 0) for item in fields["items"]),
        )
        entry = _new_history_entry("assistant", message, sing_all_job_id=job_id)
        entry["id"] = entry_id
        ended_at = datetime.now(timezone.utc)
        transaction.update(job_ref, {
            **fields, "revision": expected_revision + 1,
            "updatedAt": ended_at, "endedAt": ended_at,
        })
        if user.get("activeSingAllJobId") == job_id:
            transaction.update(user_ref, {"activeSingAllJobId": firestore.DELETE_FIELD})
        transaction.update(session_ref, {"history": firestore.ArrayUnion([entry])})
        if queue_email:
            sing_all_mail.queue_email(transaction, db, {**current, "jobId": job_id},
                                     fields["status"])
        return True

    try:
        return _finish(db.transaction())
    except ValueError as exc:
        if isinstance(exc.__cause__, Aborted):
            return False
        raise


def clear_active(uid: str, job_id: str) -> None:
    """Clear the guard only if it still names this parent job."""
    db = get_firestore_client()
    ref = db.collection("users").document(uid)

    @firestore.transactional
    def _clear(transaction: Any) -> None:
        user = ref.get(transaction=transaction).to_dict() or {}
        if user.get("activeSingAllJobId") == job_id:
            transaction.update(ref, {"activeSingAllJobId": firestore.DELETE_FIELD})

    _clear(db.transaction())
