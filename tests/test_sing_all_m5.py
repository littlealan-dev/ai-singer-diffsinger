"""M5 packaging, balance, and download contracts."""

import asyncio
from copy import deepcopy
import io
import json
import socket
import subprocess
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backend import main, sing_all_package, songs, storage_client
from src.backend.sing_all import (
    decide, progress, quoted_instruments_sound, summary_with_judged_instruments,
)
from tests.conftest import _stop_emulator
from tests.test_sing_all_reconcile import _job
from tests.test_sing_all_progress import _job as progress_job
from tests.test_sing_all_quote import _route as quote_route, _row
from tests.test_score_job_history import workspace  # noqa: F401 - parametrized store fixture


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def storage_emulator(monkeypatch, tmp_path):
    """Start only the storage emulator on its configured port and stop our process."""
    host = "127.0.0.1"
    port = 9199
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex((host, port)) == 0:
            pytest.fail("Storage emulator port is already in use.", pytrace=False)
    config = tmp_path / "firebase.json"
    config.write_text(json.dumps({
        "storage": {"rules": str(Path(__file__).resolve().parents[1] / "storage.rules")},
        "emulators": {"storage": {"host": host, "port": port},
                      "hub": {"host": host, "port": 4401},
                      "ui": {"enabled": False}, "singleProjectMode": True},
    }))
    log = (tmp_path / "storage-emulator.log").open("w+")
    process = subprocess.Popen(
        ["npx", "firebase", "emulators:start", "--only", "storage",
         "--project", "demo-project", "--config", str(config)],
        cwd=Path(__file__).resolve().parents[1], stdout=log,
        stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex((host, port)) == 0:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Storage emulator did not start.", pytrace=False)
        if process.poll() is not None:
            pytest.fail("Storage emulator exited during startup.", pytrace=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
        monkeypatch.setenv("STORAGE_EMULATOR_HOST", f"http://{host}:{port}")
        monkeypatch.setattr(storage_client, "_storage_client", None)
        yield f"http://{host}:{port}"
    finally:
        _stop_emulator(process)
        log.close()
        storage_client._storage_client = None


@pytest.mark.parametrize("mixes", ["invalid", 1, [], None])
def test_quote_rejects_invalid_mix_choice(monkeypatch, mixes):
    endpoint, request, _ = quote_route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint(main.SingAllQuoteRequest(
            songId="song-a", choices={"mixes": mixes}), request))
    assert (error.value.status_code, error.value.detail) == (422, {"code": "invalid_mixes"})


@pytest.mark.parametrize("mode,expected", [
    ("lyrics", {"all": 5, "full": 1}),
    ("both", {"all": 10, "full": 2}),
])
def test_quote_counts_ticked_rows_and_keeps_price(monkeypatch, mode, expected):
    rows = [{**_row(), "rowId": f"line-{index}", "partId": f"P{index}",
             "sourcePartId": f"P{index}", "name": f"Line {index}"}
            for index in range(4)]
    endpoint, request, snapshot = quote_route(
        monkeypatch, user={"credits": {"balance": 40}}, rows=rows)
    snapshot["score_summary"]["performance_midi"] = {"instrumental_parts": [
        {"eligible": True, "raw_part_id": "piano", "program_source": "score",
         "playback_preset": {"bank": 0, "program": 0}}]}
    choices = {"mode": mode, "rows": [{"rowId": row["rowId"], "include": True}
                                  for row in rows]}
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices=choices), request))
    assert result["options"]["mixes"] == "all"
    assert result["options"]["mixFiles"] == expected
    assert result["total"] == sum(line["credits"] for line in result["lines"]
                                  if line["kind"] == "singing")
    unticked = deepcopy(choices)
    unticked["rows"][-1]["include"] = False
    fewer = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices=unticked), request))
    assert fewer["options"]["mixFiles"] == {
        "all": expected["all"] - (2 if mode == "both" else 1),
        "full": expected["full"]}
    no_mixes = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={**choices, "mixes": "none"}), request))
    assert no_mixes["options"]["mixes"] == "none"
    assert no_mixes["options"]["mixFiles"] == expected
    assert no_mixes["lines"] == result["lines"]
    assert (no_mixes["total"], no_mixes["balance"]) == (
        result["total"], result["balance"])
    snapshot["score_summary"].pop("performance_midi")
    no_sound = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices=choices), request))
    assert no_sound["options"]["mixes"] == "full"
    assert no_sound["options"]["mixFiles"] == {
        "all": expected["full"], "full": expected["full"]}


def test_quote_uses_actual_wordless_take_modes(monkeypatch):
    rows = [{**_row(), "rowId": f"line-{index}", "partId": f"P{index}",
             "sourcePartId": f"P{index}", "hasWords": index < 3}
            for index in range(4)]
    endpoint, request, snapshot = quote_route(
        monkeypatch, user={"credits": {"balance": 40}}, rows=rows)
    snapshot["score_summary"]["performance_midi"] = {"instrumental_parts": [
        {"eligible": True, "raw_part_id": "piano", "program_source": "score",
         "playback_preset": {"bank": 0, "program": 0}}]}
    selected = [{"rowId": row["rowId"], "include": True} for row in rows]
    lyrics = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={"mode": "lyrics", "rows": selected}), request))
    both = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={"mode": "both", "rows": selected}), request))
    assert lyrics["options"]["mixFiles"] == {"all": 6, "full": 2}
    assert both["options"]["mixFiles"] == {"all": 9, "full": 2}


def test_quote_sound_choice_agrees_with_package_track_sound():
    summary = {"parts": [{"part_id": "P2", "instruments": [{
        "score_instrument_id": "P2-I1", "instrumental_candidate": True,
        "is_percussion": False, "playback_preset": None,
        "eligible_for_instrumental_midi": False}]}],
        "instrument_program_resolution": {
            "unresolved_score_instrument_ids": ["P2-I1"],
            "instrumental_score_instrument_ids": [],
            "unresolved_reasons": {"P2-I1": "undeclared"}}}
    sound = {"bank": 0, "kind": "melodic", "program": 0,
             "evidence": ["keyboard staff"]}
    amended = summary_with_judged_instruments(
        summary, {"roles": {"P2": "instrument"}, "sounds": {"P2-I1": sound}})
    assert quoted_instruments_sound(amended)
    saved = deepcopy(amended)
    saved["performance_midi"] = {"instrumental_parts": [{
        "eligible": True, "raw_part_id": "P2", "program_source": "llm_inferred",
        "playback_preset": amended["parts"][0]["instruments"][0]["playback_preset"]}]}
    assert any(value is not None for value in sing_all_package.track_sounds(saved))


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


def test_old_job_gets_a_zip_without_mix_files():
    job = _job("singing")
    job["instrumentalsPrepared"] = True
    job["items"][0]["status"] = "done"
    job["items"][1]["status"] = "done"
    job["items"][2]["status"] = "done"
    fields, actions = decide(job, {}, {"now": NOW, "scoreSummary": {}})
    assert fields["stage"] == "packaging"
    assert fields["package"]["mixes"] == {"done": 0, "total": 0}
    assert actions == [{"kind": "start_package"}]


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


def test_no_take_done_finishes_without_package():
    job = _job("singing")
    job["choices"]["mixes"] = "full"
    job["instrumentalsPrepared"] = True
    for item in job["items"]:
        item["status"] = "failed"
    fields, actions = decide(job, {}, {"now": NOW})
    assert (fields["status"], fields["stage"]) == ("failed", "done")
    assert "package" not in fields
    assert not any(action["kind"] == "start_package" for action in actions)


def test_opening_balance_preserves_owner_edits():
    job = {"scoreId": "score", "items": [
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "a"},
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "b"},
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "c"},
        {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "d"}],
        "package": {"stats": {"balance": {"mode": "lyrics", "stats": {
            "vocals": [{"gain_db": 0, "dropped": False},
                       {"gain_db": -6, "dropped": False},
                       {"gain_db": -12, "dropped": False},
                       {"gain_db": -3, "dropped": False}],
            "instruments": {"gain_db": -9}}}}}}
    player = songs.empty_player("owner", "score")
    player["vocals"] = [{"sourceJobId": "a", "volume": 1, "muted": False, "solo": False},
                        {"sourceJobId": "b", "volume": 0.7, "muted": False, "solo": False},
                        {"sourceJobId": "c", "volume": 1, "muted": False, "solo": False},
                        {"sourceJobId": "d", "volume": 1, "muted": True, "solo": False}]
    assert songs.apply_sing_all_opening_balance(player, job)
    assert player["vocals"][0]["volume"] == 1
    assert player["vocals"][1]["volume"] == 0.7
    assert player["vocals"][2]["volume"] == pytest.approx(10 ** (-12 / 20))
    assert player["vocals"][3]["volume"] == 1
    assert player["vocals"][3]["muted"] is True
    assert player["bus"]["levelDb"] == -9
    assert not songs.apply_sing_all_opening_balance(player, job)


def test_player_store_persists_opening_balance_for_workspace(workspace):
    store, _, _ = workspace

    async def scenario():
        session_id = (await store.create_session("owner")).id
        source = store.session_dir(session_id) / "score.xml"
        source.write_text("<score-partwise/>")
        await store.commit_uploaded_score(
            session_id, score_id="score-a", score={"source_musicxml_path": str(source)},
            summary={"title": "Example tune", "parts": []},
            files={"musicxml_path": str(source.relative_to(store._project_root))},
            file_name="example.xml")
        for child_id, part_id in (("take-a", "P1"), ("take-b", "P2")):
            await store.record_take(session_id, child_id, {
                "status": "completed", "scoreId": "score-a", "jobKind": "synthesis",
                "audioTrack": {"key": f"id:{part_id}", "part_id": part_id}})
        await store.save_mix(session_id, "owner", songs.PlayerMix.model_validate({
            "score_id": "score-a", "vocals": [
                {"key": "id:P1", "muted": False, "solo": False, "volume": 0.7},
                {"key": "id:P2", "muted": False, "solo": False, "volume": 1}],
            "instruments": [{"raw_part_id": "P3", "muted": False, "solo": False,
                             "volume": 1}],
            "bus": {"muted": False, "solo": False, "level_db": 0}}))
        job = {"scoreId": "score-a", "items": [
            {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "take-a"},
            {"type": "take", "status": "done", "mode": "lyrics", "childJobId": "take-b"}],
            "package": {"stats": {"balance": {"mode": "lyrics", "stats": {
                "vocals": [{"gain_db": 0, "dropped": False},
                           {"gain_db": -6, "dropped": False}],
                "instruments": {"gain_db": -9}}}}}}
        await store.set_sing_all_opening_balance(session_id, "owner", job)
        player = await store.get_player(session_id, "owner")
        assert player["vocals"][0]["volume"] == 0.7
        assert player["vocals"][1]["volume"] == pytest.approx(10 ** (-6 / 20))
        assert player["instruments"][0]["volume"] == 1
        assert player["bus"]["levelDb"] == -9
        revision = player["revision"]
        await store.set_sing_all_opening_balance(session_id, "owner", job)
        assert (await store.get_player(session_id, "owner"))["revision"] == revision

    asyncio.run(scenario())


def test_progress_package_redacts_private_fields():
    job = progress_job(stage="packaging")
    job["items"][0]["status"] = "done"
    job["package"] = {"state": "mixing", "mixes": {"done": 1, "total": 2},
                      "zipPath": "private", "files": ["private"], "error": "private"}
    assert progress(job, now=NOW)["package"] == {
        "state": "mixing", "mixes": {"done": 1, "total": 2}}


def test_stop_during_packaging_is_too_late(monkeypatch):
    parent = {"jobId": "parent-a", "uid": "owner", "sessionId": "song-a",
              "stage": "packaging", "status": "running", "cancelRequested": False,
              "items": [{"type": "take", "status": "done"}],
              "package": {"state": "mixing", "updatedAt": NOW}}
    async def auth(_):
        return "owner"
    class Sessions:
        async def get_session(self, *_):
            return SimpleNamespace(history=[])
        async def set_metadata(self, *_):
            pass
    async def reconcile(*_):
        pass
    monkeypatch.setattr(main, "_get_user_id_or_401", auth)
    monkeypatch.setattr(main, "active_job_for_song", lambda *_: parent)
    monkeypatch.setattr(main, "load_sing_all_job", lambda _: parent)
    monkeypatch.setattr(main.SingAllRunner, "reconcile", reconcile)
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sessions/{session_id}/stop")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=Sessions(), orchestrator=SimpleNamespace(),
        job_store=SimpleNamespace(list_active_jobs=lambda **_: []))))
    result = asyncio.run(endpoint("song-a", request))
    assert result["singAll"] == {"jobId": "parent-a", "result": "too_late"}
    assert parent["cancelRequested"] is False
    parent["package"]["state"] = "done"
    assert decide(parent, {}, {"now": NOW})[0]["status"] == "completed"


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
    request.app.state.sessions = Sessions()
    job["uid"] = "other"
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint("parent-a", request))
    assert (error.value.status_code, error.value.detail) == (404, {"code": "job_not_found"})
    job["uid"] = "owner"
    monkeypatch.setattr(main, "load_sing_all_job", lambda _: None)
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint("parent-a", request))
    assert (error.value.status_code, error.value.detail) == (404, {"code": "job_not_found"})
    monkeypatch.setattr(main, "load_sing_all_job", lambda _: job)
    request.app.state.settings = SimpleNamespace(backend_use_storage=True)
    job["package"]["state"] = "zipping"
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(endpoint("parent-a", request))
    assert (error.value.status_code, error.value.detail) == (409, {"code": "zip_unavailable"})


def test_download_url_serves_zip_from_storage_emulator(monkeypatch, storage_emulator):
    path = "singAll/owner/parent-a/Example tune - Sing All.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Example tune - Sing All/README.txt", "Made-up song\n")
    storage_client.upload_bytes("demo-bucket", payload.getvalue(), path, "application/zip")
    job = progress_job(stage="done", status="completed")
    job.update(uid="owner", package={"state": "done", "zipPath": path})
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
        sessions=Sessions(), settings=SimpleNamespace(
            backend_use_storage=True, storage_bucket="demo-bucket"))))
    result = asyncio.run(endpoint("parent-a", request))
    assert result["fileName"] == "Example tune - Sing All.zip"
    assert result["url"].startswith(storage_emulator)
    assert datetime.fromisoformat(result["expiresAt"]) > datetime.now(timezone.utc)
    with urllib.request.urlopen(result["url"], timeout=5) as response:
        assert response.read() == payload.getvalue()


def test_production_download_signs_get_with_iam_and_attachment_name(monkeypatch):
    import google.auth

    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("FIREBASE_STORAGE_EMULATOR_HOST", raising=False)
    calls = {}
    credentials = SimpleNamespace(token="token", service_account_email="identity",
                                  refresh=lambda _: calls.update(refreshed=True))
    monkeypatch.setattr(google.auth, "default", lambda **_: (credentials, "demo-project"))
    class Blob:
        def generate_signed_url(self, **kwargs):
            calls.update(kwargs)
            return "https://storage.example.invalid/download"
    monkeypatch.setattr(storage_client, "get_bucket", lambda _: SimpleNamespace(
        blob=lambda _path: Blob()))
    url, expires = storage_client.signed_download_url(
        "demo-bucket", "singAll/owner/parent-a/Résumé - Sing All.zip",
        "Résumé - Sing All.zip")
    assert url == "https://storage.example.invalid/download"
    assert calls["refreshed"] is True
    assert calls["version"] == "v4" and calls["method"] == "GET"
    assert calls["service_account_email"] == "identity"
    assert calls["access_token"] == "token"
    assert 'filename="Rsum - Sing All.zip"' in calls["response_disposition"]
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20-%20Sing%20All.zip" in (
        calls["response_disposition"])
    assert 14 * 60 < (expires - datetime.now(timezone.utc)).total_seconds() <= 15 * 60
