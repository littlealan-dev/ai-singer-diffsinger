"""Sing All parent records, holds, guard and receipt use one Firestore transaction."""

import uuid

import pytest

from src.backend import sing_all_store
from src.backend.credits import get_or_create_credits, settle_credits
from src.backend.firebase_app import get_firestore_client


@pytest.fixture
def parent_environment(firestore_emulator, monkeypatch):
    monkeypatch.setenv("GCLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    db = get_firestore_client()
    suffix = uuid.uuid4().hex
    uid, session_id, job_id = f"example-user-{suffix}", f"example-song-{suffix}", f"example-job-{suffix}"
    get_or_create_credits(uid, "")
    db.collection("sessions").document(session_id).set({"userId": uid, "history": []})
    yield db, uid, session_id, job_id
    for collection, identifier in (
        ("singAllJobs", job_id), ("sessions", session_id), ("users", uid),
    ):
        db.collection(collection).document(identifier).delete()
    for index in range(4):
        db.collection("credit_reservations").document(f"{job_id}-take-{index}").delete()


def test_four_takes_reserve_together_and_finish_with_one_receipt(parent_environment):
    db, uid, session_id, job_id = parent_environment
    items = [{
        "itemId": f"take:row-{index}:lyrics", "type": "take", "rowId": f"row-{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(4)]
    parent = {"jobId": job_id, "uid": uid, "sessionId": session_id,
              "scoreId": "example-score", "status": "running", "stage": "singing",
              "revision": 0, "quote": {"total": 8, "scoreVersion": 1}, "items": items}
    result = sing_all_store.create_job_with_reservations(parent)
    assert result.status == "reserved"
    user = db.collection("users").document(uid).get().to_dict()
    assert user["activeSingAllJobId"] == job_id
    assert get_or_create_credits(uid, "").reserved == 8
    history = db.collection("sessions").document(session_id).get().to_dict()["history"]
    assert len(history) == 1 and history[0]["singAllJobId"] == job_id

    for item in items:
        reservation = db.collection("credit_reservations").document(item["childJobId"]).get().to_dict()
        assert reservation["status"] == "pending"
        assert reservation["parentJobId"] == job_id
        assert settle_credits(uid, item["childJobId"], 59).status == "settled"
        item.update(status="done", creditsCharged=2)
    assert sing_all_store.finish(job_id, {"status": "completed", "stage": "done", "items": items},
                                 expected_stage="singing", expected_revision=0)
    assert not sing_all_store.finish(job_id, {"status": "completed", "stage": "done", "items": items},
                                     expected_stage="singing", expected_revision=0)
    assert get_or_create_credits(uid, "").reserved == 0
    user = db.collection("users").document(uid).get().to_dict()
    assert not user.get("activeSingAllJobId")
    history = db.collection("sessions").document(session_id).get().to_dict()["history"]
    assert len(history) == 2
    assert all(entry["singAllJobId"] == job_id for entry in history)
