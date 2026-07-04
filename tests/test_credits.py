import os
from datetime import datetime, timedelta, timezone

import pytest

from src.backend.credits import (
    CREDIT_DURATION_SECONDS,
    EXPORT_MIX_CREDIT_DURATION_SECONDS,
    TRIAL_CREDIT_AMOUNT,
    estimate_credits,
    estimate_export_mix_credits,
    get_or_create_credits,
    mark_reservation_reconciliation_required,
    release_credits,
    reserve_credits,
    settle_credits,
    settle_credits_and_complete_job,
    settle_export_mix_credits_and_complete_job,
)
from src.backend.feedback import (
    FeedbackError,
    mark_feedback_prompted,
    normalize_feedback_comment,
    submit_audio_feedback,
)
from src.backend.firebase_app import get_firestore_client

os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
os.environ["GCLOUD_PROJECT"] = "demo-project"


@pytest.fixture(autouse=True)
def cleanup_firestore():
    db = get_firestore_client()
    for collection in [
        "users",
        "credit_reservations",
        "credit_ledger",
        "jobs",
        "stripe_events",
        "audio_feedback",
        "topup_packs",
        "topup_checkout_holds",
    ]:
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


def test_estimate_export_mix_credits():
    with pytest.raises(ValueError):
        estimate_export_mix_credits(0)
    assert estimate_export_mix_credits(0.1) == 1
    assert estimate_export_mix_credits(60.0) == 1
    assert estimate_export_mix_credits(60.1) == 2
    assert estimate_export_mix_credits(600.0) == 10
    assert EXPORT_MIX_CREDIT_DURATION_SECONDS == 60


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


def test_reserve_credits_stores_export_mix_metadata():
    uid = "test-export-reserve"
    get_or_create_credits(uid, "export-reserve@example.com")

    result = reserve_credits(
        uid,
        "export-job-1",
        3,
        session_id="session-export",
        job_kind="export_mix",
        pricing="export_mix_v1",
        pricing_unit_seconds=60,
        billable_duration_seconds=121.2,
        billing_reference_job_id="source-job-1",
    )

    assert result.status == "reserved"
    db = get_firestore_client()
    reservation = db.collection("credit_reservations").document("export-job-1").get().to_dict()
    assert reservation["jobKind"] == "export_mix"
    assert reservation["pricing"] == "export_mix_v1"
    assert reservation["pricingUnitSeconds"] == 60
    assert reservation["billableDurationSeconds"] == 121.2
    assert reservation["billingReferenceJobId"] == "source-job-1"
    ledger = db.collection("credit_ledger").document("reserve_export-job-1").get().to_dict()
    assert ledger["jobKind"] == "export_mix"
    assert ledger["pricingUnitSeconds"] == 60


def test_reserve_credits_insufficient():
    uid = "test-user-3"
    get_or_create_credits(uid, "test3@example.com")

    result = reserve_credits(uid, "job-2", TRIAL_CREDIT_AMOUNT + 1)
    assert result.status == "insufficient_balance"


def test_reserve_and_settle_consumes_topup_after_subscription_balance():
    uid = "test-topup-consume"
    get_or_create_credits(uid, "topup-consume@example.com")
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    db.collection("users").document(uid).set(
        {
            "credits": {"balance": 2, "reserved": 0, "overdrafted": False},
            "topupCredits": {
                "totalRemaining": 3,
                "activePackCount": 1,
                "earliestExpiresAt": now + timedelta(days=180),
            },
        },
        merge=True,
    )
    db.collection("topup_packs").document("topup-pack-1").set(
        {
            "userId": uid,
            "packId": "topup-pack-1",
            "creditsGranted": 15,
            "creditsRemaining": 3,
            "status": "active",
            "expiresAt": now + timedelta(days=180),
            "createdAt": now,
        }
    )

    reserve_result = reserve_credits(uid, "job-topup-1", 5)
    assert reserve_result.status == "reserved"
    reserved_user = db.collection("users").document(uid).get().to_dict() or {}
    assert reserved_user["credits"]["reserved"] == 2
    assert reserved_user["topupCredits"]["totalRemaining"] == 3
    assert reserved_user["topupCredits"]["totalReserved"] == 3
    assert reserved_user["topupCredits"]["totalAvailable"] == 0
    reservation = db.collection("credit_reservations").document("job-topup-1").get().to_dict() or {}
    assert reservation["reservedMonthlyCredits"] == 2
    assert reservation["reservedTopupCredits"] == 3
    assert reservation["reservedTopupPacks"] == [{"packId": "topup-pack-1", "credits": 3}]
    reserved_pack = db.collection("topup_packs").document("topup-pack-1").get().to_dict() or {}
    assert reserved_pack["creditsRemaining"] == 3
    assert reserved_pack["creditsReserved"] == 3

    settle_result = settle_credits(uid, "job-topup-1", 150.0)
    assert settle_result.status == "settled"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 0
    assert user["credits"]["reserved"] == 0
    assert user["topupCredits"]["totalRemaining"] == 0
    pack = db.collection("topup_packs").document("topup-pack-1").get().to_dict() or {}
    assert pack["creditsRemaining"] == 0
    assert pack["status"] == "exhausted"
    settle_ledger = db.collection("credit_ledger").document("settle_job-topup-1").get().to_dict() or {}
    assert settle_ledger["subscriptionCreditsConsumed"] == 2
    assert settle_ledger["topupCreditsConsumed"] == 3
    assert db.collection("credit_ledger").document("topup_consume_job-topup-1_topup-pack-1").get().exists


def test_settle_consumes_monthly_then_topup_packs_by_earliest_expiry():
    uid = "test-topup-consume-expiry-order"
    get_or_create_credits(uid, "topup-expiry-order@example.com")
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    earlier_expiry = now + timedelta(days=30)
    later_expiry = now + timedelta(days=180)
    db.collection("users").document(uid).set(
        {
            "credits": {"balance": 2, "reserved": 0, "overdrafted": False},
            "topupCredits": {
                "totalRemaining": 10,
                "totalReserved": 0,
                "totalAvailable": 10,
                "activePackCount": 2,
                "earliestExpiresAt": earlier_expiry,
            },
        },
        merge=True,
    )
    db.collection("topup_packs").document("topup-pack-early").set(
        {
            "userId": uid,
            "packId": "topup-pack-early",
            "creditsGranted": 15,
            "creditsRemaining": 4,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": earlier_expiry,
            "createdAt": now,
        }
    )
    db.collection("topup_packs").document("topup-pack-late").set(
        {
            "userId": uid,
            "packId": "topup-pack-late",
            "creditsGranted": 15,
            "creditsRemaining": 6,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": later_expiry,
            "createdAt": now,
        }
    )

    reserve_result = reserve_credits(uid, "job-topup-expiry-order", 8)
    assert reserve_result.status == "reserved"
    reservation = db.collection("credit_reservations").document("job-topup-expiry-order").get().to_dict() or {}
    assert reservation["reservedMonthlyCredits"] == 2
    assert reservation["reservedTopupCredits"] == 6
    assert reservation["reservedTopupPacks"] == [
        {"packId": "topup-pack-early", "credits": 4},
        {"packId": "topup-pack-late", "credits": 2},
    ]

    settle_result = settle_credits(uid, "job-topup-expiry-order", 240.0)

    assert settle_result.status == "settled"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 0
    assert user["credits"]["reserved"] == 0
    assert user["topupCredits"]["totalRemaining"] == 4
    assert user["topupCredits"]["totalReserved"] == 0
    assert user["topupCredits"]["totalAvailable"] == 4
    early_pack = db.collection("topup_packs").document("topup-pack-early").get().to_dict() or {}
    late_pack = db.collection("topup_packs").document("topup-pack-late").get().to_dict() or {}
    assert early_pack["creditsRemaining"] == 0
    assert early_pack["creditsReserved"] == 0
    assert early_pack["status"] == "exhausted"
    assert late_pack["creditsRemaining"] == 4
    assert late_pack["creditsReserved"] == 0
    assert late_pack["status"] == "active"
    settle_ledger = db.collection("credit_ledger").document("settle_job-topup-expiry-order").get().to_dict() or {}
    assert settle_ledger["subscriptionCreditsConsumed"] == 2
    assert settle_ledger["topupCreditsConsumed"] == 6
    assert db.collection("credit_ledger").document(
        "topup_consume_job-topup-expiry-order_topup-pack-early"
    ).get().exists
    assert db.collection("credit_ledger").document(
        "topup_consume_job-topup-expiry-order_topup-pack-late"
    ).get().exists


def test_settle_skips_expired_topup_pack_and_consumes_later_pack():
    uid = "test-topup-skip-expired"
    get_or_create_credits(uid, "topup-skip-expired@example.com")
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    expired_at = now - timedelta(days=1)
    later_expiry = now + timedelta(days=180)
    db.collection("users").document(uid).set(
        {
            "credits": {"balance": 2, "reserved": 0, "overdrafted": False},
            "topupCredits": {
                "totalRemaining": 10,
                "totalReserved": 0,
                "totalAvailable": 10,
                "activePackCount": 2,
                "earliestExpiresAt": expired_at,
            },
        },
        merge=True,
    )
    db.collection("topup_packs").document("topup-pack-expired").set(
        {
            "userId": uid,
            "packId": "topup-pack-expired",
            "creditsGranted": 15,
            "creditsRemaining": 4,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": expired_at,
            "createdAt": now - timedelta(days=181),
        }
    )
    db.collection("topup_packs").document("topup-pack-valid").set(
        {
            "userId": uid,
            "packId": "topup-pack-valid",
            "creditsGranted": 15,
            "creditsRemaining": 6,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": later_expiry,
            "createdAt": now,
        }
    )

    reserve_result = reserve_credits(uid, "job-topup-skip-expired", 5)
    assert reserve_result.status == "reserved"
    reservation = db.collection("credit_reservations").document("job-topup-skip-expired").get().to_dict() or {}
    assert reservation["reservedMonthlyCredits"] == 2
    assert reservation["reservedTopupCredits"] == 3
    assert reservation["reservedTopupPacks"] == [{"packId": "topup-pack-valid", "credits": 3}]
    expired_pack_after_reserve = db.collection("topup_packs").document("topup-pack-expired").get().to_dict() or {}
    assert expired_pack_after_reserve["status"] == "expired"
    assert expired_pack_after_reserve["creditsRemaining"] == 0
    assert expired_pack_after_reserve["creditsReserved"] == 0

    settle_result = settle_credits(uid, "job-topup-skip-expired", 150.0)

    assert settle_result.status == "settled"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 0
    assert user["credits"]["reserved"] == 0
    assert user["topupCredits"]["totalRemaining"] == 3
    assert user["topupCredits"]["totalReserved"] == 0
    assert user["topupCredits"]["totalAvailable"] == 3
    valid_pack = db.collection("topup_packs").document("topup-pack-valid").get().to_dict() or {}
    assert valid_pack["creditsRemaining"] == 3
    assert valid_pack["creditsReserved"] == 0
    assert valid_pack["status"] == "active"
    settle_ledger = db.collection("credit_ledger").document("settle_job-topup-skip-expired").get().to_dict() or {}
    assert settle_ledger["subscriptionCreditsConsumed"] == 2
    assert settle_ledger["topupCreditsConsumed"] == 3
    assert db.collection("credit_ledger").document(
        "topup_expire_topup-pack-expired"
    ).get().exists
    assert not db.collection("credit_ledger").document(
        "topup_consume_job-topup-skip-expired_topup-pack-expired"
    ).get().exists
    assert db.collection("credit_ledger").document(
        "topup_consume_job-topup-skip-expired_topup-pack-valid"
    ).get().exists


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


def test_settle_releases_unused_reserved_topup_when_actual_is_lower():
    uid = "test-topup-lower-actual"
    get_or_create_credits(uid, "topup-lower-actual@example.com")
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    db.collection("users").document(uid).set(
        {
            "credits": {"balance": 2, "reserved": 0, "overdrafted": False},
            "topupCredits": {
                "totalRemaining": 5,
                "totalReserved": 0,
                "totalAvailable": 5,
                "activePackCount": 1,
                "earliestExpiresAt": now + timedelta(days=180),
            },
        },
        merge=True,
    )
    db.collection("topup_packs").document("topup-pack-lower").set(
        {
            "userId": uid,
            "packId": "topup-pack-lower",
            "creditsGranted": 15,
            "creditsRemaining": 5,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": now + timedelta(days=180),
            "createdAt": now,
        }
    )

    reserve_result = reserve_credits(uid, "job-topup-lower", 5)
    assert reserve_result.status == "reserved"

    settle_result = settle_credits(uid, "job-topup-lower", 60.0)

    assert settle_result.status == "settled"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 0
    assert user["credits"]["reserved"] == 0
    assert user["topupCredits"]["totalRemaining"] == 5
    assert user["topupCredits"]["totalReserved"] == 0
    assert user["topupCredits"]["totalAvailable"] == 5
    pack = db.collection("topup_packs").document("topup-pack-lower").get().to_dict() or {}
    assert pack["creditsRemaining"] == 5
    assert pack["creditsReserved"] == 0
    settle_ledger = db.collection("credit_ledger").document("settle_job-topup-lower").get().to_dict() or {}
    assert settle_ledger["subscriptionCreditsConsumed"] == 2
    assert settle_ledger["topupCreditsConsumed"] == 0


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


def test_release_credits_releases_reserved_topup_packs():
    uid = "test-topup-release"
    get_or_create_credits(uid, "topup-release@example.com")
    db = get_firestore_client()
    now = datetime.now(timezone.utc)
    db.collection("users").document(uid).set(
        {
            "credits": {"balance": 1, "reserved": 0, "overdrafted": False},
            "topupCredits": {
                "totalRemaining": 4,
                "totalReserved": 0,
                "totalAvailable": 4,
                "activePackCount": 1,
                "earliestExpiresAt": now + timedelta(days=180),
            },
        },
        merge=True,
    )
    db.collection("topup_packs").document("topup-pack-release").set(
        {
            "userId": uid,
            "packId": "topup-pack-release",
            "creditsGranted": 15,
            "creditsRemaining": 4,
            "creditsReserved": 0,
            "status": "active",
            "expiresAt": now + timedelta(days=180),
            "createdAt": now,
        }
    )
    reserve_result = reserve_credits(uid, "job-topup-release", 5)
    assert reserve_result.status == "reserved"

    result = release_credits(uid, "job-topup-release")

    assert result.status == "released"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["reserved"] == 0
    assert user["topupCredits"]["totalRemaining"] == 4
    assert user["topupCredits"]["totalReserved"] == 0
    assert user["topupCredits"]["totalAvailable"] == 4
    pack = db.collection("topup_packs").document("topup-pack-release").get().to_dict() or {}
    assert pack["creditsRemaining"] == 4
    assert pack["creditsReserved"] == 0
    release_ledger = db.collection("credit_ledger").document("release_job-topup-release").get().to_dict() or {}
    assert release_ledger["monthlyReservedDelta"] == -1
    assert release_ledger["topupReservedDelta"] == -4


def test_release_credits_preserves_export_mix_ledger_metadata():
    uid = "test-export-release"
    get_or_create_credits(uid, "export-release@example.com")
    reserve_credits(
        uid,
        "export-job-release",
        2,
        session_id="session-export",
        job_kind="export_mix",
        pricing="export_mix_v1",
        pricing_unit_seconds=60,
        billable_duration_seconds=90.0,
        billing_reference_job_id="source-job-release",
    )

    result = release_credits(uid, "export-job-release")

    assert result.status == "released"
    release_ledger = (
        get_firestore_client()
        .collection("credit_ledger")
        .document("release_export-job-release")
        .get()
        .to_dict()
    )
    assert release_ledger["jobKind"] == "export_mix"
    assert release_ledger["pricing"] == "export_mix_v1"
    assert release_ledger["billableDurationSeconds"] == 90.0


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
        lossless_output_path="sessions/test/source.wav",
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 3

    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 3
    assert credits.reserved == 0

    job = db.collection("jobs").document(job_id).get().to_dict()
    assert job["status"] == "completed"
    assert job["audioUrl"] == "/sessions/session-11/audio?file=audio.mp3"
    assert job["losslessOutputPath"] == "sessions/test/source.wav"
    assert job["losslessAudioFormat"] == "wav"
    assert job["actualDurationSeconds"] == 61.0
    assert job["consumedCredits"] == 3

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


def test_settle_export_mix_credits_and_complete_job_uses_minute_rate():
    uid = "export-settle-user"
    email = "export-settle@example.com"
    session_id = "session-export-settle"
    job_id = "export-settle-job"
    db = get_firestore_client()

    get_or_create_credits(uid, email)
    reserve_credits(
        uid,
        job_id,
        2,
        session_id=session_id,
        job_kind="export_mix",
        pricing="export_mix_v1",
        pricing_unit_seconds=60,
        billable_duration_seconds=61.0,
        billing_reference_job_id="source-export-settle",
    )
    db.collection("jobs").document(job_id).set(
        {
            "userId": uid,
            "sessionId": session_id,
            "status": "queued",
            "jobKind": "export_mix",
        }
    )

    result = settle_export_mix_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        61.0,
        actual_duration_seconds=58.5,
        output_path="sessions/test/mix.wav",
        audio_url=f"/sessions/{session_id}/audio?file=mix.wav",
        mix_metadata={"format": "wav", "trackCount": 2},
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 2
    credits = get_or_create_credits(uid, email)
    assert credits.balance == TRIAL_CREDIT_AMOUNT - 2
    assert credits.reserved == 0
    job = db.collection("jobs").document(job_id).get().to_dict()
    assert job["status"] == "completed"
    assert job["actualDurationSeconds"] == 58.5
    assert job["consumedCredits"] == 2
    assert job["billing"]["pricing"] == "export_mix_v1"
    assert job["billing"]["billingReferenceJobId"] == "source-export-settle"
    assert job["mix"]["trackCount"] == 2
    settle_ledger = db.collection("credit_ledger").document(f"settle_{job_id}").get().to_dict()
    assert settle_ledger["jobKind"] == "export_mix"
    assert settle_ledger["amount"] == -2


def test_first_completed_job_marks_feedback_candidate(monkeypatch):
    monkeypatch.setenv("FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS", "5")
    monkeypatch.setenv("FEEDBACK_PROMPT_COOLDOWN_DAYS", "5")
    uid = "feedback-first-user"
    email = "feedback-first@example.com"
    session_id = "session-feedback-first"
    job_id = "feedback-first-job"
    db = get_firestore_client()

    get_or_create_credits(uid, email)
    reserve_credits(uid, job_id, 1, session_id=session_id)
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
        1.0,
        output_path="sessions/test/first.mp3",
        audio_url=f"/sessions/{session_id}/audio?file=first.mp3",
    )

    assert result.status == "completed_and_settled"
    job = db.collection("jobs").document(job_id).get().to_dict() or {}
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert job["feedback"]["promptCandidate"] is True
    assert job["feedback"]["prompted"] is False
    assert user["feedback"]["successfulGenerationsSinceLastPrompt"] == 1
    assert "lastPromptAt" not in user["feedback"]


def test_completed_job_marks_feedback_candidate_after_configured_generation_count(monkeypatch):
    monkeypatch.setenv("FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS", "2")
    monkeypatch.setenv("FEEDBACK_PROMPT_COOLDOWN_DAYS", "5")
    uid = "feedback-candidate-user"
    email = "feedback-candidate@example.com"
    session_id = "session-feedback"
    db = get_firestore_client()

    get_or_create_credits(uid, email)
    db.collection("users").document(uid).set(
        {
            "feedback": {
                "lastPromptAt": datetime.now(timezone.utc) - timedelta(days=6),
                "successfulGenerationsSinceLastPrompt": 0,
            }
        },
        merge=True,
    )
    for index in range(2):
        job_id = f"feedback-job-{index}"
        reserve_credits(uid, job_id, 1, session_id=session_id)
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
            1.0,
            output_path=f"sessions/test/{job_id}.mp3",
            audio_url=f"/sessions/{session_id}/audio?file={job_id}.mp3",
        )
        assert result.status == "completed_and_settled"

    first_job = db.collection("jobs").document("feedback-job-0").get().to_dict() or {}
    second_job = db.collection("jobs").document("feedback-job-1").get().to_dict() or {}
    user = db.collection("users").document(uid).get().to_dict() or {}

    assert "feedback" not in first_job
    assert second_job["feedback"]["promptCandidate"] is True
    assert second_job["feedback"]["prompted"] is False
    assert user["feedback"]["successfulGenerationsSinceLastPrompt"] == 2


def test_mark_feedback_prompted_consumes_prompt_and_submit_is_idempotent(monkeypatch):
    monkeypatch.setenv("FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS", "1")
    uid = "feedback-submit-user"
    email = "feedback-submit@example.com"
    session_id = "session-submit"
    job_id = "feedback-submit-job"
    db = get_firestore_client()

    get_or_create_credits(uid, email)
    reserve_credits(uid, job_id, 1, session_id=session_id)
    db.collection("jobs").document(job_id).set(
        {
            "userId": uid,
            "sessionId": session_id,
            "status": "queued",
        }
    )
    settle_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        1.0,
        output_path="sessions/test/submit.mp3",
        audio_url=f"/sessions/{session_id}/audio?file=submit.mp3",
    )

    prompted = mark_feedback_prompted(uid=uid, job_id=job_id, trigger="audio_played")
    assert prompted["status"] == "prompted"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["feedback"]["successfulGenerationsSinceLastPrompt"] == 0
    assert user["feedback"]["lastPromptJobId"] == job_id

    ratings = {
        "voiceQuality": 4,
        "pronunciation": 3,
        "timingRhythm": 5,
        "lyricsAlignment": 4,
        "partSplittingAccuracy": 2,
    }
    submitted = submit_audio_feedback(
        uid=uid,
        job_id=job_id,
        ratings=ratings,
        comment="Good timing.",
    )
    retry = submit_audio_feedback(
        uid=uid,
        job_id=job_id,
        ratings=ratings,
        comment="Different text ignored by idempotency.",
    )

    assert submitted == {"status": "submitted", "feedbackId": job_id}
    assert retry == submitted
    feedback = db.collection("audio_feedback").document(job_id).get().to_dict() or {}
    job = db.collection("jobs").document(job_id).get().to_dict() or {}
    assert feedback["ratings"] == ratings
    assert feedback["comment"] == "Good timing."
    assert job["feedback"]["submitted"] is True
    assert job["feedback"]["feedbackId"] == job_id


def test_feedback_comment_validation_rejects_control_characters():
    with pytest.raises(FeedbackError):
        normalize_feedback_comment("safe\x00unsafe")
