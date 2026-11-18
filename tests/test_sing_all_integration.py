"""Sing All parent records, holds, guard and receipt use one Firestore transaction."""

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.backend import main, sing_all_runner, sing_all_store, songs
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
        ("singAllJobs", f"{job_id}-second"),
    ):
        db.collection(collection).document(identifier).delete()
    for index in range(4):
        db.collection("credit_reservations").document(f"{job_id}-take-{index}").delete()
    db.collection("credit_reservations").document(f"{job_id}-second-take-0").delete()
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


def test_second_sing_it_cannot_create_a_job_or_reservation(parent_environment):
    db, uid, session_id, job_id = parent_environment

    def parent(identifier):
        return {
            "jobId": identifier, "uid": uid, "sessionId": session_id,
            "scoreId": "example-score", "status": "running", "stage": "reading",
            "revision": 0, "quote": {"total": 2, "scoreVersion": 1},
            "items": [{
                "itemId": "take:P1:lyrics", "type": "take", "rowId": "P1",
                "mode": "lyrics", "childJobId": f"{identifier}-take-0",
                "creditsReserved": 2, "creditsCharged": 0,
                "estimatedSeconds": 59, "unitSeconds": 30, "status": "queued",
            }],
        }

    assert sing_all_store.create_job_with_reservations(parent(job_id)).status == "reserved"
    second_id = f"{job_id}-second"
    result = sing_all_store.create_job_with_reservations(parent(second_id))
    assert (result.status, result.reason) == ("refused", "sing_all_already_running")
    assert not db.collection("singAllJobs").document(second_id).get().exists
    assert not db.collection("credit_reservations").document(
        f"{second_id}-take-0").get().exists
    assert db.collection("users").document(uid).get().to_dict()["activeSingAllJobId"] == job_id
    assert get_or_create_credits(uid, "").reserved == 2


def test_insufficient_credits_leave_no_parent_or_reservation(parent_environment):
    db, uid, session_id, job_id = parent_environment
    db.collection("users").document(uid).update({"credits.balance": 1})
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "reading",
        "revision": 0, "quote": {"total": 2, "scoreVersion": 1},
        "items": [{
            "itemId": "take:P1:lyrics", "type": "take", "rowId": "P1",
            "mode": "lyrics", "childJobId": f"{job_id}-take-0",
            "creditsReserved": 2, "creditsCharged": 0,
            "estimatedSeconds": 59, "unitSeconds": 30, "status": "queued",
        }],
    }
    result = sing_all_store.create_job_with_reservations(parent)
    assert result.status == "insufficient_balance"
    assert not db.collection("singAllJobs").document(job_id).get().exists
    assert not db.collection("credit_reservations").document(f"{job_id}-take-0").get().exists
    user = db.collection("users").document(uid).get().to_dict()
    assert not user.get("activeSingAllJobId")
    assert user["credits"]["reserved"] == 0
    assert db.collection("sessions").document(session_id).get().to_dict()["history"] == []


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


@pytest.mark.parametrize("stage,completed,late", [
    ("reading", 0, False), ("splitting", 0, False), ("solfege", 0, False),
    ("singing", 1, False), ("singing", 1, True),
])
def test_stop_winds_down_with_the_right_credit_balance(
    parent_environment, stage, completed, late,
):
    db, uid, session_id, job_id = parent_environment
    items = [{
        "itemId": f"take:row-{index}:lyrics", "type": "take", "rowId": f"row-{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(2)]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": stage,
        "revision": 0, "quote": {"total": 4, "scoreVersion": 1}, "items": items,
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    children = {}
    for index in range(completed):
        assert settle_credits(uid, items[index]["childJobId"], 59).status == "settled"
        items[index].update(status="done", creditsCharged=2)
    if late:
        items[1]["status"] = "running"
        children[items[1]["childJobId"]] = {"status": "running", "pastStopPoint": True}
    assert sing_all_store.apply(job_id, {"items": items, "cancelRequested": True},
                                expected_stage=stage, expected_revision=0)

    async def snapshot(*_args):
        return {"score_id": "example-score", "current_score": {"version": 1, "score": {}}}

    job_store = SimpleNamespace(
        get_job=lambda child_id: children.get(child_id),
        stop_job=lambda child_id: "too_late" if children[child_id].get("pastStopPoint")
        else children[child_id].update(status="cancelled") or "stopped",
    )
    runner = SingAllRunner(SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot), _job_store=job_store,
    ))
    asyncio.run(runner.reconcile(job_id))
    if late:
        assert sing_all_store.load(job_id)["status"] == "running"
        assert get_or_create_credits(uid, "").reserved == 2
        assert settle_credits(uid, items[1]["childJobId"], 59).status == "settled"
        children[items[1]["childJobId"]] = {"status": "completed", "creditsCharged": 2}
        asyncio.run(runner.reconcile(job_id))
    ended = sing_all_store.load(job_id)
    assert ended["status"] == ("completed" if late else "cancelled")
    assert ended["stage"] == "done"
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 2 * (2 if late else completed)
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


def test_sweep_recovers_a_lost_completion_notification(parent_environment):
    db, uid, session_id, job_id = parent_environment
    child_id = f"{job_id}-take-0"
    item = {
        "itemId": "take:P1:lyrics", "type": "take", "rowId": "P1", "mode": "lyrics",
        "childJobId": child_id, "creditsReserved": 2, "creditsCharged": 0,
        "estimatedSeconds": 59, "unitSeconds": 30, "status": "running",
    }
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "instrumentalsPrepared": True,
        "quote": {"total": 2, "scoreVersion": 1}, "items": [item],
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    assert settle_credits(uid, child_id, 59).status == "settled"

    async def snapshot(*_args):
        return {"score_id": "example-score", "current_score": {"version": 1, "score": {}}}

    runner = SingAllRunner(SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _job_store=SimpleNamespace(get_job=lambda _id: {
            "status": "completed", "creditsCharged": 2}),
    ))
    assert job_id in sing_all_store.running_jobs()
    asyncio.run(runner.reconcile(job_id, sweep=True))
    assert sing_all_store.load(job_id)["status"] == "completed"
    assert get_or_create_credits(uid, "").available_balance == before - 2
    assert get_or_create_credits(uid, "").reserved == 0
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


@pytest.mark.parametrize("stage,minutes,charged", [
    ("splitting", 31, 0), ("singing", 21, 2),
])
def test_sweep_fails_stuck_children_and_refunds_with_injected_time(
    parent_environment, stage, minutes, charged,
):
    db, uid, session_id, job_id = parent_environment
    now = datetime.now(timezone.utc)
    items = [{
        "itemId": f"take:row-{index}:lyrics", "type": "take", "rowId": f"row-{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(2)]
    child_id = items[1]["childJobId"]
    if stage == "splitting":
        child_id = f"{job_id}-split"
        items.insert(0, {
            "itemId": "split:P1", "type": "split", "sourcePartId": "P1",
            "childJobId": child_id, "creditsReserved": 0, "status": "running",
            "startedAt": now - timedelta(minutes=minutes),
        })
    else:
        items[0].update(status="done", creditsCharged=2)
        items[1].update(status="running", startedAt=now - timedelta(minutes=minutes))
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": stage,
        "revision": 0, "instrumentalsPrepared": True,
        "quote": {"total": 4, "scoreVersion": 1}, "items": items,
        **({"singingStartedAt": now - timedelta(minutes=minutes)}
           if stage == "singing" else {}),
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    if charged:
        assert settle_credits(uid, items[0]["childJobId"], 59).status == "settled"
    child = {"status": "running"}
    failed = []

    async def snapshot(*_args):
        return {"score_id": "example-score", "current_score": {"version": 1, "score": {}}}

    def fail_child(identifier, **_kwargs):
        failed.append(identifier)
        child["status"] = "failed"
        return True

    runner = SingAllRunner(SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _job_store=SimpleNamespace(
            get_job=lambda identifier: child if identifier == child_id else None,
            fail_job_if_active=fail_child,
        ),
    ))
    asyncio.run(runner.reconcile(job_id, sweep=True, now=now))
    ended = sing_all_store.load(job_id)
    assert failed == [child_id]
    assert ended["status"] == ("partial" if charged else "failed")
    assert ended["endReason"] == "stuck"
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before - charged
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


def test_stop_after_parent_has_ended_changes_no_credits(parent_environment):
    db, uid, session_id, job_id = parent_environment
    child_id = f"{job_id}-take-0"
    item = {
        "itemId": "take:P1:lyrics", "type": "take", "rowId": "P1", "mode": "lyrics",
        "childJobId": child_id, "creditsReserved": 2, "creditsCharged": 0,
        "estimatedSeconds": 59, "unitSeconds": 30, "status": "queued",
    }
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "quote": {"total": 2, "scoreVersion": 1}, "items": [item],
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    assert settle_credits(uid, child_id, 59).status == "settled"
    item.update(status="done", creditsCharged=2)
    assert sing_all_store.finish(job_id, {"items": [item], "status": "completed", "stage": "done"},
                                 expected_stage="singing", expected_revision=0)
    runner = SingAllRunner(SimpleNamespace())
    asyncio.run(runner.reconcile(job_id))
    assert sing_all_store.load(job_id)["status"] == "completed"
    assert get_or_create_credits(uid, "").available_balance == before - 2
    assert get_or_create_credits(uid, "").reserved == 0
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


def test_stop_records_a_charge_that_settles_while_the_parent_releases(parent_environment):
    db, uid, session_id, job_id = parent_environment
    items = [{
        "itemId": f"take:P{index}:lyrics", "type": "take", "rowId": f"P{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index in range(2)]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "quote": {"total": 4, "scoreVersion": 1}, "items": items,
        "cancelRequested": True,
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    assert settle_credits(uid, items[0]["childJobId"], 59).status == "settled"

    async def snapshot(*_args):
        return {"score_id": "example-score", "current_score": {"version": 1, "score": {}}}

    runner = SingAllRunner(SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _job_store=SimpleNamespace(get_job=lambda _id: None),
    ))
    asyncio.run(runner.reconcile(job_id))
    ended = sing_all_store.load(job_id)
    assert ended["status"] == "cancelled"
    assert [item["status"] for item in ended["items"]] == ["done", "stopped"]
    assert [item["creditsCharged"] for item in ended["items"]] == [2, 0]
    assert get_or_create_credits(uid, "").available_balance == before - 2
    assert get_or_create_credits(uid, "").reserved == 0
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


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


@pytest.mark.parametrize("split_status,reason", [
    ("failed", "split_failed"),
    ("completed", "quote_changed"),
])
def test_stubbed_split_failure_or_wrong_lane_count_refunds_parent(
    parent_environment, split_status, reason,
):
    db, uid, session_id, job_id = parent_environment
    rows = [{
        "rowId": f"P1:staff1:V1:slot{slot}", "sourcePartId": "P1",
        "partId": None, "split": True, "voice": "example-voice",
    } for slot in (1, 2)]
    items = [{
        "itemId": "split:P1", "type": "split", "sourcePartId": "P1",
        "childJobId": f"{job_id}-split", "status": "running", "creditsReserved": 0,
    }] + [{
        "itemId": f"take:{row['rowId']}:lyrics", "type": "take",
        "rowId": row["rowId"], "mode": "lyrics",
        "childJobId": f"{job_id}-take-{index}", "creditsReserved": 2,
        "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index, row in enumerate(rows)]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "splitting",
        "revision": 0, "quote": {"total": 4, "scoreVersion": 1},
        "items": items, "rows": rows, "choices": {"verse": 1},
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 2, "score": {
            "parts": [{"part_id": "P1"}, {"part_id": "P2"}],
            "voice_part_transforms": {"made-up-lane": {
                "source_part_index": 0, "source_voice_part_id": "V1",
                "derived_lane": {"slot": 1},
                "appended_part_ref": {"part_index": 1, "part_id": "P2"},
            }},
        }},
        "score_summary": {"parts": [
            {"part_index": 0, "part_id": "P1"},
            {"part_index": 1, "part_id": "P2", "part_name": "Made-up harmony"},
        ]},
    }

    async def get_snapshot(*_args):
        return snapshot

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: {
            "status": split_status, "reviewRequired": False,
        } if child_id == f"{job_id}-split" else None),
    )
    runner = SingAllRunner(orchestrator)
    asyncio.run(runner.reconcile(job_id))
    asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert finished["status"] == "failed"
    assert finished["endReason"] == reason
    assert finished["stage"] == "done"
    assert all(item["status"] == "failed" for item in finished["items"])
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before
    assert not db.collection("users").document(uid).get().to_dict().get("activeSingAllJobId")


def test_both_modes_make_two_takes_and_two_player_tracks(parent_environment):
    _db, uid, session_id, job_id = parent_environment
    lyrics = {"number": "1", "name": "Made-up words"}
    solfege = {"number": "9", "name": "Generated solfege"}
    items = [{
        "itemId": f"take:P1:{mode}", "type": "take", "rowId": "P1",
        "mode": mode, "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": 2, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index, mode in enumerate(("lyrics", "solfege"))]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "solfege",
        "revision": 0, "instrumentalsPrepared": True,
        "solfegePrepared": True, "solfegeSelections": {"P1": solfege},
        "quote": {"total": 4, "scoreVersion": 1}, "items": items,
        "rows": [{"rowId": "P1", "partId": "P1", "sourcePartId": "P1",
                  "voice": "example-voice"}],
        "choices": {"verse": 1, "mode": "both", "language": "en", "repeats": False},
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 1, "score": {
            "selected_verse_number": 1, "selected_lyric_selection": lyrics}},
        "score_summary": {"parts": [{
            "part_id": "P1", "lyric_selections": [lyrics]}]},
    }
    children = {}
    calls = []
    player = songs.empty_player(uid, "example-score")

    async def get_snapshot(*_args):
        return snapshot

    async def reparse_score(_session_id, **kwargs):
        snapshot["current_score"]["score"]["selected_lyric_selection"] = kwargs["lyric_selection"]

    async def start_take(_session_id, _score, args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        mode = "solfege" if args["require_solfege_lyrics"] else "lyrics"
        calls.append((mode, args["lyric_selection"], args.get("solfege_pronunciation_patch")))
        assert (await asyncio.to_thread(settle_credits, uid, child_id, 59)).status == "settled"
        child = {
            "status": "completed", "creditsCharged": 2, "jobKind": "synthesis",
            "scoreId": "example-score", "audioTrack": {
                "key": f"id:P1:{mode}", "mode": mode, "part_id": "P1",
                "part_index": 0, "label": "Made-up voice"},
        }
        assert songs.apply_take(player, child_id, child)
        children[child_id] = child
        return {"job_id": child_id}

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _reparse_score=reparse_score,
        _start_synthesis_job=start_take,
    )
    runner = SingAllRunner(orchestrator)
    asyncio.run(runner.reconcile(job_id))
    asyncio.run(runner.reconcile(job_id))
    assert sing_all_store.load(job_id)["status"] == "completed"
    assert calls == [("lyrics", lyrics, None), ("solfege", solfege, True)]
    assert [(track["mode"], track["key"]) for track in player["vocals"]] == [
        ("lyrics", "id:P1:lyrics"), ("solfege", "id:P1:solfege")]
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 4


def test_quoted_split_layout_makes_exact_rows_and_completes_takes(parent_environment):
    _db, uid, session_id, job_id = parent_environment
    rows = [{
        "rowId": f"P1:staff1:V1:slot{slot}", "sourcePartId": "P1",
        "partId": None, "name": f"Quoted line {slot}", "split": True,
        "include": True, "voice": f"example-voice-{slot}",
    } for slot in (1, 2)]
    items = [{
        "itemId": "split:P1", "type": "split", "sourcePartId": "P1",
        "childJobId": f"{job_id}-split", "status": "queued", "creditsReserved": 0,
    }] + [{
        "itemId": f"take:{row['rowId']}:lyrics", "type": "take",
        "rowId": row["rowId"], "mode": "lyrics",
        "childJobId": f"{job_id}-take-{index}", "creditsReserved": 2,
        "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued",
    } for index, row in enumerate(rows)]
    parent = {
        "jobId": job_id, "uid": uid, "sessionId": session_id,
        "scoreId": "example-score", "status": "running", "stage": "splitting",
        "revision": 0, "quote": {"total": 4, "scoreVersion": 1},
        "items": items, "rows": rows,
        "choices": {"verse": 1, "language": "en", "repeats": False},
        "judgment": {"layouts": {"P1|staff1": "separate"}},
        "instrumentalsPrepared": True,
    }
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    selection = {"number": "1", "name": "Made-up words"}
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 1, "score": {
            "selected_verse_number": 1,
            "selected_lyric_selection": selection,
            "parts": [{"part_id": "P1"}],
        }},
        "score_summary": {"parts": [{"part_index": 0, "part_id": "P1"}]},
    }
    children = {}
    split_requests = []
    take_requests = []

    async def get_snapshot(*_args):
        return snapshot

    async def start_split(_session_id, _score, _calls, **kwargs):
        context = kwargs["planning_context"]
        split_requests.append(context)
        assert context["lane_layouts"] == {"P1|staff1": "separate"}
        assert context["request"]["part_id"] == "P1"
        child_id = kwargs["parent"]["childJobId"]
        children[child_id] = {
            "status": "completed", "reviewRequired": True, "actionRequired": None}
        score = snapshot["current_score"]["score"]
        score["parts"] = [{"part_id": "P1"}, {"part_id": "P2"}, {"part_id": "P3"}]
        score["voice_part_transforms"] = {
            f"made-up-{slot}": {
                "source_part_index": 0, "source_voice_part_id": "V1",
                "derived_lane": {"slot": slot},
                "appended_part_ref": {"part_index": slot, "part_id": f"P{slot + 1}"},
            } for slot in (1, 2)
        }
        snapshot["current_score"]["version"] = 2
        snapshot["score_summary"]["parts"] = [
            {"part_index": 0, "part_id": "P1"},
        ] + [{
            "part_index": slot, "part_id": f"P{slot + 1}",
            "part_name": f"Made-up harmony {slot}", "lyric_selections": [selection],
        } for slot in (1, 2)]
        return {"job_id": child_id}

    async def start_take(_session_id, _score, args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        take_requests.append((args["part_id"], args["voicebank"]))
        assert (await asyncio.to_thread(settle_credits, uid, child_id, 59)).status == "settled"
        children[child_id] = {"status": "completed", "creditsCharged": 2}
        return {"job_id": child_id}

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _build_preprocess_workflow_planning_context=lambda *_args, **_kwargs: {},
        _start_preprocess_job=start_split,
        _score_has_review_pending=lambda _score: False,
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _start_synthesis_job=start_take,
    )
    runner = SingAllRunner(orchestrator)
    asyncio.run(runner.reconcile(job_id))
    asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert finished["status"] == "completed"
    assert [row["partId"] for row in finished["rows"]] == ["P2", "P3"]
    assert [row["name"] for row in finished["rows"]] == [
        "Made-up harmony 1", "Made-up harmony 2"]
    assert [row["voice"] for row in finished["rows"]] == [
        "example-voice-1", "example-voice-2"]
    assert take_requests == [("P2", "example-voice-1"), ("P3", "example-voice-2")]
    assert len(split_requests) == 1
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 4


def test_sing_it_through_four_player_tracks_never_calls_the_model(
    parent_environment, monkeypatch,
):
    db, uid, session_id, _fixture_job_id = parent_environment
    db.collection("users").document(uid).update({"billing.activePlanKey": "plus"})
    before = get_or_create_credits(uid, "").available_balance
    selection = {"number": "1", "name": "Made-up words"}
    rows = [{
        "rowId": f"P{index}", "sourcePartId": f"P{index}",
        "partId": f"P{index}", "partName": f"Made-up line {index}",
        "name": f"Made-up line {index}", "split": False,
        "hasWords": True, "include": True, "voice": "example-voice",
    } for index in range(4)]
    choices = {
        "language": "en", "solfegeOnly": False, "mode": "lyrics",
        "verse": "1", "repeats": False,
        "rows": [{"rowId": row["rowId"], "include": True,
                  "voice": row["voice"], "voicePicked": "recommended"}
                 for row in rows],
    }
    quote = sing_all_store.save_quote(db, {
        "uid": uid, "sessionId": session_id, "scoreId": "example-score",
        "scoreVersion": 1, "choices": choices,
        "choicesHash": main.choices_hash(choices, {}),
        "judgment": {"inputHash": "made-up-hash", "roles": {}, "sounds": {}},
        "style": {}, "rows": rows,
        "items": main.build_items(rows, mode="lyrics", duration_seconds=59,
                                  instrumental_charge_required=False),
        "total": 8,
    })
    snapshot = {
        "score_id": "example-score", "song": {"title": "Made-up song"},
        "current_score": {"version": 1, "score": {
            "selected_verse_number": "1",
            "selected_lyric_selection": selection,
            "parts": [{"part_id": row["partId"]} for row in rows],
        }},
        "score_summary": {"duration_seconds": 59, "parts": [{
            "part_id": row["partId"], "part_name": row["partName"],
            "lyric_selections": [selection],
        } for row in rows]},
    }
    children = {}
    player = songs.empty_player(uid, "example-score")
    enqueued = []

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Sing it and reconcile must use the saved judgment")

    async def get_snapshot(*_args):
        return snapshot

    async def set_score_summary(_session_id, summary):
        snapshot["score_summary"] = summary

    async def voices():
        return [{"id": "example-voice", "languages": ["en"]}]

    async def prepare_instrumentals(_session_id, **_kwargs):
        return snapshot["score_summary"], None

    async def start_take(_session_id, _score, args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        assert (await asyncio.to_thread(settle_credits, uid, child_id, 59)).status == "settled"
        part_id = args["part_id"]
        child = {
            "status": "completed", "creditsCharged": 2, "jobKind": "synthesis",
            "scoreId": "example-score", "audioTrack": {
                "key": f"id:{part_id}:lyrics", "mode": "lyrics", "part_id": part_id,
                "label": f"Made-up line {part_id}"},
        }
        assert songs.apply_take(player, child_id, child)
        children[child_id] = child
        return {"job_id": child_id}

    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot,
                                  set_score_summary=set_score_summary),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _settings=SimpleNamespace(preprocess_chooser_evidence_max_chars=1000),
        _get_voicebank_details=voices,
        _ensure_instrumental_midi_artifacts=prepare_instrumentals,
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _start_synthesis_job=start_take,
        _enqueue_sing_all_reconcile=lambda job_id: asyncio.sleep(
            0, result=enqueued.append(job_id)),
        _call_sing_all_judgment=no_model,
    )
    monkeypatch.setattr(main, "_get_user_id_or_401", lambda _request: asyncio.sleep(0, result=uid))
    monkeypatch.setattr(main, "_get_snapshot_or_404",
                        lambda *_args: asyncio.sleep(0, result=snapshot))
    monkeypatch.setattr(main, "quote_facts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "input_hash", lambda *_args, **_kwargs: "made-up-hash")
    monkeypatch.setattr(main, "materialize_rows", lambda *_args, **_kwargs: [dict(row) for row in rows])
    monkeypatch.setattr(main, "mark_voice_ranges", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "summary_with_judged_instruments",
                        lambda summary, *_args, **_kwargs: summary)
    monkeypatch.setattr(main, "score_has_instrumental_parts", lambda _summary: False)
    monkeypatch.setattr(main, "selected_score_duration", lambda *_args, **_kwargs: 59)
    monkeypatch.setattr(sing_all_runner, "quote_facts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(sing_all_runner, "materialize_rows",
                        lambda *_args, **_kwargs: [dict(row) for row in rows])
    monkeypatch.setattr(sing_all_runner, "mark_voice_ranges", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sing_all_runner, "summary_with_judged_instruments",
                        lambda summary, *_args, **_kwargs: summary)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=orchestrator._sessions, orchestrator=orchestrator,
        settings=main.app.state.settings)))
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sing-all/jobs")
    parent = asyncio.run(endpoint(main.SingAllJobRequest(quoteId=quote["quoteId"]), request))
    assert parent["songId"] == session_id
    assert parent["title"] == "Made-up song"
    assert parent["status"] == "running" and parent["stage"] == "reading"
    assert parent["choices"] == {key: choices[key] for key in
                                 ("language", "solfegeOnly", "mode", "verse", "repeats")}
    assert parent["counts"] == {"splits": {"done": 0, "total": 0},
                                 "takes": {"done": 0, "failed": 0, "total": 4}}
    assert len(parent["rows"]) == 4
    assert parent["credits"] == {"reserved": 8, "charged": 0, "refunded": 0}
    assert parent["receipt"] == []
    assert enqueued == [parent["jobId"]]
    assert get_or_create_credits(uid, "").reserved == 8
    asyncio.run(SingAllRunner(orchestrator).reconcile(parent["jobId"]))
    finished = sing_all_store.load(parent["jobId"])
    assert finished["status"] == "completed"
    assert len(player["vocals"]) == 4
    assert {track["partId"] for track in player["vocals"]} == {
        "P0", "P1", "P2", "P3"}
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 8
