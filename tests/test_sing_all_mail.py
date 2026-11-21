"""Step 5: a Sing All's end queues one email in its ending transaction, and Brevo sends it."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.backend import sing_all_mail, sing_all_store
from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all import progress
from tests.test_sing_all_mixes_credits import MIX_FILES, _parent, _runner, mixes_environment  # noqa: F401


OWNER_EMAIL = "owner@example.test"


def _settings(**overrides):
    values = {"max_render_slots": 1, "sing_all_emails": "on",
              "sing_all_email_from": "songs@example.test",
              "sing_all_email_reply_to": "help@example.test",
              "share_base_url": "https://app.example.test"}
    return SimpleNamespace(**{**values, **overrides})


class _Brevo:
    """Stands in for httpx.AsyncClient; answers each POST with the next status code."""

    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.requests = []

    def client(self, **_kwargs):
        brevo = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, headers, json):
                brevo.requests.append({"url": url, "headers": headers, "json": json})
                status = brevo.statuses.pop(0) if brevo.statuses else 201
                body = {} if status < 300 else {"code": "made_up_error", "message": OWNER_EMAIL}
                return SimpleNamespace(status_code=status, json=lambda: body)

        return _Client()


@pytest.fixture
def brevo(monkeypatch):
    fake = _Brevo()
    monkeypatch.setattr(sing_all_mail.httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(sing_all_mail, "_load_brevo_api_key", lambda _settings: "made-up-key")
    return fake


def _outbox(job_id, status):
    return (get_firestore_client().collection(sing_all_mail.OUTBOX)
            .document(sing_all_mail.outbox_id(job_id, status)).get().to_dict())


def _outbox_ids(job_id):
    return sorted(doc.id for doc in get_firestore_client().collection(sing_all_mail.OUTBOX)
                  .where("jobId", "==", job_id).stream())


@pytest.fixture
def email_environment(mixes_environment):
    db, uid, session_id, job_id = mixes_environment
    yield db, uid, session_id, job_id
    for doc_id in _outbox_ids(job_id):
        db.collection(sing_all_mail.OUTBOX).document(doc_id).delete()


def _run(uid, session_id, job_id, monkeypatch, *, takes_succeed, package, settings,
         notify=True, stop=False, passes=3):
    parent = _parent(uid, session_id, job_id)
    if notify:
        parent["notifyEmail"] = OWNER_EMAIL
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    if stop:
        get_firestore_client().collection("singAllJobs").document(job_id).update(
            {"cancelRequested": True})
    runner, _children = _runner(uid, job_id, takes_succeed=takes_succeed,
                                package=package, monkeypatch=monkeypatch)
    runner.orchestrator._settings = settings
    for _ in range(passes):
        asyncio.run(runner.reconcile(job_id))
    return runner


@pytest.mark.parametrize("takes_succeed,package,status,subject,first_line", [
    ((True, True), {"state": "done", "made": MIX_FILES}, "completed",
     "Your song is ready: Made-up tune", "Every part is sung."),
    ((True, False), {"state": "done", "made": 1}, "partial",
     "Your song is partly ready: Made-up tune", "1 of 2 takes were sung. The rest were refunded."),
    ((False, False), None, "failed",
     "We couldn't sing your song: Made-up tune", "No take could be sung. Your credits were refunded."),
])
def test_each_ending_sends_one_email_with_its_receipt_and_link(
    email_environment, monkeypatch, brevo, takes_succeed, package, status, subject, first_line,
):
    _db, uid, session_id, job_id = email_environment
    runner = _run(uid, session_id, job_id, monkeypatch, takes_succeed=takes_succeed,
                  package=package, settings=_settings())
    finished = sing_all_store.load(job_id)
    assert finished["status"] == status
    assert _outbox_ids(job_id) == [f"{job_id}-{status}"]
    outbox = _outbox(job_id, status)
    assert (outbox["state"], outbox["to"], outbox["attempts"]) == ("sent", OWNER_EMAIL, 0)
    assert finished["emailSentFor"] == [status]

    assert len(brevo.requests) == 1
    request = brevo.requests[0]
    assert request["url"] == sing_all_mail.BREVO_EMAIL_ENDPOINT
    assert request["headers"]["api-key"] == "made-up-key"
    sent = request["json"]
    assert sent["sender"] == {"email": "songs@example.test"}
    assert sent["replyTo"] == {"email": "help@example.test"}
    assert sent["to"] == [{"email": OWNER_EMAIL}]
    assert sent["subject"] == subject
    text = sent["textContent"]
    assert text.startswith(first_line)
    assert f"Open your song: https://app.example.test/jobs/{job_id}" in text
    assert f'href="https://app.example.test/jobs/{job_id}"' in sent["htmlContent"]
    public = progress(finished)
    assert (f"Reserved {finished['quote']['total']} cr · charged {public['credits']['charged']} cr"
            f" · refunded {public['credits']['refunded']} cr") in text
    assert "- Line 0: " in text
    assert ("Your zip is ready to download from the song." in text) == (
        (package or {}).get("state") == "done")

    # Reconciling and sweeping the ended job again sends nothing more.
    asyncio.run(runner.reconcile(job_id))
    asyncio.run(sing_all_mail.send_unsent(_settings()))
    assert len(brevo.requests) == 1
    assert _outbox_ids(job_id) == [f"{job_id}-{status}"]


@pytest.mark.parametrize("case", ["stopped", "no_address", "emails_off"])
def test_stop_no_address_and_emails_off_send_nothing(email_environment, monkeypatch, brevo, case):
    _db, uid, session_id, job_id = email_environment
    _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, True),
         package={"state": "done", "made": MIX_FILES},
         settings=_settings(sing_all_emails="off" if case == "emails_off" else "on"),
         notify=case != "no_address", stop=case == "stopped", passes=2)
    finished = sing_all_store.load(job_id)
    assert finished["status"] == ("cancelled" if case == "stopped" else "completed")
    assert _outbox_ids(job_id) == []
    assert brevo.requests == []
    assert "emailSentFor" not in finished


def test_a_failed_send_is_retried_by_the_sweeper_and_stops_after_five(
    email_environment, monkeypatch, brevo, caplog,
):
    _db, uid, session_id, job_id = email_environment
    brevo.statuses = [503]
    with caplog.at_level(logging.WARNING, logger=sing_all_mail.logger.name):
        _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, True),
             package={"state": "done", "made": MIX_FILES}, settings=_settings())
    doc_id = sing_all_mail.outbox_id(job_id, "completed")
    outbox = _outbox(job_id, "completed")
    assert (outbox["state"], outbox["attempts"], outbox["lastError"]) == ("pending", 1, "503 made_up_error")
    assert "emailSentFor" not in sing_all_store.load(job_id)

    # The sweeper's next pass sends it.
    asyncio.run(sing_all_mail.send_unsent(_settings()))
    assert _outbox(job_id, "completed")["state"] == "sent"
    assert sing_all_store.load(job_id)["emailSentFor"] == ["completed"]
    assert len(brevo.requests) == 2

    # A document that keeps failing stops after five attempts and logs an error.
    get_firestore_client().collection(sing_all_mail.OUTBOX).document(doc_id).update(
        {"state": "pending", "attempts": 0, "sentAt": None})
    brevo.statuses = [500] * 10
    with caplog.at_level(logging.WARNING, logger=sing_all_mail.logger.name):
        for _ in range(7):
            asyncio.run(sing_all_mail.send_outbox(doc_id, _settings()))
    outbox = _outbox(job_id, "completed")
    assert (outbox["state"], outbox["attempts"]) == ("failed", sing_all_mail.MAX_SEND_ATTEMPTS)
    assert len(brevo.requests) == 2 + sing_all_mail.MAX_SEND_ATTEMPTS
    assert any("sing_all_email_send_failed" in record.getMessage() for record in caplog.records)

    # No log line names the address, the job or the Song.
    messages = [record.getMessage() for record in caplog.records]
    assert messages
    for message in messages:
        assert OWNER_EMAIL not in message and job_id not in message and session_id not in message


def test_a_key_that_cannot_be_read_counts_as_a_failed_attempt(email_environment, monkeypatch, brevo):
    _db, uid, session_id, job_id = email_environment

    def no_key(_settings):
        raise RuntimeError("made-up secret failure")

    monkeypatch.setattr(sing_all_mail, "_load_brevo_api_key", no_key)
    _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, True),
         package={"state": "done", "made": MIX_FILES}, settings=_settings())
    outbox = _outbox(job_id, "completed")
    assert (outbox["state"], outbox["attempts"], outbox["lastError"]) == ("pending", 1, "RuntimeError")
    assert outbox["leaseUntil"] is None
    assert brevo.requests == []


def test_two_senders_at_once_send_one_email(email_environment, monkeypatch, brevo):
    _db, uid, session_id, job_id = email_environment
    brevo.statuses = [503]
    _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, True),
         package={"state": "done", "made": MIX_FILES}, settings=_settings())
    doc_id = sing_all_mail.outbox_id(job_id, "completed")

    async def both():
        return await asyncio.gather(sing_all_mail.send_outbox(doc_id, _settings()),
                                    sing_all_mail.send_outbox(doc_id, _settings()))

    outcomes = sorted(asyncio.run(both()))
    assert outcomes == ["sent", "skipped"]
    assert len(brevo.requests) == 2


def test_the_email_escapes_the_title_and_names_each_receipt_row():
    now = datetime.now(timezone.utc)
    job = _parent("example-user", "example-song", "example-job")
    job.update(title="Made-up <tune> & co", status="completed", stage="done", endedAt=now,
               package={"state": "failed", "mixes": {"done": 0, "total": 3}})
    for item in job["items"]:
        if item["type"] == "take":
            item.update(status="done", creditsCharged=2)
        else:
            item.update(status="failed", reason="package_failed")
    job["items"][1]["mode"] = "solfege"
    job["items"].append({"itemId": "instrumental", "type": "instrumental", "creditsReserved": 1,
                         "creditsCharged": 1, "status": "done", "reason": None})
    subject, text, html_body = sing_all_mail.render_ended_email(job, "https://app.example.test/")
    assert subject == "Your song is ready: Made-up <tune> & co"
    assert "Made-up <tune>" not in html_body
    assert "- Line 0: 2 cr" in text
    assert "- Line 1 (solfège): 2 cr" in text
    assert "- Instrumentals: 1 cr" in text
    assert "- Mixes (3 files): 3 cr refunded" in text
    assert "Your zip is ready" not in text
    assert "https://app.example.test/jobs/example-job" in text
