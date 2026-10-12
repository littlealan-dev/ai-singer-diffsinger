from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.backend.config import Settings
import src.backend.job_store as job_store_module
from src.backend.job_store import JobStore
import src.backend.preprocess_dispatch as dispatch_module
from src.backend.preprocess_dispatch import (
    WORKER_TOKEN_HEADER,
    PreprocessDispatchError,
    PreprocessDispatcher,
)


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data or {})


class _JobRef:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Collection:
    def __init__(self, ref):
        self.ref = ref

    def document(self, _job_id):
        return self.ref


class _Transaction:
    def __init__(self):
        self.writes = []

    def set(self, ref, payload, merge=False):
        self.writes.append(payload)


class _Client:
    def __init__(self, data):
        self.ref = _JobRef(data)
        self.transaction_value = _Transaction()

    def collection(self, _name):
        return _Collection(self.ref)

    def transaction(self):
        return self.transaction_value


def _store(monkeypatch, data):
    monkeypatch.setattr(job_store_module.firestore, "transactional", lambda func: func)
    store = JobStore()
    store._client = _Client(data)
    return store


def _writes(store):
    return store._client.transaction_value.writes


def test_claim_takes_a_queued_job_as_attempt_one(monkeypatch):
    store = _store(monkeypatch, {"status": "queued", "sessionId": "s1"})

    claim = store.claim_preprocess_job("job-1", worker_id="w1", lease_seconds=90, max_attempts=3)

    assert claim.outcome == "claimed"
    assert claim.attempt == 1
    assert claim.job["sessionId"] == "s1"
    write = _writes(store)[0]
    assert write["status"] == "running"
    assert write["dispatchAttempt"] == 1
    assert write["leaseOwner"] == "w1"
    assert write["leaseExpiresAt"] > datetime.now(timezone.utc) + timedelta(seconds=80)


def test_claim_waits_while_another_worker_holds_the_lease(monkeypatch):
    store = _store(
        monkeypatch,
        {
            "status": "running",
            "dispatchAttempt": 1,
            "leaseOwner": "w1",
            "leaseExpiresAt": datetime.now(timezone.utc) + timedelta(seconds=60),
        },
    )

    claim = store.claim_preprocess_job("job-1", worker_id="w2", lease_seconds=90, max_attempts=3)

    assert claim.outcome == "busy"
    assert _writes(store) == []


def test_claim_takes_over_a_lapsed_lease_as_the_next_attempt(monkeypatch):
    store = _store(
        monkeypatch,
        {
            "status": "running",
            "dispatchAttempt": 1,
            "leaseOwner": "w1",
            "leaseExpiresAt": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
    )

    claim = store.claim_preprocess_job("job-1", worker_id="w2", lease_seconds=90, max_attempts=3)

    assert claim.outcome == "claimed"
    assert claim.attempt == 2
    assert _writes(store)[0]["leaseOwner"] == "w2"


@pytest.mark.parametrize(
    ("data", "outcome"),
    [
        (None, "missing"),
        ({"status": "completed"}, "finished"),
        ({"status": "failed"}, "finished"),
        ({"status": "queued", "dispatchAttempt": 3}, "exhausted"),
    ],
)
def test_claim_does_not_run_missing_finished_or_exhausted_jobs(monkeypatch, data, outcome):
    store = _store(monkeypatch, data)

    claim = store.claim_preprocess_job("job-1", worker_id="w1", lease_seconds=90, max_attempts=3)

    assert claim.outcome == outcome
    assert _writes(store) == []


def test_renew_extends_only_the_owners_lease(monkeypatch):
    store = _store(monkeypatch, {"status": "running", "leaseOwner": "w1"})

    assert store.renew_preprocess_lease("job-1", worker_id="w2", lease_seconds=90) is False
    assert store.renew_preprocess_lease("job-1", worker_id="w1", lease_seconds=90) is True
    assert "leaseExpiresAt" in _writes(store)[0]


def test_release_requeues_an_owned_running_job(monkeypatch):
    store = _store(monkeypatch, {"status": "running", "leaseOwner": "w1"})

    assert store.release_preprocess_job_for_retry("job-1", worker_id="w2", message="m") is False
    assert store.release_preprocess_job_for_retry("job-1", worker_id="w1", message="m") is True
    write = _writes(store)[0]
    assert write["status"] == "queued"
    assert write["leaseOwner"] is job_store_module.firestore.DELETE_FIELD


def test_fail_job_if_active_leaves_a_finished_job_alone(monkeypatch):
    finished = _store(monkeypatch, {"status": "completed"})
    assert (
        finished.fail_job_if_active("job-1", message="m", error="e", job_kind="preprocess")
        is False
    )
    assert _writes(finished) == []

    running = _store(monkeypatch, {"status": "running"})
    assert (
        running.fail_job_if_active("job-1", message="m", error="e", job_kind="preprocess")
        is True
    )
    assert _writes(running)[0]["status"] == "failed"


def _settings(**overrides):
    base = Settings.from_env()
    values = {
        "preprocess_dispatch_mode": "http",
        "preprocess_worker_url": "http://worker.test",
        "preprocess_worker_token": "secret-token",
    }
    values.update(overrides)
    return replace(base, **values)


def _run_http_dispatch(responses, *, on_give_up=None):
    """Dispatch one job against a fake worker answering with `responses` in turn."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen), len(responses)) - 1]

    dispatcher = PreprocessDispatcher(
        _settings(),
        on_give_up=on_give_up,
        retry_backoff_seconds=[0.0, 0.0],
        transport=httpx.MockTransport(handler),
    )

    async def run():
        await dispatcher.dispatch("job-1")
        await asyncio.gather(*dispatcher._http_tasks)

    asyncio.run(run())
    return seen


def test_http_dispatch_posts_to_the_worker_with_the_shared_token():
    seen = _run_http_dispatch([httpx.Response(200, json={"status": "done"})])

    assert len(seen) == 1
    assert str(seen[0].url) == "http://worker.test/internal/preprocess-jobs/job-1"
    assert seen[0].headers[WORKER_TOKEN_HEADER] == "secret-token"


def test_http_dispatch_retries_busy_and_server_errors_like_cloud_tasks():
    seen = _run_http_dispatch(
        [httpx.Response(409), httpx.Response(503), httpx.Response(200)]
    )

    assert len(seen) == 3


def test_http_dispatch_gives_up_after_its_attempts_and_reports_it():
    gave_up = []

    async def on_give_up(job_id):
        gave_up.append(job_id)

    seen = _run_http_dispatch([httpx.Response(500)], on_give_up=on_give_up)

    assert len(seen) == 3
    assert gave_up == ["job-1"]


def test_http_dispatch_stops_at_once_on_a_rejection():
    gave_up = []

    async def on_give_up(job_id):
        gave_up.append(job_id)

    seen = _run_http_dispatch([httpx.Response(401)], on_give_up=on_give_up)

    assert len(seen) == 1
    assert gave_up == ["job-1"]


def test_cloud_tasks_dispatch_creates_one_named_oidc_task(monkeypatch):
    queue = "projects/p/locations/us-east4/queues/preprocess-jobs"
    settings = _settings(
        preprocess_dispatch_mode="cloud_tasks",
        preprocess_worker_url="https://worker.example.run.app",
        preprocess_worker_audience="https://worker.example.run.app",
        preprocess_worker_invoker_service_account="invoker@p.iam.gserviceaccount.com",
        preprocess_tasks_queue=queue,
    )
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return httpx.Response(200, json={"name": json["task"]["name"]})

    monkeypatch.setattr(dispatch_module, "_google_access_token", lambda: "access-token")
    monkeypatch.setattr(dispatch_module.httpx, "post", fake_post)

    asyncio.run(PreprocessDispatcher(settings).dispatch("job-1"))

    url, headers, body = calls[0]
    task = body["task"]
    assert url == f"https://cloudtasks.googleapis.com/v2/{queue}/tasks"
    assert headers["Authorization"] == "Bearer access-token"
    assert task["name"] == f"{queue}/tasks/preprocess-job-1"
    assert task["dispatchDeadline"] == "1800s"
    assert task["httpRequest"]["url"] == (
        "https://worker.example.run.app/internal/preprocess-jobs/job-1"
    )
    assert task["httpRequest"]["oidcToken"] == {
        "serviceAccountEmail": "invoker@p.iam.gserviceaccount.com",
        "audience": "https://worker.example.run.app",
    }
    assert json.loads(base64.b64decode(task["httpRequest"]["body"])) == {}


def test_cloud_tasks_dispatch_treats_an_existing_task_as_done(monkeypatch):
    settings = _settings(
        preprocess_dispatch_mode="cloud_tasks",
        preprocess_worker_invoker_service_account="invoker@p.iam.gserviceaccount.com",
        preprocess_tasks_queue="projects/p/locations/l/queues/q",
    )
    monkeypatch.setattr(dispatch_module, "_google_access_token", lambda: "t")
    monkeypatch.setattr(
        dispatch_module.httpx, "post", lambda *args, **kwargs: httpx.Response(409)
    )

    asyncio.run(PreprocessDispatcher(settings).dispatch("job-1"))


def test_cloud_tasks_dispatch_raises_when_the_queue_refuses(monkeypatch):
    settings = _settings(
        preprocess_dispatch_mode="cloud_tasks",
        preprocess_worker_invoker_service_account="invoker@p.iam.gserviceaccount.com",
        preprocess_tasks_queue="projects/p/locations/l/queues/q",
    )
    monkeypatch.setattr(dispatch_module, "_google_access_token", lambda: "t")
    monkeypatch.setattr(
        dispatch_module.httpx, "post", lambda *args, **kwargs: httpx.Response(403, text="denied")
    )

    with pytest.raises(PreprocessDispatchError):
        asyncio.run(PreprocessDispatcher(settings).dispatch("job-1"))
