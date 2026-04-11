import os
import pytest
from datetime import datetime, timedelta, timezone
from src.backend.credits import (
    get_or_create_credits,
    estimate_credits,
    reserve_credits,
    settle_credits,
    settle_credits_and_complete_job,
    release_credits,
    mark_reservation_reconciliation_required,
    CREDIT_DURATION_SECONDS,
    TRIAL_CREDIT_AMOUNT
)
from src.backend.firebase_app import get_firestore_client

# Ensure we use the emulator
os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
os.environ["GCLOUD_PROJECT"] = "demo-project"

@pytest.fixture(autouse=True)
def cleanup_firestore():
    """Clear Firestore before each test."""
    db = get_firestore_client()
    # Delete all documents in relevant collections
    for collection in ["users", "credit_reservations", "credit_ledger", "jobs"]:
        docs = db.collection(collection).list_documents()
        for doc in docs:
            doc.delete()
    yield

def test_trial_grant():
    uid = "test-user-1"
    email = "test@example.com"
    
    # First call should grant trial credits
    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT
    assert credits.reserved == 0
    assert not credits.overdrafted
    assert not credits.is_expired
    
    # Second call should return the same
    credits2 = get_or_create_credits(uid, email)
    assert credits2.balance == TRIAL_CREDIT_AMOUNT
    # Note: depends on get_or_create_credits correctly mapping trialGrantedAt to trial_granted_at
    assert credits2.trial_granted_at == credits.trial_granted_at

def test_estimate_credits():
    assert estimate_credits(0) == 0
    assert estimate_credits(15) == 1  # 15s -> 1 credit (30s block)
    assert estimate_credits(30) == 1
    assert estimate_credits(30.00018140589569) == 1
    assert estimate_credits(30.0006) == 2
    assert estimate_credits(31) == 2
    assert estimate_credits(60) == 2

def test_reserve_credits_success():
    uid = "test-user-2"
    get_or_create_credits(uid, "test2@example.com")
    
    job_id = "job-1"
    result = reserve_credits(uid, job_id, 3, session_id="session-1") # Request 3 credits
    assert result.status == "reserved"
    
    credits = get_or_create_credits(uid, "test2@example.com")
    assert credits.reserved == 3
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 3
    db = get_firestore_client()
    reservation = db.collection("credit_reservations").document(job_id).get().to_dict()
    assert reservation["jobId"] == job_id
    assert reservation["sessionId"] == "session-1"

def test_reserve_credits_insufficient():
    uid = "test-user-3"
    get_or_create_credits(uid, "test3@example.com")
    
    result = reserve_credits(uid, "job-2", TRIAL_CREDIT_AMOUNT + 1)
    assert result.status == "insufficient_balance"
    
    credits = get_or_create_credits(uid, "test3@example.com")
    assert credits.reserved == 0

def test_settle_credits_exact():
    uid = "test-user-4"
    get_or_create_credits(uid, "test4@example.com")
    
    job_id = "job-3"
    reserve_credits(uid, job_id, 5) # Reserve 5
    
    # Settle with 2 credits (60s)
    result = settle_credits(uid, job_id, 60.0)
    assert result.status == "settled"
    assert result.actual_credits == 2
    assert not result.overdrafted
    
    credits = get_or_create_credits(uid, "test4@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 2
    assert credits.reserved == 0

def test_settle_credits_overdraft():
    uid = "test-user-5"
    get_or_create_credits(uid, "test5@example.com")
    
    job_id = "job-4"
    reserve_credits(uid, job_id, 5)
    
    # Settle with 25 credits (750s) -> should overdraft now that trial is 20
    result = settle_credits(uid, job_id, 750.0)
    assert result.status == "settled"
    assert result.actual_credits == 25
    assert result.overdrafted
    
    credits = get_or_create_credits(uid, "test5@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 25
    assert credits.overdrafted

def test_release_credits():
    uid = "test-user-6"
    get_or_create_credits(uid, "test6@example.com")
    
    job_id = "job-5"
    reserve_credits(uid, job_id, 4)
    
    result = release_credits(uid, job_id)
    assert result.status == "released"
    
    credits = get_or_create_credits(uid, "test6@example.com")
    assert credits.reserved == 0
    assert credits.balance == TRIAL_CREDIT_AMOUNT

def test_expired_credits():
    uid = "test-user-7"
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    
    # Manually create expired user
    db.collection("users").document(uid).set({
        "credits": {
            "balance": TRIAL_CREDIT_AMOUNT,
            "reserved": 0,
            "expiresAt": now - timedelta(days=1),
            "overdrafted": False,
            "trial_reset_v1": True
        }
    })
    
    # Should show as expired
    credits = get_or_create_credits(uid, "expired@example.com")
    assert credits.is_expired
    
    # Reservation should fail
    result = reserve_credits(uid, "job-6", 1)
    assert result.status == "expired"


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

    job_id = "job-8"
    reserve_credits(uid, job_id, 2)
    settle_credits(uid, job_id, 30.0)

    result = release_credits(uid, job_id)
    assert result.status == "already_settled"


def test_mark_reconciliation_required_updates_reservation():
    uid = "test-user-10"
    get_or_create_credits(uid, "test10@example.com")
    job_id = "job-9"
    reserve_credits(uid, job_id, 1)

    marked = mark_reservation_reconciliation_required(
        uid,
        job_id,
        last_error="release_failed",
        last_error_message="boom",
    )

    assert marked is True
    db = get_firestore_client()
    reservation = db.collection("credit_reservations").document(job_id).get().to_dict()
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

    credits_after_retry = get_or_create_credits(uid, email)
    assert credits_after_retry.balance == TRIAL_CREDIT_AMOUNT - 3
    assert credits_after_retry.reserved == 0

def test_trial_reset_v1_existing_user():
    uid = "old-user-1"
    email = "old@example.com"
    db = get_firestore_client()
    
    # Manually create user with old credits (e.g. 5) and NO reset flag
    db.collection("users").document(uid).set({
        "credits": {
            "balance": 5,
            "reserved": 0,
            "expiresAt": datetime.now(timezone.utc) + timedelta(days=5),
            "overdrafted": False
        }
    })
    
    # Call get_or_create_credits - should trigger reset
    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT
    assert credits.trial_reset_v1 is True
    user_data = db.collection("users").document(uid).get().to_dict()
    assert user_data["metadata"]["pendingAnnouncementId"] == "trial_reset_v1"
    
    # Check ledger for reset entry
    ledger = list(db.collection("credit_ledger").where("userId", "==", uid).where("type", "==", "trial_reset").stream())
    assert len(ledger) == 1
    assert ledger[0].to_dict()["amount"] == TRIAL_CREDIT_AMOUNT

def test_trial_reset_v1_only_once():
    uid = "old-user-2"
    email = "old2@example.com"
    
    # First call triggers reset
    credits1 = get_or_create_credits(uid, email)
    assert credits1.balance == TRIAL_CREDIT_AMOUNT
    
    # Modify credits manually to simulate usage
    db = get_firestore_client()
    db.collection("users").document(uid).update({"credits.balance": 10})
    
    # Second call should NOT trigger reset
    credits2 = get_or_create_credits(uid, email)
    assert credits2.balance == 10
    assert credits2.trial_reset_v1 is True

def test_new_user_starts_with_reset_flag():
    uid = "new-user-99"
    email = "new@example.com"
    
    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT
    assert credits.trial_reset_v1 is True
    
    # Ledger should NOT have a "reset" entry for new users (it's initial grant)
    db = get_firestore_client()
    ledger = list(db.collection("credit_ledger").where("userId", "==", uid).where("type", "==", "trial_reset").stream())
    assert len(ledger) == 0
    user_data = db.collection("users").document(uid).get().to_dict()
    metadata = user_data.get("metadata", {})
    assert "pendingAnnouncementId" not in metadata

def test_new_user_does_not_auto_mark_announcement_seen():
    uid = "new-user-announcement"
    email = "announce@example.com"

    get_or_create_credits(uid, email)

    db = get_firestore_client()
    user_data = db.collection("users").document(uid).get().to_dict()
    metadata = user_data.get("metadata", {})
    assert "lastSeenAnnouncementId" not in metadata
