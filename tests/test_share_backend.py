"""Read-only Song sharing with made-up score, take, and account data."""

import asyncio
import uuid
from dataclasses import replace

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client

from src.backend.message_catalog import backend_message
from src.backend.orchestrator import INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY
from tests.test_backend_api import _create_session, _upload_score, client_with_env  # noqa: F401
from tests.test_score_job_history import workspace  # noqa: F401
from tests.test_songs_backend import _start_take, _upload


def test_share_store_is_atomic_and_read_only(workspace):
    store, _, _ = workspace
    owner = f"made-up-owner-{uuid.uuid4().hex}"

    async def scenario():
        sid = (await store.create_session(owner)).id
        await _upload(store, sid)
        before = (await store.get_song_row(sid, owner))["last_active_at"]
        first = await store.share_on(sid, owner)
        assert len(first) == 22
        assert await store.share_on(sid, owner) == first
        assert (await store.get_share(first))["sessionId"] == sid
        assert (await store.read_shared(sid)).share_token == first
        assert (await store.get_song_row(sid, owner))["last_active_at"] == before
        assert (await store.list_songs(owner, 10))[0]["shared"] is True
        await store.share_off(sid, owner)
        await store.share_off(sid, owner)
        assert await store.get_share(first) is None
        assert (await store.get_song_row(sid, owner))["shared"] is False
        second = await store.share_on(sid, owner)
        assert second != first
        assert await store.get_share(first) is None
        assert (await store.get_share(second))["ownerUid"] == owner

    asyncio.run(scenario())


def _share_firestore(monkeypatch):
    firestore = Client(project=f"demo-share-{uuid.uuid4().hex}", credentials=AnonymousCredentials())
    monkeypatch.setattr("src.backend.main.get_firestore_client", lambda: firestore)


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_owner_lifecycle_and_public_page(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    owner_path = f"/songs/{sid}/share"
    absent = http.post(owner_path, json={})
    assert absent.status_code == 409
    assert absent.json() == {"code": "share_notice_required", "message": backend_message("share.notice_required")}
    assert http.get(f"/songs/{sid}/workspace").json()["song"]["share"] == {
        "available": True, "enabled": False, "url": None,
    }

    first = http.post(owner_path, json={"notice_accepted": True})
    assert first.status_code == 200
    assert first.json()["enabled"] is True
    assert http.post(owner_path, json={}).json() == first.json()
    token = first.json()["url"].rsplit("/", 1)[-1]
    base = f"/public/s/{token}"
    assert http.get("/songs").json()["songs"][0]["shared"] is True
    assert http.get(f"/songs/{sid}/workspace").json()["song"]["share"]["url"] == first.json()["url"]

    # A public viewer never changes the Song's activity or sees internal IDs.
    state = app.state.sessions._sessions[sid]
    before = state.last_active_at
    page = http.get(base, headers={"authorization": ""})
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    payload = page.json()
    assert payload["is_owner"] is False and payload["song_id"] is None
    assert payload["title"] == "Test"
    assert payload["player"]["vocals"] == []
    assert sid not in page.text and "test-user" not in page.text
    assert http.get(f"{base}/score").status_code == 200
    assert state.last_active_at == before

    off = http.delete(owner_path)
    assert off.status_code == 200 and off.json() == {"enabled": False, "url": None}
    assert http.delete(owner_path).json() == off.json()
    for path in (base, f"{base}/score", f"{base}/midi", f"{base}/audio?track=id%3AP1"):
        response = http.get(path)
        assert response.status_code == 404
        assert response.json() == {"code": "share_off", "message": backend_message("share.off")}
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    second = http.post(owner_path, json={})
    assert second.status_code == 200 and second.json()["url"] != first.json()["url"]
    assert http.get(base).json()["code"] == "share_off"
    assert http.get(f"/public/s/{second.json()['url'].rsplit('/', 1)[-1]}").status_code == 200


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_audio_range_and_stale_track(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    audio = app.state.sessions.session_dir(sid) / "take.wav"
    audio.write_bytes(b"abcdefgh")
    take = _start_take(app, sid, "made-up-take", outputPath=str(audio.relative_to(app.state.settings.project_root)))
    state = app.state.sessions._sessions[sid]
    state.score_summary["parts"][0]["take_signature"] = "original-music"
    take["audioTrack"]["take_signature"] = "original-music"
    asyncio.run(app.state.sessions.record_take(sid, "made-up-take", take))
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    page = http.get(f"/public/s/{token}").json()
    [vocal] = page["player"]["vocals"]
    assert set(vocal) == {"key", "mode", "label", "verse_number", "duration_seconds", "voice_name",
                          "muted", "solo", "volume", "audio_url"}
    assert vocal["mode"] == "lyrics"
    data = http.get(vocal["audio_url"], headers={"range": "bytes=2-4"})
    assert data.status_code == 206 and data.content == b"cde"
    assert "content-disposition" not in data.headers
    assert "content-disposition" not in http.get(vocal["audio_url"] + "&download=1").headers
    assert http.get(f"/public/s/{token}/audio?track=missing").status_code == 404
    app.state.job_store.update_job("made-up-take", outputPath="outside/take.wav")
    assert http.get(vocal["audio_url"]).status_code == 404
    app.state.sessions._sessions[sid].score_summary["parts"][0]["take_signature"] = "edited-music"
    assert http.get(f"/public/s/{token}").json()["player"]["vocals"] == []
    assert http.get(vocal["audio_url"]).status_code == 404


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "off"}], indirect=True)
def test_sharing_off_by_default(client_with_env):
    http, _ = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    assert http.get(f"/songs/{sid}/workspace").json()["song"]["share"] == {
        "available": False, "enabled": False, "url": None,
    }
    for response in (http.post(f"/songs/{sid}/share", json={"notice_accepted": True}),
                     http.delete(f"/songs/{sid}/share")):
        assert response.status_code == 403
        assert response.json()["code"] == "share_not_available"
    for suffix in ("", "/score", "/midi", "/audio?track=id%3AP1"):
        response = http.get(f"/public/s/{'x' * 22}{suffix}")
        assert response.status_code == 404 and response.json()["code"] == "share_off"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "allowlist"}], indirect=True)
def test_share_allowlist_and_unified_off_response(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    app.state.share_allowlist._load_config = lambda: {"allowedUids": []}
    assert http.get(f"/songs/{sid}/workspace").json()["song"]["share"]["available"] is False
    assert http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["code"] == "share_not_available"
    app.state.share_allowlist._config = None
    app.state.share_allowlist._load_config = lambda: {"allowedUids": ["test-user"]}
    url = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"]
    token = url.rsplit("/", 1)[-1]
    path = f"/public/s/{token}"
    assert http.get(path, headers={"authorization": ""}).status_code == 200
    unknown = http.get(f"/public/s/{'x' * 22}")
    assert unknown.status_code == 404
    app.state.settings = replace(app.state.settings, share_links="off")
    off = http.get(path)
    assert off.status_code == unknown.status_code and off.json() == unknown.json()
    app.state.settings = replace(app.state.settings, share_links="allowlist")
    assert http.delete(f"/songs/{sid}").status_code == 200
    deleted = http.get(path)
    assert deleted.status_code == unknown.status_code and deleted.json() == unknown.json()


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_midi_matches_owner_and_rate_limits(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    state = app.state.sessions._sessions[sid]
    midi = app.state.sessions.session_dir(sid) / "made-up.mid"
    midi.write_bytes(b"MThd" + bytes(10))
    state.files.update({
        INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY: "made-up-signature",
        "instrumental_midi_original_path": str(midi),
    })
    state.score_summary["performance_midi"] = {
        "version": 1, "instrumental_parts": [{"raw_part_id": "P5"}],
        "has_instrumental_parts": True, "original_midi_available": True,
        "expanded_midi_available": False,
    }
    async def signature(*args, **kwargs):
        return "made-up-signature"
    monkeypatch.setattr(app.state.orchestrator, "instrumental_midi_signature", signature)
    owner = http.get(f"/sessions/{sid}/instrumental-midi")
    before = state.last_active_at
    page = http.get(f"/public/s/{token}")
    assert page.json()["player"]["midi_url"] == f"/public/s/{token}/midi?expand_repeats=false"
    public = http.get(f"/public/s/{token}/midi")
    assert owner.status_code == public.status_code == 200
    assert owner.content == public.content == midi.read_bytes()
    assert "content-disposition" not in public.headers
    assert state.last_active_at == before

    app.state.settings = replace(app.state.settings, share_page_limit_per_minute=2)
    now = [100.0]
    app.state.share_clock = lambda: now[0]
    for _ in range(2):
        assert http.get(f"/public/s/{token}", headers={"x-forwarded-for": "192.0.2.1"}).status_code == 200
    blocked = http.get(f"/public/s/{token}", headers={"x-forwarded-for": "192.0.2.1"})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"
    assert blocked.json()["code"] == "rate_limited"
    assert http.get(f"/public/s/{token}", headers={"x-forwarded-for": "192.0.2.2"}).status_code == 200
    now[0] += 61
    assert http.get(f"/public/s/{token}", headers={"x-forwarded-for": "192.0.2.1"}).status_code == 200
