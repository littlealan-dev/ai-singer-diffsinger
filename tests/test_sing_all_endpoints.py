"""Progress endpoints expose only the owner's made-up parent job."""

import asyncio
from types import SimpleNamespace

import pytest

from src.backend import main
from tests.test_sing_all_progress import _job


class _Snapshot:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value


class _Db:
    def __init__(self, active_id):
        self.active_id = active_id

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, user_id):
        assert user_id == "owner"
        return self

    def get(self):
        return _Snapshot({"activeSingAllJobId": self.active_id} if self.active_id else {})


def _route(monkeypatch, path, *, active_id="parent-a", job=None, user_id="owner"):
    async def auth(request):
        return user_id

    monkeypatch.setattr(main, "_get_user_id_or_401", auth)
    monkeypatch.setattr(main, "get_firestore_client", lambda: _Db(active_id))
    monkeypatch.setattr(main, "load_sing_all_job", lambda job_id: job if job_id == "parent-a" else None)
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == path)
    return endpoint, SimpleNamespace()


def test_active_without_pointer_or_with_stale_pointer_is_null(monkeypatch):
    endpoint, request = _route(monkeypatch, "/sing-all/active", active_id=None)
    assert asyncio.run(endpoint(request)) == {"job": None}
    endpoint, request = _route(monkeypatch, "/sing-all/active", job=None)
    assert asyncio.run(endpoint(request)) == {"job": None}


@pytest.mark.parametrize("stage", ["reading", "splitting", "solfege", "singing"])
def test_active_from_any_song_at_each_running_stage(monkeypatch, stage):
    job = _job(stage=stage)
    job["uid"] = "owner"
    endpoint, request = _route(monkeypatch, "/sing-all/active", job=job)
    result = asyncio.run(endpoint(request))["job"]
    assert result["jobId"] == "parent-a"
    assert result["songId"] == "song-a"
    assert result["stage"] == stage
    assert result["status"] == "running"


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "cancelled"])
def test_active_ignores_ended_parent(monkeypatch, status):
    job = _job(stage="done", status=status)
    job["uid"] = "owner"
    endpoint, request = _route(monkeypatch, "/sing-all/active", job=job)
    assert asyncio.run(endpoint(request)) == {"job": None}


@pytest.mark.parametrize("status", ["running", "completed", "partial", "failed", "cancelled"])
def test_owner_can_read_any_job_status(monkeypatch, status):
    job = _job(stage="reading" if status == "running" else "done", status=status)
    job["uid"] = "owner"
    endpoint, request = _route(monkeypatch, "/sing-all/jobs/{job_id}", job=job)
    result = asyncio.run(endpoint("parent-a", request))
    assert result["jobId"] == "parent-a"
    assert result["status"] == status


@pytest.mark.parametrize("user_id,job_id", [("other", "parent-a"), ("owner", "missing")])
def test_job_is_hidden_from_nonowner_and_missing_id(monkeypatch, user_id, job_id):
    job = _job()
    job["uid"] = "owner"
    endpoint, request = _route(monkeypatch, "/sing-all/jobs/{job_id}",
                               job=job, user_id=user_id)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(job_id, request))
    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "job_not_found"}
