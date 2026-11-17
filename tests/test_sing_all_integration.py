"""Sing All parent records, holds, guard and receipt use one Firestore transaction."""

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from src.backend import sing_all_store
from src.backend.credits import (
    get_instrumental_charge_state, get_or_create_credits, instrumental_charge_scope,
    release_credits, settle_credits,
)
from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all_runner import SingAllRunner


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
    db.collection("credit_reservations").document(f"{job_id}-instrumentals").delete()
    scope = instrumental_charge_scope(uid, session_id, "example-score")
    db.collection("instrumental_generation_charges").document(
        hashlib.sha256(scope.encode("utf-8")).hexdigest()).delete()


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


def test_failed_split_releases_every_unsung_take_and_clears_guard(parent_environment):
    db, uid, session_id, job_id = parent_environment
    items = [{
        "itemId": f"take:row-{index}:lyrics", "type": "take", "rowId": f"row-{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(2)]
    parent = {"jobId": job_id, "uid": uid, "sessionId": session_id,
              "scoreId": "example-score", "status": "running", "stage": "splitting",
              "revision": 0, "quote": {"total": 4, "scoreVersion": 1}, "items": items}
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    for item in items:
        assert release_credits(uid, item["childJobId"]).status == "released"
        item.update(status="failed", reason="split_failed")
    assert sing_all_store.finish(
        job_id, {"status": "failed", "stage": "done", "endReason": "split_failed",
                 "items": items}, expected_stage="splitting", expected_revision=0)
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")
    history = db.collection("sessions").document(session_id).get().to_dict()["history"]
    assert len(history) == 2


def test_concurrent_reconcile_runs_four_stubbed_takes_once(parent_environment):
    db, uid, session_id, job_id = parent_environment
    rows = [{"rowId": f"P{index}", "partId": f"P{index}",
             "sourcePartId": f"P{index}", "voice": "example-voice"}
            for index in range(4)]
    items = [{
        "itemId": f"take:P{index}:lyrics", "type": "take", "rowId": f"P{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(4)]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "instrumentalsPrepared": True,
        "quote": {"total": 8, "scoreVersion": 1}, "items": items, "rows": rows,
        "choices": {"verse": 1, "language": "en", "repeats": False},
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    selection = {"number": "1", "name": "Made-up line"}
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 1, "score": {
            "selected_verse_number": 1, "selected_lyric_selection": selection}},
        "score_summary": {"parts": [
            {"part_id": row["partId"], "lyric_selections": [selection]} for row in rows]},
    }
    children = {}
    started = []

    async def get_snapshot(*_args):
        return snapshot

    async def start_take(_session_id, _score, _args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        started.append(child_id)
        await asyncio.sleep(0)
        result = await asyncio.to_thread(settle_credits, uid, child_id, 59)
        assert result.status == "settled"
        children[child_id] = {"status": "completed", "creditsCharged": 2}
        return {"job_id": child_id}

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _start_synthesis_job=start_take,
    )
    runner = SingAllRunner(orchestrator)

    async def run_twice():
        await asyncio.gather(runner.reconcile(job_id), runner.reconcile(job_id))
        await runner.reconcile(job_id)

    asyncio.run(run_twice())
    finished = sing_all_store.load(job_id)
    assert finished["status"] == "completed"
    assert finished["stage"] == "done"
    assert [item["status"] for item in finished["items"]] == ["done"] * 4
    assert len(started) == len(set(started)) == 4
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 8
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


@pytest.mark.parametrize("second_succeeds", [True, False])
def test_instrumentals_settle_on_first_success_or_refund_if_all_fail(
    parent_environment, second_succeeds,
):
    db, uid, session_id, job_id = parent_environment
    scope = instrumental_charge_scope(uid, session_id, "example-score")
    selection = {"number": "1", "name": "Made-up line"}
    rows = [{"rowId": f"P{index}", "partId": f"P{index}",
             "sourcePartId": f"P{index}", "voice": "example-voice"}
            for index in range(3)]
    items = [{
        "itemId": f"take:P{index}:lyrics", "type": "take", "rowId": f"P{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(3)]
    items.append({
        "itemId": "instrumental", "type": "instrumental",
        "reservationId": f"{job_id}-instrumentals", "creditsReserved": 1,
        "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 120, "status": "queued",
    })
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "instrumentalsPrepared": True,
        "quote": {"total": 7, "scoreVersion": 1}, "items": items, "rows": rows,
        "choices": {"verse": 1, "language": "en", "repeats": False},
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(
        parent, instrumental_scope=scope).status == "reserved"
    assert get_instrumental_charge_state(scope) == "pending"
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 1, "score": {
            "selected_verse_number": 1, "selected_lyric_selection": selection}},
        "score_summary": {"parts": [
            {"part_id": row["partId"], "lyric_selections": [selection]} for row in rows]},
    }
    children = {}
    started = []

    async def get_snapshot(*_args):
        return snapshot

    async def start_take(_session_id, _score, _args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        index = int(child_id.rsplit("-", 1)[1])
        started.append(child_id)
        if index == 2:
            # The charge follows the first successful take, before later takes start.
            assert get_instrumental_charge_state(scope) == (
                "paid" if second_succeeds else "pending")
        if index == 1 and second_succeeds:
            assert (await asyncio.to_thread(settle_credits, uid, child_id, 59)).status == "settled"
            children[child_id] = {"status": "completed", "creditsCharged": 2}
        else:
            assert (await asyncio.to_thread(release_credits, uid, child_id)).status == "released"
            children[child_id] = {"status": "failed", "error": "stub_worker_failed"}
        return {"job_id": child_id}

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _start_synthesis_job=start_take,
    )
    runner = SingAllRunner(orchestrator)
    asyncio.run(runner.reconcile(job_id))
    asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert finished["status"] == ("partial" if second_succeeds else "failed")
    assert finished["stage"] == "done"
    assert len(started) == len(set(started)) == 3
    assert get_instrumental_charge_state(scope) == ("paid" if second_succeeds else "unpaid")
    assert finished["items"][-1]["status"] == ("done" if second_succeeds else "failed")
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - (3 if second_succeeds else 0)
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")
