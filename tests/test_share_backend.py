"""Read-only Song sharing with made-up score, take, and account data."""

import asyncio
import uuid

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client

from src.backend.message_catalog import backend_message
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
    app.state.sessions._sessions[sid].score_summary["parts"][0]["take_signature"] = "edited-music"
    assert http.get(f"/public/s/{token}").json()["player"]["vocals"] == []
    assert http.get(vocal["audio_url"]).status_code == 404
