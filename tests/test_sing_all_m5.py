"""M5 packaging, balance, and download contracts."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.backend import main, songs
from src.backend.sing_all import decide, progress
from tests.test_sing_all_reconcile import _job
from tests.test_sing_all_progress import _job as progress_job


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_last_take_enters_packaging_once_and_finishes_from_package():
    job = _job("singing")
    job["choices"]["mixes"] = "all"
    job["instrumentalsPrepared"] = True
    job["items"][0]["status"] = "done"
    job["items"][1]["status"] = "done"
    job["items"][2]["status"] = "failed"
    fields, actions = decide(job, {}, {"now": NOW, "scoreSummary": {}})
    assert fields["stage"] == "packaging"
    assert fields["package"]["mixes"] == {"done": 0, "total": 1}
    assert actions == [{"kind": "start_package"}]
    job.update(fields)
    assert decide(job, {}, {"now": NOW}) == ({}, [])
    job["package"].update(state="done", stats={"balance": {}})
    fields, actions = decide(job, {}, {"now": NOW})
    assert (fields["stage"], fields["status"]) == ("done", "partial")
    assert {item["kind"] for item in actions} >= {"set_opening_balance"}


def test_package_stale_and_failure_finish_by_takes():
    job = _job("packaging")
    job["items"][1]["status"] = "done"
    job["items"][2]["status"] = "failed"
    job["package"] = {"state": "mixing", "updatedAt": NOW - timedelta(minutes=11),
                      "mixes": {"done": 0, "total": 1}}
    assert decide(job, {}, {"now": NOW}) == ({}, [])
    fields, _ = decide(job, {}, {"now": NOW, "sweep": True})
    assert (fields["status"], fields["package"]["state"]) == ("partial", "failed")
    job["package"]["state"] = "failed"
    assert decide(job, {}, {"now": NOW})[0]["status"] == "partial"


def test_opening_balance_preserves_owner_edits():
    job = {"scoreId": "score", "items": [
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "a"},
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "b"}],
        "package": {"stats": {"balance": {"mode": "lyrics", "stats": {
            "vocals": [{"gain_db": 0, "dropped": False},
                       {"gain_db": -6, "dropped": False}],
            "instruments": {"gain_db": -12}}}}}}
    player = songs.empty_player("owner", "score")
    player["vocals"] = [{"sourceJobId": "a", "volume": 1, "muted": False, "solo": False},
                        {"sourceJobId": "b", "volume": 0.7, "muted": False, "solo": False}]
    assert songs.apply_sing_all_opening_balance(player, job)
    assert player["vocals"][0]["volume"] == 1
    assert player["vocals"][1]["volume"] == 0.7
    assert player["bus"]["levelDb"] == -12
    assert not songs.apply_sing_all_opening_balance(player, job)


def test_progress_package_redacts_private_fields():
    job = progress_job(stage="packaging")
    job["items"][0]["status"] = "done"
    job["package"] = {"state": "mixing", "mixes": {"done": 1, "total": 2},
                      "zipPath": "private", "files": ["private"], "error": "private"}
    assert progress(job, now=NOW)["package"] == {
        "state": "mixing", "mixes": {"done": 1, "total": 2}}


def test_download_rejects_local_mode_and_deleted_song(monkeypatch):
    job = progress_job(stage="done", status="completed")
    job.update(uid="owner", package={"state": "done", "zipPath":
               "singAll/owner/parent-a/Example.zip"})
    async def auth(_):
        return "owner"
    class Sessions:
        async def get_song_row(self, *_):
            return {}
    monkeypatch.setattr(main, "_get_user_id_or_401", auth)
    monkeypatch.setattr(main, "load_sing_all_job", lambda _: job)
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sing-all/jobs/{job_id}/download")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=Sessions(), settings=SimpleNamespace(backend_use_storage=False))))
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint("parent-a", request))
    assert (error.value.status_code, error.value.detail) == (409, {"code": "zip_unavailable"})
    async def deleted(*_):
        raise KeyError("deleted")
    request.app.state.sessions.get_song_row = deleted
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint("parent-a", request))
    assert (error.value.status_code, error.value.detail) == (404, {"code": "job_not_found"})
