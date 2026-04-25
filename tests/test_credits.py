import os
from datetime import datetime, timedelta, timezone

import pytest

from src.backend.credits import (
    CREDIT_DURATION_SECONDS,
    TRIAL_CREDIT_AMOUNT,
    estimate_credits,
    get_or_create_credits,
    mark_reservation_reconciliation_required,
    release_credits,
    reserve_credits,
    settle_credits,
    settle_credits_and_complete_job,
)
from src.backend.firebase_app import get_firestore_client

os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
os.environ["GCLOUD_PROJECT"] = "demo-project"


@pytest.fixture(autouse=True)
def cleanup_firestore():
    db = get_firestore_client()
    for collection in ["users", "credit_reservations", "credit_ledger", "jobs", "stripe_events"]:
        for doc in db.collection(collection).list_documents():
            doc.delete()
    yield


def test_free_tier_bootstrap():
    credits = get_or_create_credits("test-user-1", "test@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT
    assert credits.reserved == 0
    assert credits.expires_at is None
    assert credits.monthly_allowance == TRIAL_CREDIT_AMOUNT
    assert credits.last_grant_type == "grant_free_monthly"
    assert not credits.is_expired


def test_active_legacy_trial_preserves_balance_on_migration():
    uid = "legacy-active"
    db = get_firestore_client()
    anchor = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)
    db.collection("users").document(uid).set(
        {
            "email": "legacy@example.com",
            "createdAt": anchor,
            "credits": {
                    "balance": 20,
                    "reserved": 0,
                    "expiresAt": datetime.now(timezone.utc) + timedelta(days=5),
                    "overdrafted": False,
                    "trialGrantedAt": anchor,
                    "trial_reset_v1": True,
                },
            }
    )

    credits = get_or_create_credits(uid, "legacy@example.com")
    user = db.collection("users").document(uid).get().to_dict() or {}
    billing = user["billing"]
    assert credits.balance == 20
    assert billing["activePlanKey"] == "free"
    assert billing["creditRefreshAnchor"] == anchor


def test_expired_legacy_trial_converts_to_free_tier():
    uid = "legacy-expired"
    db = get_firestore_client()
    then = datetime.now(timezone.utc) - timedelta(days=40)
    db.collection("users").document(uid).set(
        {
            "credits": {
                "balance": 0,
                "reserved": 0,
                "expiresAt": then,
                "overdrafted": False,
                "trialGrantedAt": then - timedelta(days=30),
                "trial_reset_v1": True,
            }
        }
    )

    credits = get_or_create_credits(uid, "expired@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT
    assert credits.expires_at is None
    assert reserve_credits(uid, "job-6", 1).status == "reserved"


def test_estimate_credits():
    assert estimate_credits(0) == 0
    assert estimate_credits(15) == 1
    assert estimate_credits(30) == 1
    assert estimate_credits(30.00018140589569) == 1
    assert estimate_credits(30.0006) == 2
    assert estimate_credits(31) == 2
    assert estimate_credits(60) == 2
    assert CREDIT_DURATION_SECONDS == 30


def test_reserve_credits_success():
    uid = "test-user-2"
    get_or_create_credits(uid, "test2@example.com")

    result = reserve_credits(uid, "job-1", 3, session_id="session-1")
    assert result.status == "reserved"

    credits = get_or_create_credits(uid, "test2@example.com")
    assert credits.reserved == 3
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 3
    reservation = get_firestore_client().collection("credit_reservations").document("job-1").get().to_dict()
    assert reservation["jobId"] == "job-1"
    assert reservation["sessionId"] == "session-1"


def test_reserve_credits_insufficient():
    uid = "test-user-3"
    get_or_create_credits(uid, "test3@example.com")

    result = reserve_credits(uid, "job-2", TRIAL_CREDIT_AMOUNT + 1)
    assert result.status == "insufficient_balance"


def test_settle_credits_exact():
    uid = "test-user-4"
    get_or_create_credits(uid, "test4@example.com")

    reserve_credits(uid, "job-3", 5)
    result = settle_credits(uid, "job-3", 60.0)
    assert result.status == "settled"
    assert result.actual_credits == 2
    assert not result.overdrafted

    credits = get_or_create_credits(uid, "test4@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 2
    assert credits.reserved == 0


def test_settle_credits_overdraft():
    uid = "test-user-5"
    get_or_create_credits(uid, "test5@example.com")

    reserve_credits(uid, "job-4", 5)
    result = settle_credits(uid, "job-4", 750.0)
    assert result.status == "settled"
    assert result.actual_credits == 25
    assert result.overdrafted

    credits = get_or_create_credits(uid, "test5@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 25
    assert credits.overdrafted


def test_release_credits():
    uid = "test-user-6"
    get_or_create_credits(uid, "test6@example.com")

    reserve_credits(uid, "job-5", 4)
    result = release_credits(uid, "job-5")
    assert result.status == "released"

    credits = get_or_create_credits(uid, "test6@example.com")
    assert credits.reserved == 0
    assert credits.balance == TRIAL_CREDIT_AMOUNT


def test_reserve_credits_duplicate_is_idempotent():
    uid = "test-user-8"
    get_or_create_credits(uid, "test8@example.com")

    first = reserve_credits(uid, "job-7", 2)
    second = reserve_credits(uid, "job-7", 2)

    assert first.status == "reserved"
    assert second.status == "reservation_exists"

    credits = get_or_create_credits(uid, "test8@example.com")
    assert credits.reserved == 2


def test_release_credits_reports_already_settled():
    uid = "test-user-9"
    get_or_create_credits(uid, "test9@example.com")

    reserve_credits(uid, "job-8", 2)
    settle_credits(uid, "job-8", 30.0)

    result = release_credits(uid, "job-8")
    assert result.status == "already_settled"


def test_mark_reconciliation_required_updates_reservation():
    uid = "test-user-10"
    get_or_create_credits(uid, "test10@example.com")
    reserve_credits(uid, "job-9", 1)

    marked = mark_reservation_reconciliation_required(
        uid,
        "job-9",
        last_error="release_failed",
        last_error_message="boom",
    )

    assert marked is True
    reservation = get_firestore_client().collection("credit_reservations").document("job-9").get().to_dict()
    assert reservation["status"] == "reconciliation_required"
    assert reservation["lastError"] == "release_failed"


def test_settle_credits_and_complete_job_is_atomic_and_idempotent():
    uid = "test-user-11"
    session_id = "session-11"
    email = "test11@example.com"
    job_id = "job-10"
    db = get_firestore_client()

    get_or_create_credits(uid, email)
    reserve_credits(uid, job_id, 2)
    db.collection("jobs").document(job_id).set(
        {
            "userId": uid,
            "sessionId": session_id,
            "status": "queued",
        }
    )

    result = settle_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        61.0,
        output_path="sessions/test/audio.mp3",
        audio_url="/sessions/session-11/audio?file=audio.mp3",
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 3

    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 3
    assert credits.reserved == 0

    job = db.collection("jobs").document(job_id).get().to_dict()
    assert job["status"] == "completed"
    assert job["audioUrl"] == "/sessions/session-11/audio?file=audio.mp3"

    ledger = list(
        db.collection("credit_ledger")
        .where("jobId", "==", job_id)
        .where("type", "==", "settle")
        .stream()
    )
    assert len(ledger) == 1

    retry_result = settle_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        61.0,
        output_path="sessions/test/audio.mp3",
        audio_url="/sessions/session-11/audio?file=audio.mp3",
    )

    assert retry_result.status == "already_completed_and_settled"
