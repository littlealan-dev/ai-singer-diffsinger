import os
import pytest
from datetime import datetime, timedelta, timezone
from src.backend.credits import (
    get_or_create_credits,
    estimate_credits,
    reserve_credits,
    settle_credits,
    release_credits,
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
    for collection in ["users", "credit_reservations", "credit_ledger"]:
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
    assert credits2.trial_granted_at == credits.trial_granted_at

def test_estimate_credits():
    assert estimate_credits(0) == 0
    assert estimate_credits(15) == 1  # 15s -> 1 credit (30s block)
    assert estimate_credits(30) == 1
    assert estimate_credits(31) == 2
    assert estimate_credits(60) == 2

def test_reserve_credits_success():
    uid = "test-user-2"
    get_or_create_credits(uid, "test2@example.com")
    
    job_id = "job-1"
    success = reserve_credits(uid, job_id, 3) # Request 3 credits
    assert success
    
    credits = get_or_create_credits(uid, "test2@example.com")
    assert credits.reserved == 3
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 3

def test_reserve_credits_insufficient():
    uid = "test-user-3"
    get_or_create_credits(uid, "test3@example.com")
    
    success = reserve_credits(uid, "job-2", TRIAL_CREDIT_AMOUNT + 1)
    assert not success
    
    credits = get_or_create_credits(uid, "test3@example.com")
    assert credits.reserved == 0

def test_settle_credits_exact():
    uid = "test-user-4"
    get_or_create_credits(uid, "test4@example.com")
    
    job_id = "job-3"
    reserve_credits(uid, job_id, 5) # Reserve 5
    
    # Settle with 2 credits (60s)
    actual_credits, overdrafted = settle_credits(uid, job_id, 60.0)
    assert actual_credits == 2
    assert not overdrafted
    
    credits = get_or_create_credits(uid, "test4@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 2
    assert credits.reserved == 0

def test_settle_credits_overdraft():
    uid = "test-user-5"
    get_or_create_credits(uid, "test5@example.com")
    
    job_id = "job-4"
    reserve_credits(uid, job_id, 5)
    
    # Settle with 12 credits (360s) -> should overdraft
    actual_credits, overdrafted = settle_credits(uid, job_id, 360.0)
    assert actual_credits == 12
    assert overdrafted
    
    credits = get_or_create_credits(uid, "test5@example.com")
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 12
    assert credits.overdrafted

def test_release_credits():
    uid = "test-user-6"
    get_or_create_credits(uid, "test6@example.com")
    
    job_id = "job-5"
    reserve_credits(uid, job_id, 4)
    
    success = release_credits(uid, job_id)
    assert success
    
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
            "balance": 10,
            "reserved": 0,
            "expiresAt": now - timedelta(days=1),
            "overdrafted": False
        }
    })
    
    # Should show as expired
    credits = get_or_create_credits(uid, "expired@example.com")
    assert credits.is_expired
    
    # Reservation should fail
    success = reserve_credits(uid, "job-6", 1)
    assert not success
