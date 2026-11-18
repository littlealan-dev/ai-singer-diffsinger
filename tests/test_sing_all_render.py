"""Slot fairness and bounded render-job execution."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.backend import sing_all_render, sing_all_runner, sing_all_store
from src.backend.firebase_app import get_firestore_client


@pytest.fixture
def render_db(firestore_emulator, monkeypatch):
    monkeypatch.setenv("GCLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    db = get_firestore_client()
    db.collection("singAllRender").document("slots").delete()
    ids = [f"render-test-{uuid.uuid4().hex}" for _ in range(3)]
    for job_id in ids:
        db.collection("singAllJobs").document(job_id).set({
            "jobId": job_id, "status": "running", "stage": "singing",
            "render": {"state": "none"},
        })
    yield db, ids
    db.collection("singAllRender").document("slots").delete()
    for job_id in ids:
        db.collection("singAllJobs").document(job_id).delete()


def test_one_slot_queues_second_parent_then_starts_it(render_db):
    db, (first, second, third) = render_db
    assert sing_all_render.enter_line(first, 1, db=db) == "start"
    assert sing_all_render.enter_line(second, 1, db=db) == "waiting"
    assert sing_all_render.enter_line(third, 1, db=db) == "waiting"
    assert db.collection("singAllJobs").document(second).get().to_dict()["render"]["placeInLine"] == 1
    assert db.collection("singAllJobs").document(third).get().to_dict()["render"]["placeInLine"] == 2
    assert sing_all_render.leave_slot(first, retry=False, max_slots=1, db=db) == [second]
    slots = db.collection("singAllRender").document("slots").get().to_dict()
    assert slots["running"] == [second]
    assert slots["waiting"][0]["jobId"] == third
    assert db.collection("singAllJobs").document(third).get().to_dict()["render"]["placeInLine"] == 1
    assert db.collection("singAllJobs").document(first).get().to_dict()["render"]["state"] == "ended"


def test_stopped_waiter_leaves_line(render_db):
    db, (first, second, third) = render_db
    sing_all_render.enter_line(first, 1, db=db)
    sing_all_render.enter_line(second, 1, db=db)
    sing_all_render.enter_line(third, 1, db=db)
    assert sing_all_render.remove_waiting(second, db=db)
    assert db.collection("singAllJobs").document(third).get().to_dict()["render"]["placeInLine"] == 1


def test_incomplete_first_execution_waits_behind_older_parent(render_db):
    db, (first, second, _) = render_db
    sing_all_render.enter_line(first, 1, db=db)
    sing_all_render.enter_line(second, 1, db=db)
    assert sing_all_render.leave_slot(first, retry=True, max_slots=1, db=db) == [second]
    first_render = db.collection("singAllJobs").document(first).get().to_dict()["render"]
    assert first_render["state"] == "waiting"
    assert first_render["placeInLine"] == 1
    assert sing_all_render.leave_slot(second, retry=False, max_slots=1, db=db) == [first]
    assert db.collection("singAllJobs").document(first).get().to_dict()["render"]["attempt"] == 2


def test_launch_uses_one_jobs_api_execution_with_parent_override(monkeypatch):
    calls = []
    monkeypatch.setattr(sing_all_render, "reserve_launch", lambda job_id: True)
    monkeypatch.setattr(sing_all_render, "record_execution",
                        lambda job_id, operation: calls.append((job_id, operation)))
    monkeypatch.setattr("google.auth.default", lambda **kwargs: (object(), "project"))

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"name": "projects/project/locations/region/operations/op"}

    class Session:
        def __init__(self, credentials):
            pass

        def post(self, url, *, json, timeout):
            calls.append((url, json, timeout))
            return Response()

    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", Session)
    settings = SimpleNamespace(sing_all_render_job="projects/project/locations/region/jobs/render")
    asyncio.run(sing_all_runner.start_render_execution("parent", settings))
    assert calls == [
        ("https://run.googleapis.com/v2/projects/project/locations/region/jobs/render:run",
         {"overrides": {"containerOverrides": [{"env": [
             {"name": "SING_ALL_JOB_ID", "value": "parent"}]}]}}, 30),
        ("parent", "projects/project/locations/region/operations/op"),
    ]


def test_slot_promotion_launches_waiter_once_with_stubbed_jobs_api(render_db, monkeypatch):
    db, (first, second, _) = render_db
    calls = []
    monkeypatch.setattr("google.auth.default", lambda **kwargs: (object(), "project"))

    class Response:
        def __init__(self, operation):
            self.operation = operation

        def raise_for_status(self):
            pass

        def json(self):
            return {"name": self.operation}

    class Session:
        def __init__(self, credentials):
            pass

        def post(self, url, *, json, timeout):
            job_id = json["overrides"]["containerOverrides"][0]["env"][0]["value"]
            calls.append(job_id)
            return Response(f"projects/project/locations/region/operations/{job_id}")

    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", Session)
    settings = SimpleNamespace(sing_all_render_job="projects/project/locations/region/jobs/render")
    assert sing_all_render.enter_line(first, 1, db=db) == "start"
    asyncio.run(sing_all_runner.start_render_execution(first, settings))
    assert sing_all_render.enter_line(second, 1, db=db) == "waiting"
    assert db.collection("singAllJobs").document(second).get().to_dict()["render"]["placeInLine"] == 1
    promoted = sing_all_render.leave_slot(first, retry=False, max_slots=1, db=db)
    assert promoted == [second]
    asyncio.run(sing_all_runner.start_render_execution(promoted[0], settings))
    assert calls == [first, second]


def _fake_render(monkeypatch, *, stop_after_first=False):
    parent_id = f"parent-{uuid.uuid4().hex}"
    parent = {"jobId": parent_id, "uid": "test", "sessionId": "song",
              "status": "running", "stage": "singing", "revision": 0,
              "render": {"state": "running", "attempt": 1},
              "items": [{"type": "take", "itemId": f"take-{i}",
                         "childJobId": f"child-{i}", "status": "queued"} for i in range(6)]}
    children = {}
    active = 0
    peak = 0
    started = []
    left = []

    def load(_job_id):
        return parent

    def get_job(child_id):
        return children.get(child_id)

    async def start_take(self, job, action, snapshot, *, defer_dispatch=False):
        assert defer_dispatch
        child_id = next(item["childJobId"] for item in job["items"]
                        if item["itemId"] == action["itemId"])
        children[child_id] = {"status": "queued"}

    async def reconcile(self, _job_id):
        for item in parent["items"]:
            if children.get(item["childJobId"], {}).get("status") == "completed":
                item["status"] = "done"

    def claim(child_id, **kwargs):
        return SimpleNamespace(outcome="claimed", job=children[child_id], attempt=1)

    async def run_child(child_id, job, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(child_id)
        await asyncio.sleep(0.005)
        if not (parent["render"]["attempt"] == 1 and child_id == "child-5"):
            children[child_id]["status"] = "completed"
        active -= 1
        if stop_after_first and len(started) == 4:
            parent["cancelRequested"] = True

    def leave(_job_id, **kwargs):
        left.append(kwargs["retry"])
        return []

    async def enqueue(_job_id):
        pass

    monkeypatch.setattr(sing_all_store, "load", load)
    monkeypatch.setattr("src.backend.sing_all_runner.SingAllRunner._start_take", start_take)
    monkeypatch.setattr("src.backend.sing_all_runner.SingAllRunner.reconcile", reconcile)
    monkeypatch.setattr(sing_all_render, "leave_slot", leave)
    orchestrator = SimpleNamespace(
        _job_store=SimpleNamespace(get_job=get_job, claim_dispatched_job=claim),
        _sessions=SimpleNamespace(get_snapshot=lambda *args: asyncio.sleep(0, result={})),
        _synthesis_dispatcher=SimpleNamespace(route=SimpleNamespace(
            lease_seconds=90, max_dispatch_attempts=3)),
        _settings=SimpleNamespace(max_render_slots=1),
        run_dispatched_synthesis_job=run_child,
        _enqueue_sing_all_reconcile=enqueue,
    )
    return parent, orchestrator, started, left, lambda: peak


def test_six_takes_run_four_at_once_and_second_execution_skips_completed(monkeypatch):
    parent, orchestrator, started, left, peak = _fake_render(monkeypatch)
    asyncio.run(sing_all_render.run_parent(parent["jobId"], orchestrator,
                                           concurrency=4, launch_next=lambda _: None))
    assert peak() == 4
    assert started == [f"child-{i}" for i in range(6)]
    assert left == [True]
    parent["render"]["attempt"] = 2
    asyncio.run(sing_all_render.run_parent(parent["jobId"], orchestrator,
                                           concurrency=4, launch_next=lambda _: None))
    assert started == [f"child-{i}" for i in range(6)] + ["child-5"]
    assert left == [True, False]


def test_stop_between_batches_ends_run(monkeypatch):
    parent, orchestrator, started, left, _ = _fake_render(monkeypatch, stop_after_first=True)
    asyncio.run(sing_all_render.run_parent(parent["jobId"], orchestrator,
                                           concurrency=4, launch_next=lambda _: None))
    assert started == [f"child-{i}" for i in range(4)]
    assert left == [False]
