import os
from datetime import datetime, timedelta, timezone

import pytest

from src.backend.credits import (
    get_synthesis_quote,
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
    active_synthesis_quote_context,
    canonical_render_choices_hash,
    ui_voicebank_conflicts_with_quote,
    create_synthesis_quote,
    instrumental_charge_scope,
)
from src.backend.synthesis_pricing import estimate_synthesis_credits
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
def cleanup_firestore(firestore_emulator):
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
        "synthesis_quotes",
        "instrumental_generation_charges",
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


def _create_combined_synthesis_quote(uid: str, session_id: str, score_id: str):
    scope = instrumental_charge_scope(uid, session_id, score_id)
    estimate = estimate_synthesis_credits(
        vocal_duration_seconds=60.0,
        vocal_part_id="P1",
        expand_repeats=True,
        has_instrumental_parts=True,
        instrumental_charge_required=True,
        instrumental_charge_scope=scope,
    ).to_dict()
    render_choices = {
        "voicebank": "test-voice",
        "language": "en",
        "part_id": "P1",
        "lyric_selection": {"id": "P1-L1", "number": "1", "name": "Verse 1"},
        "expand_repeats": True,
        "require_solfege_lyrics": False,
        "solfege_pronunciation_patch": False,
    }
    quote = create_synthesis_quote(
        user_id=uid,
        session_id=session_id,
        score_id=score_id,
        score_version_no=1,
        render_choices=render_choices,
        estimate=estimate,
    )
    return quote, render_choices, scope


def test_combined_reservation_retry_is_idempotent_and_claim_is_job_owned():
    uid = "quote-retry-user"
    session_id = "quote-retry-session"
    score_id = "quote-retry-score"
    get_or_create_credits(uid, "quote-retry@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    kwargs = {
        "session_id": session_id,
        "job_kind": "synthesis",
        "score_id": score_id,
        "score_version_no": 1,
        "quote_id": quote["quote_id"],
        "render_choices_hash": canonical_render_choices_hash(choices),
        "billing_components": ["vocal", "instrumental"],
        "vocal_estimated_credits": 2,
        "instrumental_estimated_credits": 1,
        "instrumental_charge_scope_value": scope,
    }

    first = reserve_credits(uid, "quote-job", 3, **kwargs)
    retry_after_lost_response = reserve_credits(uid, "quote-job", 3, **kwargs)
    reused_by_other_job = reserve_credits(uid, "other-job", 3, **kwargs)

    assert first.status == "reserved"
    assert retry_after_lost_response.status == "reservation_exists"
    assert reused_by_other_job.status == "infra_error"
    reservation = get_firestore_client().collection("credit_reservations").document("quote-job").get().to_dict()
    assert reservation["components"]["vocal"]["status"] == "pending"
    assert reservation["components"]["instrumental"]["status"] == "pending"


def test_combined_release_rolls_back_both_components_and_pending_claim():
    uid = "quote-release-user"
    session_id = "quote-release-session"
    score_id = "quote-release-score"
    get_or_create_credits(uid, "quote-release@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    reserve_credits(
        uid,
        "quote-release-job",
        3,
        session_id=session_id,
        job_kind="synthesis",
        score_id=score_id,
        score_version_no=1,
        quote_id=quote["quote_id"],
        render_choices_hash=canonical_render_choices_hash(choices),
        billing_components=["vocal", "instrumental"],
        vocal_estimated_credits=2,
        instrumental_estimated_credits=1,
        instrumental_charge_scope_value=scope,
    )
    assert release_credits(uid, "quote-release-job").status == "released"
    db = get_firestore_client()
    reservation = db.collection("credit_reservations").document("quote-release-job").get().to_dict()
    assert reservation["components"]["vocal"]["status"] == "released"
    assert reservation["components"]["instrumental"]["status"] == "released"
    charge_docs = list(db.collection("instrumental_generation_charges").stream())
    assert len(charge_docs) == 1
    assert charge_docs[0].to_dict()["status"] == "unpaid"
    quote_doc = db.collection("synthesis_quotes").document(quote["quote_id"]).get().to_dict()
    assert quote_doc["status"] == "expired"


def test_combined_settlement_reprices_actual_duration_and_publishes_midi_metadata():
    uid = "quote-settle-user"
    session_id = "quote-settle-session"
    score_id = "quote-settle-score"
    job_id = "quote-settle-job"
    get_or_create_credits(uid, "quote-settle@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    assert reserve_credits(
        uid,
        job_id,
        3,
        session_id=session_id,
        job_kind="synthesis",
        score_id=score_id,
        score_version_no=1,
        quote_id=quote["quote_id"],
        render_choices_hash=canonical_render_choices_hash(choices),
        billing_components=["vocal", "instrumental"],
        vocal_estimated_credits=2,
        instrumental_estimated_credits=1,
        instrumental_charge_scope_value=scope,
    ).status == "reserved"
    db = get_firestore_client()
    db.collection("jobs").document(job_id).set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    performance_midi = {
        "version": 1,
        "has_instrumental_parts": True,
        "instrumental_parts": [{"part_id": "P2", "eligible": True}],
        "original_midi_available": True,
        "expanded_midi_available": True,
    }

    result = settle_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        121.0,
        output_path="sessions/test/audio.mp3",
        audio_url="/sessions/quote-settle-session/audio?file=audio.mp3",
        performance_midi=performance_midi,
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 7
    reservation = db.collection("credit_reservations").document(job_id).get().to_dict()
    assert reservation["components"]["vocal"]["actualCredits"] == 5
    assert reservation["components"]["instrumental"]["actualCredits"] == 2
    charge_docs = list(db.collection("instrumental_generation_charges").stream())
    assert len(charge_docs) == 1
    assert charge_docs[0].to_dict()["status"] == "paid"
    job = db.collection("jobs").document(job_id).get().to_dict()
    assert job["creditBreakdown"]["actual"] == {
        "duration_seconds": 121.0,
        "vocal_part_credits": 5,
        "instrumental_credits": 2,
        "total_credits": 7,
    }
    assert job["creditBreakdown"]["estimated"] == {
        "vocal_part_credits": 2,
        "instrumental_credits": 1,
        "total_credits": 3,
    }
    assert job["performanceMidi"] == performance_midi


def test_later_vocal_only_job_does_not_rewrite_paid_instrumental_component():
    uid = "vocal-only-after-midi-user"
    session_id = "vocal-only-after-midi-session"
    score_id = "vocal-only-after-midi-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "vocal-only-after-midi@example.com")
    first_quote, first_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid,
        "combined-job",
        3,
        session_id=session_id,
        job_kind="synthesis",
        score_id=score_id,
        score_version_no=1,
        quote_id=first_quote["quote_id"],
        render_choices_hash=canonical_render_choices_hash(first_choices),
        billing_components=["vocal", "instrumental"],
        vocal_estimated_credits=2,
        instrumental_estimated_credits=1,
        instrumental_charge_scope_value=scope,
    ).status == "reserved"
    db.collection("jobs").document("combined-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    assert settle_credits_and_complete_job(
        uid,
        "combined-job",
        session_id,
        60.0,
        output_path="combined.mp3",
        audio_url="/combined.mp3",
    ).status == "completed_and_settled"
    paid_charge_before = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()

    second_choices = {
        **first_choices,
        "lyric_selection": {"id": "P1-L2", "number": "2", "name": "Verse 2"},
    }
    second_estimate = estimate_synthesis_credits(
        vocal_duration_seconds=60.0,
        vocal_part_id="P1",
        expand_repeats=True,
        has_instrumental_parts=True,
        instrumental_charge_required=False,
        instrumental_charge_scope=scope,
    ).to_dict()
    second_quote = create_synthesis_quote(
        user_id=uid,
        session_id=session_id,
        score_id=score_id,
        score_version_no=2,
        render_choices=second_choices,
        estimate=second_estimate,
    )
    assert reserve_credits(
        uid,
        "vocal-only-job",
        2,
        session_id=session_id,
        job_kind="synthesis",
        score_id=score_id,
        score_version_no=2,
        quote_id=second_quote["quote_id"],
        render_choices_hash=canonical_render_choices_hash(second_choices),
        billing_components=["vocal"],
        vocal_estimated_credits=2,
        instrumental_estimated_credits=0,
        instrumental_charge_scope_value=scope,
    ).status == "reserved"
    db.collection("jobs").document("vocal-only-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    assert settle_credits_and_complete_job(
        uid,
        "vocal-only-job",
        session_id,
        60.0,
        output_path="vocal-only.mp3",
        audio_url="/vocal-only.mp3",
    ).status == "completed_and_settled"

    paid_charge_after = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert paid_charge_after == paid_charge_before
    vocal_reservation = db.collection("credit_reservations").document("vocal-only-job").get().to_dict()
    assert set(vocal_reservation["components"]) == {"vocal"}


def _create_vocal_only_synthesis_quote(
    uid: str,
    session_id: str,
    score_id: str,
    *,
    score_version_no: int = 1,
    has_instrumental_parts: bool = True,
):
    """Quote a take whose uploaded score owes no instrumental fee."""
    scope = instrumental_charge_scope(uid, session_id, score_id)
    estimate = estimate_synthesis_credits(
        vocal_duration_seconds=60.0,
        vocal_part_id="P1",
        expand_repeats=True,
        has_instrumental_parts=has_instrumental_parts,
        instrumental_charge_required=False,
        instrumental_charge_scope=scope,
    ).to_dict()
    render_choices = {
        "voicebank": "test-voice",
        "language": "en",
        "part_id": "P1",
        "lyric_selection": {"id": "P1-L1", "number": "1", "name": "Verse 1"},
        "expand_repeats": True,
        "require_solfege_lyrics": False,
        "solfege_pronunciation_patch": False,
    }
    quote = create_synthesis_quote(
        user_id=uid,
        session_id=session_id,
        score_id=score_id,
        score_version_no=score_version_no,
        render_choices=render_choices,
        estimate=estimate,
    )
    return quote, render_choices, scope


def _reserve_kwargs(quote, choices, scope, *, components, vocal, instrumental, version=1):
    return {
        "session_id": quote["sessionId"],
        "job_kind": "synthesis",
        "score_id": quote["scoreId"],
        "score_version_no": version,
        "quote_id": quote["quote_id"],
        "render_choices_hash": canonical_render_choices_hash(choices),
        "billing_components": components,
        "vocal_estimated_credits": vocal,
        "instrumental_estimated_credits": instrumental,
        "instrumental_charge_scope_value": scope,
    }


def test_vocal_only_reservation_consumes_quote_without_touching_instrumental_scope():
    uid = "vocal-only-reserve-user"
    session_id = "vocal-only-reserve-session"
    score_id = "vocal-only-reserve-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "vocal-only-reserve@example.com")
    quote, choices, scope = _create_vocal_only_synthesis_quote(uid, session_id, score_id)

    result = reserve_credits(
        uid,
        "vocal-only-reserve-job",
        2,
        **_reserve_kwargs(
            quote, choices, scope, components=["vocal"], vocal=2, instrumental=0
        ),
    )

    assert result.status == "reserved"
    reservation = (
        db.collection("credit_reservations")
        .document("vocal-only-reserve-job")
        .get()
        .to_dict()
    )
    assert reservation["billingComponents"] == ["vocal"]
    assert set(reservation["components"]) == {"vocal"}
    assert reservation["components"]["vocal"]["status"] == "pending"
    # A vocal-only job performs no instrumental payment-state write at all.
    assert list(db.collection("instrumental_generation_charges").stream()) == []
    # It must still consume its own quote.
    quote_doc = db.collection("synthesis_quotes").document(quote["quote_id"]).get().to_dict()
    assert quote_doc["status"] == "reserved"
    assert quote_doc["reservedByJobId"] == "vocal-only-reserve-job"


def test_reservation_rejects_quote_owned_by_another_user_or_session():
    uid = "quote-owner-user"
    other_uid = "quote-owner-intruder"
    session_id = "quote-owner-session"
    score_id = "quote-owner-score"
    get_or_create_credits(uid, "quote-owner@example.com")
    get_or_create_credits(other_uid, "quote-owner-intruder@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    kwargs = _reserve_kwargs(
        quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
    )

    stolen = reserve_credits(other_uid, "stolen-job", 3, **kwargs)
    wrong_session = reserve_credits(
        uid, "wrong-session-job", 3, **{**kwargs, "session_id": "someone-elses-session"}
    )

    assert stolen.status == "infra_error"
    assert wrong_session.status == "infra_error"
    assert list(get_firestore_client().collection("credit_reservations").stream()) == []


def test_reservation_rejects_changed_render_choices_score_version_and_amount():
    uid = "quote-binding-user"
    session_id = "quote-binding-session"
    score_id = "quote-binding-score"
    get_or_create_credits(uid, "quote-binding@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    kwargs = _reserve_kwargs(
        quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
    )

    changed_voice = reserve_credits(
        uid,
        "changed-voice-job",
        3,
        **{
            **kwargs,
            "render_choices_hash": canonical_render_choices_hash(
                {**choices, "voicebank": "another-voice"}
            ),
        },
    )
    changed_repeats = reserve_credits(
        uid,
        "changed-repeats-job",
        3,
        **{
            **kwargs,
            "render_choices_hash": canonical_render_choices_hash(
                {**choices, "expand_repeats": False}
            ),
        },
    )
    changed_version = reserve_credits(
        uid, "changed-version-job", 3, **{**kwargs, "score_version_no": 2}
    )
    changed_total = reserve_credits(uid, "changed-total-job", 2, **kwargs)
    changed_components = reserve_credits(
        uid, "changed-components-job", 3, **{**kwargs, "billing_components": ["vocal"]}
    )

    for result in (
        changed_voice,
        changed_repeats,
        changed_version,
        changed_total,
        changed_components,
    ):
        assert result.status == "infra_error"
    assert list(get_firestore_client().collection("credit_reservations").stream()) == []


def test_reservation_rejects_quote_for_an_already_paid_instrumental_scope():
    uid = "stale-scope-user"
    session_id = "stale-scope-session"
    score_id = "stale-scope-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "stale-scope@example.com")
    first_quote, first_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid,
        "paid-job",
        3,
        **_reserve_kwargs(
            first_quote,
            first_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    ).status == "reserved"
    db.collection("jobs").document("paid-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    assert settle_credits_and_complete_job(
        uid, "paid-job", session_id, 60.0, output_path="paid.mp3", audio_url="/paid.mp3"
    ).status == "completed_and_settled"

    # A combined quote prepared before the scope was paid is now stale.
    stale_quote, stale_choices, _ = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    stale = reserve_credits(
        uid,
        "stale-combined-job",
        3,
        **_reserve_kwargs(
            stale_quote,
            stale_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    )

    assert stale.status == "infra_error"
    charge = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert charge["status"] == "paid"
    assert charge["jobId"] == "paid-job"


def test_two_concurrent_first_jobs_cannot_both_claim_the_instrumental_fee():
    uid = "concurrent-claim-user"
    session_id = "concurrent-claim-session"
    score_id = "concurrent-claim-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "concurrent-claim@example.com")
    first_quote, first_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    second_quote, second_choices, _ = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )

    first = reserve_credits(
        uid,
        "first-take",
        3,
        **_reserve_kwargs(
            first_quote,
            first_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    )
    # A second first-take for the same unpaid upload races for the same scope.
    second = reserve_credits(
        uid,
        "second-take",
        3,
        **_reserve_kwargs(
            second_quote,
            second_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    )

    assert first.status == "reserved"
    assert second.status == "infra_error"
    charge_docs = list(db.collection("instrumental_generation_charges").stream())
    assert len(charge_docs) == 1
    charge = charge_docs[0].to_dict()
    assert charge["status"] == "pending"
    assert charge["jobId"] == "first-take"
    assert db.collection("credit_reservations").document("second-take").get().exists is False


def test_lower_actual_duration_releases_the_unused_reservation():
    uid = "lower-actual-user"
    session_id = "lower-actual-session"
    score_id = "lower-actual-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "lower-actual@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    # Quote a 121 s score: 5 vocal + 2 instrumental = 7 reserved.
    db.collection("synthesis_quotes").document(quote["quote_id"]).update(
        {
            "vocalPartCredits": 5,
            "instrumentalCredits": 2,
            "totalEstimatedCredits": 7,
            "vocalDurationSeconds": 121.0,
        }
    )
    assert reserve_credits(
        uid,
        "lower-actual-job",
        7,
        **_reserve_kwargs(
            quote,
            choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=5,
            instrumental=2,
        ),
    ).status == "reserved"
    assert get_or_create_credits(uid, "lower-actual@example.com").reserved == 7
    db.collection("jobs").document("lower-actual-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )

    # The generated audio came in shorter than the score-derived estimate.
    result = settle_credits_and_complete_job(
        uid,
        "lower-actual-job",
        session_id,
        119.8,
        output_path="lower.mp3",
        audio_url="/lower.mp3",
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 5
    assert get_or_create_credits(uid, "lower-actual@example.com").reserved == 0
    job = db.collection("jobs").document("lower-actual-job").get().to_dict()
    assert job["creditBreakdown"]["estimated"]["total_credits"] == 7
    assert job["creditBreakdown"]["actual"] == {
        "duration_seconds": 119.8,
        "vocal_part_credits": 4,
        "instrumental_credits": 1,
        "total_credits": 5,
    }
    assert job["consumedCredits"] == 5


def test_higher_actual_duration_charges_the_larger_total():
    uid = "higher-actual-user"
    session_id = "higher-actual-session"
    score_id = "higher-actual-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "higher-actual@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    assert reserve_credits(
        uid,
        "higher-actual-job",
        3,
        **_reserve_kwargs(
            quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
        ),
    ).status == "reserved"
    db.collection("jobs").document("higher-actual-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )

    # Generated audio ran longer than the 60 s quote: 3 vocal + 1 instrumental.
    result = settle_credits_and_complete_job(
        uid,
        "higher-actual-job",
        session_id,
        70.0,
        output_path="higher.mp3",
        audio_url="/higher.mp3",
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 4
    job = db.collection("jobs").document("higher-actual-job").get().to_dict()
    assert job["creditBreakdown"]["actual"]["vocal_part_credits"] == 3
    assert job["creditBreakdown"]["actual"]["instrumental_credits"] == 1
    assert job["consumedCredits"] == 4
    ledger = db.collection("credit_ledger").document("settle_higher-actual-job").get().to_dict()
    assert ledger["amount"] == -4
    assert ledger["reservedDelta"] == -3


def test_settlement_tolerates_a_missing_generated_duration():
    uid = "zero-duration-user"
    session_id = "zero-duration-session"
    score_id = "zero-duration-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "zero-duration@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    assert reserve_credits(
        uid,
        "zero-duration-job",
        3,
        **_reserve_kwargs(
            quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
        ),
    ).status == "reserved"
    db.collection("jobs").document("zero-duration-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )

    # The shared pricing primitive rejects a non-positive duration; settlement
    # must still publish the job and charge nothing rather than raising.
    result = settle_credits_and_complete_job(
        uid,
        "zero-duration-job",
        session_id,
        0.0,
        output_path="zero.mp3",
        audio_url="/zero.mp3",
    )

    assert result.status == "completed_and_settled"
    assert result.actual_credits == 0
    job = db.collection("jobs").document("zero-duration-job").get().to_dict()
    assert job["consumedCredits"] == 0
    assert job["creditBreakdown"]["actual"]["total_credits"] == 0


def test_vocal_only_release_preserves_a_previously_paid_instrumental_scope():
    uid = "vocal-only-release-user"
    session_id = "vocal-only-release-session"
    score_id = "vocal-only-release-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "vocal-only-release@example.com")
    paid_quote, paid_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid,
        "paid-combined-job",
        3,
        **_reserve_kwargs(
            paid_quote,
            paid_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    ).status == "reserved"
    db.collection("jobs").document("paid-combined-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    assert settle_credits_and_complete_job(
        uid,
        "paid-combined-job",
        session_id,
        60.0,
        output_path="paid.mp3",
        audio_url="/paid.mp3",
    ).status == "completed_and_settled"
    paid_before = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()

    later_quote, later_choices, _ = _create_vocal_only_synthesis_quote(
        uid, session_id, score_id, score_version_no=2
    )
    assert reserve_credits(
        uid,
        "later-vocal-job",
        2,
        **_reserve_kwargs(
            later_quote,
            later_choices,
            scope,
            components=["vocal"],
            vocal=2,
            instrumental=0,
            version=2,
        ),
    ).status == "reserved"

    assert release_credits(uid, "later-vocal-job").status == "released"

    reservation = db.collection("credit_reservations").document("later-vocal-job").get().to_dict()
    assert set(reservation["components"]) == {"vocal"}
    assert reservation["components"]["vocal"]["status"] == "released"
    paid_after = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert paid_after == paid_before


def test_repeated_release_of_a_combined_job_is_idempotent():
    uid = "release-idempotent-user"
    session_id = "release-idempotent-session"
    score_id = "release-idempotent-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "release-idempotent@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    assert reserve_credits(
        uid,
        "release-idempotent-job",
        3,
        **_reserve_kwargs(
            quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
        ),
    ).status == "reserved"

    first = release_credits(uid, "release-idempotent-job")
    second = release_credits(uid, "release-idempotent-job")

    assert first.status == "released"
    assert second.status == "already_released"
    assert get_or_create_credits(uid, "release-idempotent@example.com").reserved == 0
    charge = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert charge["status"] == "unpaid"


def test_release_never_removes_a_pending_claim_owned_by_another_job():
    uid = "claim-owner-user"
    session_id = "claim-owner-session"
    score_id = "claim-owner-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "claim-owner@example.com")
    owner_quote, owner_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid,
        "claim-owner-job",
        3,
        **_reserve_kwargs(
            owner_quote,
            owner_choices,
            scope,
            components=["vocal", "instrumental"],
            vocal=2,
            instrumental=1,
        ),
    ).status == "reserved"
    # Forge a second combined reservation naming the same scope, as a buggy or
    # replayed caller might.
    db.collection("credit_reservations").document("intruder-job").set(
        {
            "userId": uid,
            "sessionId": session_id,
            "scoreId": score_id,
            "status": "pending",
            "estimatedCredits": 3,
            "reservedMonthlyCredits": 3,
            "billingComponents": ["vocal", "instrumental"],
            "components": {
                "vocal": {"status": "pending", "estimatedCredits": 2},
                "instrumental": {
                    "status": "pending",
                    "estimatedCredits": 1,
                    "chargeScope": scope,
                },
            },
        }
    )

    result = release_credits(uid, "intruder-job")

    assert result.status == "reconciliation_required"
    charge = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert charge["status"] == "pending"
    assert charge["jobId"] == "claim-owner-job"


def test_release_still_frees_credits_when_its_own_claim_is_already_gone():
    uid = "claim-missing-user"
    session_id = "claim-missing-session"
    score_id = "claim-missing-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "claim-missing@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    assert reserve_credits(
        uid,
        "claim-missing-job",
        3,
        **_reserve_kwargs(
            quote, choices, scope, components=["vocal", "instrumental"], vocal=2, instrumental=1
        ),
    ).status == "reserved"
    # Simulate the claim already resolved out of band. The reserved credits are
    # still this job's to release; stranding the balance would be worse.
    for doc in db.collection("instrumental_generation_charges").list_documents():
        doc.delete()

    result = release_credits(uid, "claim-missing-job")

    assert result.status == "released"
    assert get_or_create_credits(uid, "claim-missing@example.com").reserved == 0
    assert list(db.collection("instrumental_generation_charges").stream()) == []


def test_shutdown_release_of_a_vocal_only_job_never_writes_instrumental_state():
    """Cancellation and shutdown both land on release_credits.

    A vocal-only job must roll back its own vocal amount and leave an
    already-paid upload scope byte-for-byte intact, whoever paid it.
    """
    uid = "shutdown-vocal-only-user"
    session_id = "shutdown-vocal-only-session"
    score_id = "shutdown-vocal-only-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "shutdown-vocal-only@example.com")

    paid_quote, paid_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid, "earlier-paid-job", 3,
        **_reserve_kwargs(
            paid_quote, paid_choices, scope,
            components=["vocal", "instrumental"], vocal=2, instrumental=1,
        ),
    ).status == "reserved"
    db.collection("jobs").document("earlier-paid-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    assert settle_credits_and_complete_job(
        uid, "earlier-paid-job", session_id, 60.0,
        output_path="paid.mp3", audio_url="/paid.mp3",
    ).status == "completed_and_settled"
    paid_before = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()

    later_quote, later_choices, _ = _create_vocal_only_synthesis_quote(
        uid, session_id, score_id, score_version_no=2
    )
    assert reserve_credits(
        uid, "interrupted-job", 2,
        **_reserve_kwargs(
            later_quote, later_choices, scope,
            components=["vocal"], vocal=2, instrumental=0, version=2,
        ),
    ).status == "reserved"

    # Shutdown rollback for the interrupted take.
    assert release_credits(uid, "interrupted-job").status == "released"

    reservation = (
        db.collection("credit_reservations").document("interrupted-job").get().to_dict()
    )
    assert set(reservation["components"]) == {"vocal"}
    assert reservation["components"]["vocal"]["status"] == "released"
    paid_after = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert paid_after == paid_before
    assert get_or_create_credits(uid, "shutdown-vocal-only@example.com").reserved == 0


# ui_at_quote, ui_now, quoted_voice, conflicts, why
_UI_VOICEBANK_CASES = [
    ("A", "B", "A", True, "user switched the dropdown away from the quoted voice"),
    ("A", "B", "C", True, "switched to a voice the quote never priced"),
    ("PM31", "PM31", "LIEE", False, "accepted a suggested voice; UI untouched"),
    (None, "LIEE", "LIEE", False, "selected exactly the voice that was quoted"),
    (None, "B", "LIEE", True, "selected a different voice after quoting"),
    ("A", "A", "A", False, "nothing changed"),
    ("A", None, "A", False, "selection cleared back to recommended"),
    ("A", None, "LIEE", False, "cleared to recommended; quoted voice stands"),
    (None, None, "LIEE", False, "no selection at any point"),
    ("A", "A", "LIEE", False, "override still accepted on an unchanged selection"),
]


@pytest.mark.parametrize(
    ("ui_at_quote", "ui_now", "quoted_voice", "conflicts", "why"), _UI_VOICEBANK_CASES
)
def test_ui_voicebank_conflict_covers_every_combination(
    ui_at_quote, ui_now, quoted_voice, conflicts, why
):
    """Reject only when the dropdown moved AND disagrees with the quote.

    Comparing the quoted voice against the UI rejects an accepted suggestion;
    comparing only "did the UI change" rejects selecting the quoted voice itself.
    Both conditions are load-bearing.
    """
    quote = {
        "uiVoicebankAtQuote": ui_at_quote,
        "renderChoices": {"voicebank": quoted_voice},
    }

    assert ui_voicebank_conflicts_with_quote(quote, ui_now) is conflicts, why


def test_ui_voicebank_conflict_is_lenient_about_missing_data():
    """An absent quote or unknown quoted voice must not block a render."""
    assert ui_voicebank_conflicts_with_quote(None, "A") is False
    assert ui_voicebank_conflicts_with_quote({}, "A") is False
    assert (
        ui_voicebank_conflicts_with_quote(
            {"uiVoicebankAtQuote": None, "renderChoices": {}}, "A"
        )
        is False
    )
    # Empty strings behave like "no selection", not like a distinct voice.
    assert (
        ui_voicebank_conflicts_with_quote(
            {"uiVoicebankAtQuote": "", "renderChoices": {"voicebank": "A"}}, ""
        )
        is False
    )


def test_quote_records_the_ui_selection_it_was_presented_with():
    """Confirmation needs the selection as it was, not as it is now."""
    uid = "ui-at-quote-user"
    session_id = "ui-at-quote-session"
    score_id = "ui-at-quote-score"
    get_or_create_credits(uid, "ui-at-quote@example.com")
    estimate = estimate_synthesis_credits(
        vocal_duration_seconds=60.0,
        vocal_part_id="P1",
        expand_repeats=True,
        has_instrumental_parts=False,
        instrumental_charge_required=False,
        instrumental_charge_scope=None,
    ).to_dict()
    choices = {"voicebank": "Suggested", "language": "en", "part_id": "P1"}

    quote = create_synthesis_quote(
        user_id=uid,
        session_id=session_id,
        score_id=score_id,
        score_version_no=1,
        render_choices=choices,
        estimate=estimate,
        ui_voicebank_at_quote="UiSelected",
    )

    stored = get_synthesis_quote(quote["quote_id"])
    assert stored["uiVoicebankAtQuote"] == "UiSelected"
    # The recorded selection is not part of the priced choices, so the hash that
    # binds the render is unaffected by it.
    assert stored["renderChoicesHash"] == canonical_render_choices_hash(choices)
    # An accepted suggestion is not a conflict while the dropdown stays put.
    assert ui_voicebank_conflicts_with_quote(stored, "UiSelected") is False
    assert ui_voicebank_conflicts_with_quote(stored, "SomethingElse") is True
    context = active_synthesis_quote_context(
        quote["quote_id"], user_id=uid, session_id=session_id
    )
    assert context["ui_voicebank_at_quote"] == "UiSelected"


def test_active_quote_context_offers_only_a_still_confirmable_quote():
    """The confirmation turn reads the quote_id from here, so be strict.

    A real model cannot see the quote tool's result: it arrives in a
    message-only follow-up. This context is its only source for the id, and
    offering a stale one would bind synthesis to the wrong price.
    """
    uid = "quote-context-user"
    session_id = "quote-context-session"
    score_id = "quote-context-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "quote-context@example.com")
    quote, choices, scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    quote_id = quote["quote_id"]

    context = active_synthesis_quote_context(
        quote_id, user_id=uid, session_id=session_id
    )
    assert context is not None
    assert context["quote_id"] == quote_id
    assert context["total_estimated_credits"] == 3
    assert context["vocal_part_credits"] == 2
    assert context["instrumental_credits"] == 1
    assert "call synthesize once with this exact quote_id" in context["instruction"]
    # The model can only notice a change made after quoting -- such as a new voice
    # picked in the UI -- if it can see what the quote is actually bound to.
    assert context["bound_render_choices"] == choices
    assert context["bound_render_choices"]["voicebank"] == "test-voice"
    assert "call prepare_synthesis_quote for the new choices" in context["instruction"]

    # Another user's or session's quote is never offered.
    assert active_synthesis_quote_context(
        quote_id, user_id="someone-else", session_id=session_id
    ) is None
    assert active_synthesis_quote_context(
        quote_id, user_id=uid, session_id="other-session"
    ) is None
    assert active_synthesis_quote_context(
        None, user_id=uid, session_id=session_id
    ) is None
    assert active_synthesis_quote_context(
        "no-such-quote", user_id=uid, session_id=session_id
    ) is None

    # Once reserved by a job it is no longer awaiting confirmation.
    assert reserve_credits(
        uid, "context-job", 3,
        **_reserve_kwargs(
            quote, choices, scope,
            components=["vocal", "instrumental"], vocal=2, instrumental=1,
        ),
    ).status == "reserved"
    assert active_synthesis_quote_context(
        quote_id, user_id=uid, session_id=session_id
    ) is None


def test_active_quote_context_drops_an_expired_quote():
    """An expired quote must not be presented as confirmable."""
    uid = "quote-expiry-user"
    session_id = "quote-expiry-session"
    score_id = "quote-expiry-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "quote-expiry@example.com")
    quote, _choices, _scope = _create_combined_synthesis_quote(uid, session_id, score_id)
    db.collection("synthesis_quotes").document(quote["quote_id"]).update(
        {"expiresAt": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )

    assert active_synthesis_quote_context(
        quote["quote_id"], user_id=uid, session_id=session_id
    ) is None


def test_retry_after_a_failed_combined_job_can_claim_and_pay_the_scope():
    """A released scope must be claimable again, and pay from the retry's own take."""
    uid = "retry-claim-user"
    session_id = "retry-claim-session"
    score_id = "retry-claim-score"
    db = get_firestore_client()
    get_or_create_credits(uid, "retry-claim@example.com")

    first_quote, first_choices, scope = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid, "failed-job", 3,
        **_reserve_kwargs(
            first_quote, first_choices, scope,
            components=["vocal", "instrumental"], vocal=2, instrumental=1,
        ),
    ).status == "reserved"
    assert release_credits(uid, "failed-job").status == "released"
    released = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert released["status"] == "unpaid"

    # The retry prepares its own quote and claims the now-unpaid scope.
    retry_quote, retry_choices, _ = _create_combined_synthesis_quote(
        uid, session_id, score_id
    )
    assert reserve_credits(
        uid, "retry-job", 3,
        **_reserve_kwargs(
            retry_quote, retry_choices, scope,
            components=["vocal", "instrumental"], vocal=2, instrumental=1,
        ),
    ).status == "reserved"
    claimed = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert claimed["status"] == "pending"
    assert claimed["jobId"] == "retry-job"

    db.collection("jobs").document("retry-job").set(
        {"userId": uid, "sessionId": session_id, "status": "queued"}
    )
    result = settle_credits_and_complete_job(
        uid, "retry-job", session_id, 121.0,
        output_path="retry.mp3", audio_url="/retry.mp3",
    )

    # The retry's own actual duration sets the one-time instrumental charge.
    assert result.status == "completed_and_settled"
    assert result.actual_credits == 7
    paid = list(db.collection("instrumental_generation_charges").stream())[0].to_dict()
    assert paid["status"] == "paid"
    assert paid["jobId"] == "retry-job"
    assert paid["actualCredits"] == 2
    assert paid["actualDurationSeconds"] == 121.0


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


def test_release_credits_atomically_marks_job_terminal():
    uid = "test-user-terminal-release"
    job_id = "job-terminal-release"
    get_or_create_credits(uid, "terminal-release@example.com")
    db = get_firestore_client()
    db.collection("jobs").document(job_id).set(
        {
            "userId": uid,
            "sessionId": "session-terminal-release",
            "status": "running",
        }
    )
    reserve_credits(uid, job_id, 4)

    result = release_credits(
        uid,
        job_id,
        terminal_job_fields={
            "status": "cancelled",
            "step": "cancelled",
            "message": "Generation cancelled.",
            "progress": 1.0,
        },
    )

    assert result.status == "released"
    credits = get_or_create_credits(uid, "terminal-release@example.com")
    assert credits.reserved == 0
    reservation = (
        db.collection("credit_reservations").document(job_id).get().to_dict()
        or {}
    )
    assert reservation["status"] == "released"
    assert db.collection("credit_ledger").document(f"release_{job_id}").get().exists
    job = db.collection("jobs").document(job_id).get().to_dict() or {}
    assert job["status"] == "cancelled"
    assert job["step"] == "cancelled"
    assert job["progress"] == 1.0


def test_release_credits_rolls_back_if_terminal_job_update_fails():
    uid = "test-user-missing-terminal-job"
    job_id = "job-missing-terminal-job"
    get_or_create_credits(uid, "missing-terminal-job@example.com")
    db = get_firestore_client()
    reserve_credits(uid, job_id, 4)

    result = release_credits(
        uid,
        job_id,
        terminal_job_fields={
            "status": "cancelled",
            "step": "cancelled",
            "progress": 1.0,
        },
    )

    assert result.status == "infra_error"
    credits = get_or_create_credits(uid, "missing-terminal-job@example.com")
    assert credits.reserved == 4
    reservation = (
        db.collection("credit_reservations").document(job_id).get().to_dict()
        or {}
    )
    assert reservation["status"] == "pending"
    assert not db.collection("credit_ledger").document(f"release_{job_id}").get().exists


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
    reserve_credits(uid, job_id, 2, session_id=session_id, job_kind="synthesis",
                    score_id="score-A", score_version_no=3)
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
    assert ledger[0].to_dict()["scoreId"] == "score-A"
    assert ledger[0].to_dict()["scoreVersionNo"] == 3
    completed_at = job["completedAt"]

    retry_result = settle_credits_and_complete_job(
        uid,
        job_id,
        session_id,
        61.0,
        output_path="sessions/test/audio.mp3",
        audio_url="/sessions/session-11/audio?file=audio.mp3",
    )

    assert retry_result.status == "already_completed_and_settled"
    assert db.collection("jobs").document(job_id).get().to_dict()["completedAt"] == completed_at


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
