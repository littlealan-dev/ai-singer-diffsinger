"""The parent runner orders credit releases and durable transitions."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from src.backend import sing_all_runner


def _run_job(monkeypatch, job, children):
    record = deepcopy(job)
    events = []

    def load(_):
        return deepcopy(record)

    def apply(_, fields, *, expected_stage, expected_revision):
        assert record["status"] == "running"
        assert (record["stage"], record["revision"]) == (expected_stage, expected_revision)
        events.append(("apply", fields.get("status"), fields.get("stage")))
        record.update(deepcopy(fields))
        record["revision"] += 1
        return True

    monkeypatch.setattr(sing_all_runner.sing_all_store, "load", load)
    monkeypatch.setattr(sing_all_runner.sing_all_store, "apply", apply)
    def finish(job_id, fields, *, expected_stage, expected_revision):
        assert (record["stage"], record["revision"]) == (expected_stage, expected_revision)
        events.append(("finish", fields["status"], fields["stage"]))
        record.update(deepcopy(fields))
        record["revision"] += 1
        events.extend([("clear", job_id), ("history", job_id)])
        return True
    monkeypatch.setattr(sing_all_runner.sing_all_store, "finish", finish)
    monkeypatch.setattr(sing_all_runner.sing_all_store, "clear_active",
                        lambda uid, job_id: events.append(("clear", job_id)))
    monkeypatch.setattr(sing_all_runner, "release_credits",
                        lambda uid, job_id: (events.append(("release", job_id))
                                             or SimpleNamespace(status="released")))
    async def snapshot(*args):
        return {"current_score": {"version": 3, "score": {}}, "score_id": "score-a"}
    async def history(*args, **kwargs):
        events.append(("history", kwargs.get("sing_all_job_id")))
    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot, append_history=history),
        _job_store=SimpleNamespace(get_job=lambda job_id: children.get(job_id)),
    )
    runner = sing_all_runner.SingAllRunner(orchestrator)
    asyncio.run(runner.reconcile(job["jobId"]))
    asyncio.run(runner.reconcile(job["jobId"]))
    return record, events


def _job(items, **extra):
    return {
        "jobId": "parent-a", "uid": "owner", "sessionId": "song-a",
        "scoreId": "score-a", "status": "running", "stage": "singing",
        "revision": 0, "instrumentalsPrepared": True, "items": items,
        **extra,
    }


def test_cancel_releases_every_hold_before_terminal_state_and_writes_one_history(monkeypatch):
    job = _job([
        {"itemId": "take:a:lyrics", "type": "take", "mode": "lyrics",
         "childJobId": "take-a", "status": "queued", "creditsReserved": 2},
        {"itemId": "instrumental", "type": "instrumental",
         "reservationId": "parent-a-instrumentals", "status": "queued",
         "creditsReserved": 1},
    ], cancelRequested=True)
    record, events = _run_job(monkeypatch, job, {})
    assert record["status"] == "cancelled"
    assert events[:3] == [
        ("release", "take-a"), ("release", "parent-a-instrumentals"),
        ("finish", "cancelled", "done"),
    ]
    assert events.count(("history", "parent-a")) == 1
    assert events.count(("clear", "parent-a")) == 1


def test_completed_child_is_counted_once_then_parent_finishes(monkeypatch):
    job = _job([{"itemId": "take:a:lyrics", "type": "take", "mode": "lyrics",
                "childJobId": "take-a", "status": "running", "creditsReserved": 2,
                "creditsCharged": 0}])
    record, events = _run_job(monkeypatch, job, {
        "take-a": {"status": "completed", "creditsCharged": 2}})
    assert record["status"] == "completed"
    assert record["items"][0]["creditsCharged"] == 2
    assert events.count(("history", "parent-a")) == 1
    assert not any(event[0] == "release" for event in events)


def test_completed_split_maps_the_visible_derived_lane_to_quoted_row():
    async def snapshot(*args):
        return {}
    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _job_store=SimpleNamespace(get_job=lambda job_id: {
            "status": "completed", "reviewRequired": True, "actionRequired": None}),
    )
    runner = sing_all_runner.SingAllRunner(orchestrator)
    job = _job([{"itemId": "split:P1", "type": "split", "sourcePartId": "P1",
                "childJobId": "split-a", "status": "running"}])
    job["rows"] = [{"rowId": "P1:staff1:V1:slot1", "sourcePartId": "P1",
                    "split": True, "voice": "voice_a"}]
    state = {"current_score": {"score": {
        "parts": [{"part_id": "P1"}, {"part_id": "P2"}],
        "voice_part_transforms": {"a": {
            "source_part_index": 0, "source_voice_part_id": "V1",
            "derived_lane": {"slot": 1},
            "appended_part_ref": {"part_index": 1, "part_id": "P2"},
        }},
    }}, "score_summary": {"parts": [
        {"part_index": 0, "part_id": "P1"},
        {"part_index": 1, "part_id": "P2", "part_name": "Made-up harmony"},
    ]}}
    children = asyncio.run(runner._children(job, state))
    assert children["split-a"]["parts"] == [
        {"laneKey": "P1:staff1:V1:slot1", "partId": "P2", "name": "Made-up harmony"}]
    assert children["split-a"]["reviewRequired"] is False


def test_concurrent_reconcile_starts_one_solfege_transform(monkeypatch):
    record = _job([{
        "itemId": "take:a:solfege", "type": "take", "rowId": "a",
        "mode": "solfege", "childJobId": "take-a", "status": "queued",
        "creditsReserved": 2,
    }], stage="solfege")
    record["rows"] = [{"rowId": "a", "partId": "P1", "voice": "example-voice"}]
    record["choices"] = {"verse": 1}
    transforms = []

    def load(_job_id):
        return deepcopy(record)

    def apply(_job_id, fields, *, expected_stage, expected_revision):
        if (record["stage"] != expected_stage
                or record["revision"] != expected_revision):
            return False
        record.update(deepcopy(fields))
        record["revision"] += 1
        return True

    monkeypatch.setattr(sing_all_runner.sing_all_store, "load", load)
    monkeypatch.setattr(sing_all_runner.sing_all_store, "apply", apply)

    async def snapshot(*_args):
        return {"score_id": "score-a", "current_score": {"version": 1, "score": {}}}

    runner = sing_all_runner.SingAllRunner(SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _job_store=SimpleNamespace(get_job=lambda _job_id: None),
    ))

    async def add_solfege(job, _snapshot):
        transforms.append(job["revision"])
        await asyncio.sleep(0.01)
        assert apply(job["jobId"], {"solfegePrepared": True},
                     expected_stage="solfege", expected_revision=job["revision"])

    async def start_take(_job, _action, _snapshot):
        return None

    runner._add_solfege = add_solfege
    runner._start_take = start_take

    async def run_concurrently():
        await asyncio.gather(runner.reconcile(record["jobId"]),
                             runner.reconcile(record["jobId"]))

    asyncio.run(run_concurrently())
    assert len(transforms) == 1
    assert record["solfegePrepared"] is True
    assert record["stage"] == "singing"
