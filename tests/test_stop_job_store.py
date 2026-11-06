"""Transactional stop boundaries for synthetic jobs."""

from datetime import datetime

import pytest
from google.cloud import firestore

from src.backend.job_store import JobStore, build_progress_payload
from src.backend.credits import (
    settle_credits_and_complete_job,
    settle_export_mix_credits_and_complete_job,
)


@pytest.fixture
def store(firestore_emulator, monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    db = firestore.Client(project="demo-project")
    monkeypatch.setattr("src.backend.job_store.get_firestore_client", lambda: db)
    monkeypatch.setattr("src.backend.credits.get_firestore_client", lambda: db)
    for doc in db.collection("stop_test_jobs").list_documents():
        doc.delete()
    store = JobStore(collection="stop_test_jobs")
    store._client = db
    yield store
    for doc in db.collection("stop_test_jobs").list_documents():
        doc.delete()


def _job(store, name, status="running", **extra):
    store._client.collection(store.collection).document(name).set(
        {"userId": "synthetic-owner", "sessionId": "synthetic-song", "status": status, **extra}
    )


@pytest.mark.parametrize("initial", ["queued", "running"])
def test_stop_active_job_sets_user_stopped_fields(store, initial):
    _job(store, "synthetic-take", initial)
    assert store.stop_job("synthetic-take") == "stopped"
    data = store.get_job("synthetic-take")
    assert data["status"] == "cancelled"
    assert data["cancelledBy"] == "user"
    assert data["step"] == "cancelled"
    assert data["progress"] == 1.0
    assert isinstance(data["stoppedAt"], datetime)
    assert build_progress_payload("synthetic-take", data)["status"] == "stopped"
    assert store.stop_job("synthetic-take") == "too_late"


def test_stopped_queued_take_cannot_be_claimed(store):
    _job(store, "synthetic-take", "queued")
    assert store.stop_job("synthetic-take") == "stopped"
    claim = store.claim_dispatched_job(
        "synthetic-take", worker_id="synthetic-worker", lease_seconds=30,
        max_attempts=3,
    )
    assert claim.outcome == "finished"
    assert store.get_job("synthetic-take")["status"] == "cancelled"


@pytest.mark.parametrize("initial", ["completed", "failed", "cancelled"])
def test_stop_finished_job_does_not_write(store, initial):
    _job(store, "synthetic-take", initial)
    before = store.get_job("synthetic-take")
    assert store.stop_job("synthetic-take") == "too_late"
    assert store.get_job("synthetic-take") == before


@pytest.mark.parametrize("stop_first", [True, False])
def test_stop_and_completion_point_have_one_winner(store, stop_first):
    _job(store, "synthetic-take")
    if stop_first:
        assert store.stop_job("synthetic-take") == "stopped"
        assert store.pass_stop_point("synthetic-take") is False
        assert store.get_job("synthetic-take")["status"] == "cancelled"
    else:
        assert store.pass_stop_point("synthetic-take") is True
        assert store.stop_job("synthetic-take") == "too_late"
        assert store.get_job("synthetic-take")["status"] == "running"
        assert store.get_job("synthetic-take")["pastStopPoint"] is True


def test_active_jobs_are_scoped_and_retry_clears_stop_point(store):
    _job(store, "synthetic-queued", "queued")
    _job(store, "synthetic-running", "running", leaseOwner="worker", pastStopPoint=True)
    _job(store, "synthetic-finished", "completed")
    store._client.collection(store.collection).document("other-song").set(
        {"userId": "synthetic-owner", "sessionId": "other-song", "status": "running"}
    )
    assert {name for name, _ in store.list_active_jobs(user_id="synthetic-owner", session_id="synthetic-song")} == {
        "synthetic-queued", "synthetic-running"
    }
    assert store.release_job_for_retry("synthetic-running", worker_id="worker", message="retry")
    assert "pastStopPoint" not in store.get_job("synthetic-running")


def test_shutdown_cancel_still_reports_error():
    assert build_progress_payload("synthetic-take", {"status": "cancelled"})["status"] == "error"


@pytest.mark.parametrize("kind", ["synthesis", "export_mix"])
def test_completion_safety_net_returns_cancelled_before_reservation_lookup(store, kind):
    job_ref = store._client.collection("jobs").document("synthetic-stop-safety")
    job_ref.set({"status": "cancelled", "cancelledBy": "user"})
    try:
        if kind == "synthesis":
            result = settle_credits_and_complete_job(
                "synthetic-owner", "synthetic-stop-safety", "synthetic-song", 10,
                output_path="synthetic.wav", audio_url="/synthetic.wav",
            )
        else:
            result = settle_export_mix_credits_and_complete_job(
                "synthetic-owner", "synthetic-stop-safety", "synthetic-song", 10,
                output_path="synthetic.wav", audio_url="/synthetic.wav",
            )
        assert result.status == "cancelled"
        assert job_ref.get().to_dict() == {"status": "cancelled", "cancelledBy": "user"}
    finally:
        job_ref.delete()
