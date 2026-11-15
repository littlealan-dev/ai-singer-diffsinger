"""Sing All's reservations: one per take plus one for the instrumentals, in one transaction."""

from typing import Any, Optional

import pytest

from src.backend.credits import (
    TRIAL_CREDIT_AMOUNT,
    ReservationItem,
    get_or_create_credits,
    instrumental_charge_scope,
    release_credits,
    reserve_credits_for_items,
    settle_credits,
    settle_instrumental_reservation,
)
from src.backend.firebase_app import get_firestore_client


@pytest.fixture(scope="module", autouse=True)
def firestore_emulator_env():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
        patch.setenv("GCLOUD_PROJECT", "demo-project")
        yield


@pytest.fixture(autouse=True)
def cleanup_firestore(firestore_emulator):
    db = get_firestore_client()
    for collection in [
        "users",
        "credit_reservations",
        "credit_ledger",
        "topup_packs",
        "instrumental_generation_charges",
        "guard_records",
    ]:
        for doc in db.collection(collection).list_documents():
            doc.delete()
    yield


class _Guard:
    """Stands in for the one-active check: reads one document, writes one document."""

    def __init__(self, refusal: Optional[str] = None) -> None:
        self.refusal = refusal
        self.read_calls = 0
        self.write_calls = 0

    def read(self, transaction: Any) -> Optional[str]:
        self.read_calls += 1
        get_firestore_client().collection("guard_records").document("active").get(transaction=transaction)
        return self.refusal

    def write(self, transaction: Any) -> None:
        self.write_calls += 1
        transaction.set(
            get_firestore_client().collection("guard_records").document("active"), {"parent": "parent-1"}
        )


def _takes(*credits: int) -> list[ReservationItem]:
    return [
        ReservationItem(job_id=f"take-{index}", estimated_credits=amount, job_kind="synthesis")
        for index, amount in enumerate(credits, start=1)
    ]


def _instrumentals(scope: str, credits: int = 1) -> ReservationItem:
    return ReservationItem(
        job_id="parent-1-instrumentals",
        estimated_credits=credits,
        billing_components=("instrumental",),
        job_kind="synthesis",
        instrumental_charge_scope_value=scope,
    )


def _reservation(job_id: str) -> Optional[dict]:
    snapshot = get_firestore_client().collection("credit_reservations").document(job_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def _reserve(uid: str, items: list[ReservationItem], guard: Optional[_Guard] = None):
    return reserve_credits_for_items(
        uid,
        items,
        guard=guard or _Guard(),
        session_id="session-1",
        score_id="score-1",
        score_version_no=1,
        parent_job_id="parent-1",
    )


def test_every_item_gets_its_own_reservation_in_one_transaction():
    uid = "multi-user-1"
    get_or_create_credits(uid, "multi1@example.test")
    guard = _Guard()

    result = _reserve(uid, _takes(2, 2, 2), guard)

    assert result.status == "reserved"
    assert result.estimated_credits == 6
    for job_id in ("take-1", "take-2", "take-3"):
        reservation = _reservation(job_id)
        assert reservation["status"] == "pending"
        assert reservation["estimatedCredits"] == 2
        assert reservation["parentJobId"] == "parent-1"
        assert reservation["components"] == {"vocal": {"status": "pending", "estimatedCredits": 2}}
    credits = get_or_create_credits(uid, "multi1@example.test")
    assert credits.reserved == 6
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 6
    assert guard.write_calls >= 1
    assert get_firestore_client().collection("guard_records").document("active").get().exists


def test_not_enough_credits_writes_nothing():
    uid = "multi-user-2"
    get_or_create_credits(uid, "multi2@example.test")
    guard = _Guard()

    result = _reserve(uid, _takes(TRIAL_CREDIT_AMOUNT, 1), guard)

    assert result.status == "insufficient_balance"
    assert _reservation("take-1") is None and _reservation("take-2") is None
    assert get_or_create_credits(uid, "multi2@example.test").reserved == 0
    assert guard.write_calls == 0


def test_a_guard_refusal_writes_nothing():
    uid = "multi-user-3"
    get_or_create_credits(uid, "multi3@example.test")

    result = _reserve(uid, _takes(1, 1), _Guard(refusal="sing_all_already_running"))

    assert result.status == "refused"
    assert result.reason == "sing_all_already_running"
    assert _reservation("take-1") is None
    assert get_or_create_credits(uid, "multi3@example.test").reserved == 0


def test_an_existing_reservation_writes_nothing():
    uid = "multi-user-4"
    get_or_create_credits(uid, "multi4@example.test")
    get_firestore_client().collection("credit_reservations").document("take-2").set({"userId": uid})

    result = _reserve(uid, _takes(1, 1))

    assert result.status == "infra_error"
    assert _reservation("take-1") is None
    assert get_or_create_credits(uid, "multi4@example.test").reserved == 0


def test_each_take_settles_or_is_released_on_its_own():
    uid = "multi-user-5"
    get_or_create_credits(uid, "multi5@example.test")
    _reserve(uid, _takes(2, 2, 2))

    assert settle_credits(uid, "take-1", 40.0).status == "settled"
    assert release_credits(uid, "take-2").status == "released"
    assert settle_credits(uid, "take-3", 40.0).status == "settled"

    credits = get_or_create_credits(uid, "multi5@example.test")
    assert credits.reserved == 0
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 4


def test_instrumentals_are_charged_once_when_settled():
    uid = "multi-user-6"
    get_or_create_credits(uid, "multi6@example.test")
    scope = instrumental_charge_scope(uid, "session-1", "score-1")
    _reserve(uid, [*_takes(2), _instrumentals(scope)])
    db = get_firestore_client()
    charges = list(db.collection("instrumental_generation_charges").stream())
    assert [doc.to_dict()["status"] for doc in charges] == ["pending"]
    assert charges[0].to_dict()["jobId"] == "parent-1-instrumentals"

    first = settle_instrumental_reservation(uid, "parent-1-instrumentals")
    second = settle_instrumental_reservation(uid, "parent-1-instrumentals")

    assert first.status == "settled" and first.actual_credits == 1
    assert second.status == "already_settled"
    assert _reservation("parent-1-instrumentals")["components"]["instrumental"]["status"] == "settled"
    assert [doc.to_dict()["status"] for doc in db.collection("instrumental_generation_charges").stream()] == ["paid"]
    credits = get_or_create_credits(uid, "multi6@example.test")
    assert credits.reserved == 2  # the take is still reserved
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT - 3


def test_instrumentals_released_when_no_take_was_made():
    uid = "multi-user-7"
    get_or_create_credits(uid, "multi7@example.test")
    scope = instrumental_charge_scope(uid, "session-1", "score-1")
    _reserve(uid, [*_takes(2), _instrumentals(scope)])

    assert release_credits(uid, "take-1").status == "released"
    assert release_credits(uid, "parent-1-instrumentals").status == "released"
    assert settle_instrumental_reservation(uid, "parent-1-instrumentals").status == "already_released"

    db = get_firestore_client()
    assert [doc.to_dict()["status"] for doc in db.collection("instrumental_generation_charges").stream()] == ["unpaid"]
    credits = get_or_create_credits(uid, "multi7@example.test")
    assert credits.reserved == 0
    assert credits.available_balance == TRIAL_CREDIT_AMOUNT


def test_instrumentals_already_paid_for_the_score_write_nothing():
    uid = "multi-user-8"
    get_or_create_credits(uid, "multi8@example.test")
    scope = instrumental_charge_scope(uid, "session-1", "score-1")
    _reserve(uid, [*_takes(1), _instrumentals(scope)])
    settle_instrumental_reservation(uid, "parent-1-instrumentals")
    for doc in get_firestore_client().collection("credit_reservations").list_documents():
        doc.delete()

    result = _reserve(uid, [*_takes(1), _instrumentals(scope)])

    assert result.status == "infra_error"
    assert _reservation("take-1") is None


def test_settle_instrumentals_refuses_a_take_reservation():
    uid = "multi-user-9"
    get_or_create_credits(uid, "multi9@example.test")
    _reserve(uid, _takes(2))

    assert settle_instrumental_reservation(uid, "take-1").status == "reconciliation_required"
    assert _reservation("take-1")["status"] == "pending"
