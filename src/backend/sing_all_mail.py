"""The email sent when a Sing All ends: its outbox document, its text and its Brevo send.

The outbox document is written in the transaction that ends the job (see
sing_all_store.finish), so a job's end queues its email exactly once. Sending
can fail; the sweeper retries unsent documents.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import httpx
from google.cloud import firestore

from src.backend.firebase_app import get_firestore_client
from src.backend.message_catalog import backend_message
from src.backend.sing_all import progress
from src.backend.waitlist import _load_brevo_api_key
from src.mcp.logging_utils import get_logger

OUTBOX = "mailOutbox"
EMAIL_STATUSES = frozenset({"completed", "partial", "failed"})
MAX_SEND_ATTEMPTS = 5
SEND_LEASE = timedelta(minutes=2)
BREVO_EMAIL_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT_SECONDS = 10.0

logger = get_logger(__name__)


def outbox_id(job_id: str, status: str) -> str:
    return f"{job_id}-{status}"


def emails_on(settings: Any) -> bool:
    return getattr(settings, "sing_all_emails", "off") == "on"


def queue_email(transaction: Any, db: Any, job: Mapping[str, Any], status: str) -> bool:
    """Write the outbox document inside the caller's job-ending transaction."""
    if status not in EMAIL_STATUSES or not job.get("notifyEmail"):
        return False
    transaction.set(db.collection(OUTBOX).document(outbox_id(job["jobId"], status)), {
        "kind": "sing_all_ended", "uid": job["uid"], "jobId": job["jobId"],
        "status": status, "to": job["notifyEmail"], "state": "pending",
        "createdAt": datetime.now(timezone.utc), "sentAt": None, "attempts": 0,
    })
    return True


def _receipt_label(row: Mapping[str, Any]) -> str:
    kind = row.get("type")
    if kind == "instrumental":
        return backend_message("sing_all.email.receipt.instrumentals")
    if kind == "mixes":
        files = int(row.get("files") or 0)
        return backend_message("sing_all.email.receipt.mixes_one" if files == 1
                               else "sing_all.email.receipt.mixes", files=files)
    if kind == "split":
        return backend_message("sing_all.email.receipt.split", name=row.get("name") or "")
    return backend_message("sing_all.email.receipt.take_solfege" if row.get("mode") == "solfege"
                           else "sing_all.email.receipt.take", name=row.get("name") or "")


def _receipt_amount(row: Mapping[str, Any]) -> str:
    return backend_message(f"sing_all.email.receipt.{row['outcome']}", credits=row["credits"])


def render_ended_email(job: Mapping[str, Any], base_url: str) -> tuple[str, str, str]:
    """Return the subject, plain text and HTML for an ended job, from its public receipt."""
    public = progress(job)
    status = public["status"]
    takes = public["counts"]["takes"]
    subject = backend_message(f"sing_all.email.subject.{status}", title=public["title"])
    first_line = backend_message(f"sing_all.email.first_line.{status}",
                                 done=takes["done"], takes=takes["total"])
    credits = public["credits"]
    totals = backend_message(
        "sing_all.email.receipt.totals",
        reserved=int((job.get("quote") or {}).get("total") or 0),
        charged=credits["charged"], refunded=credits["refunded"])
    zip_line = (backend_message("sing_all.email.zip_ready")
                if (public.get("package") or {}).get("state") == "done" else None)
    link = f"{base_url.rstrip('/')}/jobs/{public['jobId']}"
    button = backend_message("sing_all.email.button")
    rows = [(_receipt_label(row), _receipt_amount(row)) for row in public["receipt"]]

    heading = backend_message("sing_all.email.receipt.heading")
    text_lines = [first_line, ""]
    if zip_line:
        text_lines.extend([zip_line, ""])
    text_lines.append(heading)
    text_lines.extend(f"- {label}: {amount}" for label, amount in rows)
    text_lines.extend([totals, "", f"{button}: {link}"])

    escape = html.escape
    html_rows = "".join(
        f'<tr><td style="padding:2px 16px 2px 0">{escape(label)}</td>'
        f'<td style="padding:2px 0;text-align:right">{escape(amount)}</td></tr>'
        for label, amount in rows)
    html_body = (
        '<div style="font-family:sans-serif;font-size:15px;line-height:1.5">'
        f"<p>{escape(first_line)}</p>"
        + (f"<p>{escape(zip_line)}</p>" if zip_line else "")
        + f"<p><strong>{escape(heading)}</strong></p>"
        f'<table style="border-collapse:collapse">{html_rows}</table>'
        f"<p>{escape(totals)}</p>"
        f'<p><a href="{escape(link, quote=True)}" style="display:inline-block;padding:8px 16px;'
        f'background:#3b5bdb;color:#ffffff;text-decoration:none;border-radius:4px">'
        f"{escape(button)}</a></p></div>")
    return subject, "\n".join(text_lines), html_body


def _claim(doc_id: str) -> dict[str, Any] | None:
    """Take a short lease on an unsent document, so two senders never both send it."""
    db = get_firestore_client()
    ref = db.collection(OUTBOX).document(doc_id)

    @firestore.transactional
    def _take(transaction: Any) -> dict[str, Any] | None:
        doc = ref.get(transaction=transaction).to_dict()
        now = datetime.now(timezone.utc)
        if not doc or doc.get("state") != "pending":
            return None
        leased = doc.get("leaseUntil")
        if isinstance(leased, datetime) and leased > now:
            return None
        transaction.update(ref, {"leaseUntil": now + SEND_LEASE})
        return doc

    return _take(db.transaction())


def _record_sent(doc_id: str, doc: Mapping[str, Any]) -> None:
    db = get_firestore_client()
    batch = db.batch()
    batch.update(db.collection(OUTBOX).document(doc_id),
                 {"state": "sent", "sentAt": datetime.now(timezone.utc), "leaseUntil": None})
    batch.update(db.collection("singAllJobs").document(doc["jobId"]),
                 {"emailSentFor": firestore.ArrayUnion([doc["status"]])})
    batch.commit()


def _record_failure(doc_id: str, doc: Mapping[str, Any], error: str) -> bool:
    """Count one failed attempt; returns True when it was the last one."""
    attempts = int(doc.get("attempts") or 0) + 1
    final = attempts >= MAX_SEND_ATTEMPTS
    get_firestore_client().collection(OUTBOX).document(doc_id).update(
        {"attempts": attempts, "lastError": error, "leaseUntil": None,
         **({"state": "failed"} if final else {})})
    if final:
        logger.error("sing_all_email_send_failed attempts=%s error=%s", attempts, error)
    else:
        logger.warning("sing_all_email_send_retry attempts=%s error=%s", attempts, error)
    return final


def _load_job(job_id: str) -> dict[str, Any] | None:
    return get_firestore_client().collection("singAllJobs").document(job_id).get().to_dict()


async def _post(settings: Any, doc: Mapping[str, Any], job: Mapping[str, Any]) -> str | None:
    """Send the email through Brevo; returns None when Brevo accepted it, else the error."""
    subject, text, html_body = render_ended_email(job, settings.share_base_url)
    payload = {
        "sender": {"email": settings.sing_all_email_from},
        "replyTo": {"email": settings.sing_all_email_reply_to},
        "to": [{"email": doc["to"]}],
        "subject": subject, "textContent": text, "htmlContent": html_body,
    }
    try:
        api_key = await asyncio.to_thread(_load_brevo_api_key, settings)
        async with httpx.AsyncClient(timeout=BREVO_TIMEOUT_SECONDS) as client:
            response = await client.post(
                getattr(settings, "sing_all_email_api_url", BREVO_EMAIL_ENDPOINT),
                headers={"api-key": api_key, "accept": "application/json"}, json=payload)
    except (httpx.TimeoutException, httpx.TransportError):
        return "transport_error"
    except Exception as exc:
        # For example the key couldn't be read; counted as an attempt like any failure.
        return type(exc).__name__
    if 200 <= response.status_code < 300:
        return None
    try:
        code = str((response.json() or {}).get("code") or "")
    except ValueError:
        code = ""
    return f"{response.status_code} {code}".strip()


async def send_outbox(doc_id: str, settings: Any) -> str:
    """Send one queued email. Returns sent, retry, failed or skipped; never raises."""
    try:
        doc = await asyncio.to_thread(_claim, doc_id)
        if doc is None:
            return "skipped"
        job = await asyncio.to_thread(_load_job, doc["jobId"])
        error = await _post(settings, doc, job) if job else "job_missing"
        if error is None:
            await asyncio.to_thread(_record_sent, doc_id, doc)
            return "sent"
        final = await asyncio.to_thread(_record_failure, doc_id, doc, error)
        return "failed" if final else "retry"
    except Exception as exc:
        logger.error("sing_all_email_send_error error_type=%s", type(exc).__name__)
        return "retry"


async def send_unsent(settings: Any, *, limit: int = 50) -> int:
    """Send the pending outbox documents; the sweeper calls this. Returns how many were sent."""
    def _pending() -> list[str]:
        return [doc.id for doc in get_firestore_client().collection(OUTBOX)
                .where("state", "==", "pending").limit(limit).stream()]

    sent = 0
    for doc_id in await asyncio.to_thread(_pending):
        if await send_outbox(doc_id, settings) == "sent":
            sent += 1
    return sent
