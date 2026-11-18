"""Read-only Song sharing with made-up score, take, and account data."""

import asyncio
import uuid
from dataclasses import replace

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client

from src.backend.message_catalog import backend_message
from src.backend.config import Settings
from src.backend.orchestrator import INSTRUMENTAL_MIDI_SIGNATURE_METADATA_KEY
from tests.test_backend_api import _create_session, _upload_score, client_with_env  # noqa: F401
from tests.test_score_job_history import workspace  # noqa: F401
from tests.test_songs_backend import _start_take, _upload


def test_share_setting_rejects_unknown_mode(monkeypatch):
    monkeypatch.setenv("SHARE_LINKS", "unexpected")
    with pytest.raises(ValueError, match="SHARE_LINKS must be one of"):
        Settings.from_env()


def test_share_store_is_atomic_and_read_only(workspace):
    store, _, _ = workspace
    owner = f"made-up-owner-{uuid.uuid4().hex}"

    async def scenario():
        sid = (await store.create_session(owner)).id
        await _upload(store, sid)
        before = (await store.get_song_row(sid, owner))["last_active_at"]
        first = await store.share_on(sid, owner)
        assert len(first) == 22
        if hasattr(store, "_doc_ref"):
            session_updated = store._doc_ref(sid).get().update_time
            share_ref = store._client.collection("shares").document(first)
            share_updated = share_ref.get().update_time
        assert await store.share_on(sid, owner) == first
        if hasattr(store, "_doc_ref"):
            assert store._doc_ref(sid).get().update_time == session_updated
            assert share_ref.get().update_time == share_updated
        assert (await store.get_share(first))["sessionId"] == sid
        assert (await store.read_shared(sid)).share_token == first
        assert (await store.ensure_active_musicxml(sid, owner, touch=False)).is_file()
        assert (await store.get_player(sid, owner, shared=True)) is not None
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
    return firestore


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
    owner_score = http.get(f"/sessions/{sid}/score")
    before = state.last_active_at
    order_before = http.get("/songs").json()
    page = http.get(base, headers={"authorization": ""})
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    payload = page.json()
    assert payload["is_owner"] is False and payload["song_id"] is None
    assert payload["title"] == "Test"
    assert payload["player"]["vocals"] == []
    assert sid not in page.text and "test-user" not in page.text
    public_score = http.get(f"{base}/score")
    assert public_score.status_code == owner_score.status_code == 200
    assert public_score.content == owner_score.content
    assert state.last_active_at == before
    assert http.get("/songs").json() == order_before

    off = http.delete(owner_path)
    assert off.status_code == 200 and off.json() == {"enabled": False, "url": None}
    assert http.get("/songs").json()["songs"][0]["shared"] is False
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
    app.state.settings = replace(app.state.settings, share_audio_limit_per_minute=1)
    later = app.state.share_clock() + 61
    app.state.share_clock = lambda: later
    assert http.get(vocal["audio_url"]).status_code == 200
    limited = http.get(vocal["audio_url"])
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    app.state.settings = replace(app.state.settings, share_audio_limit_per_minute=1200)
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


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "allowlist"}], indirect=True)
def test_removed_allowlist_owner_can_revoke_share(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    app.state.share_allowlist._load_config = lambda: {"allowedUids": ["test-user"]}
    url = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"]
    path = f"/public/s/{url.rsplit('/', 1)[-1]}"
    assert http.get(path).status_code == 200

    app.state.share_allowlist._config = None
    app.state.share_allowlist._load_config = lambda: {"allowedUids": []}
    assert http.get(f"/songs/{sid}/workspace").json()["song"]["share"]["available"] is False
    response = http.delete(f"/songs/{sid}/share")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "url": None}
    revoked = http.get(path)
    assert revoked.status_code == 404 and revoked.json()["code"] == "share_off"


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_midi_matches_owner_and_rate_limits(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    state = app.state.sessions._sessions[sid]
    audio = app.state.sessions.session_dir(sid) / "take.wav"
    audio.write_bytes(b"made-up-audio")
    take = _start_take(app, sid, "made-up-midi-test-take",
                       outputPath=str(audio.relative_to(app.state.settings.project_root)))
    asyncio.run(app.state.sessions.record_take(sid, "made-up-midi-test-take", take))
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
    order_before = http.get("/songs").json()
    page = http.get(f"/public/s/{token}")
    assert page.json()["player"]["midi_url"] == f"/public/s/{token}/midi?expand_repeats=false"
    public = http.get(f"/public/s/{token}/midi")
    assert http.get(f"/public/s/{token}/score").status_code == 200
    assert http.get(page.json()["player"]["vocals"][0]["audio_url"]).content == b"made-up-audio"
    assert owner.status_code == public.status_code == 200
    assert owner.content == public.content == midi.read_bytes()
    assert "content-disposition" not in public.headers
    assert state.last_active_at == before
    assert http.get("/songs").json() == order_before

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


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_page_uses_latest_mix_modes_and_score(client_with_env, firestore_emulator, monkeypatch):
    firestore = _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    score_id = _upload_score(http, sid).json()["score_id"]
    state = app.state.sessions._sessions[sid]
    state.score_summary.update({
        "duration_seconds": 12.5,
        "expanded_duration_seconds": 15.0,
        "repeat_expansion_available": True,
        "performance_measure_map": {"written": [], "expanded": []},
        "private_extra": "do-not-send",
    })
    state.score_summary["parts"][0]["take_signature"] = "original-music"
    for job_id, key, mode in (("made-up-lyrics", "id:P1:lyrics", "lyrics"),
                              ("made-up-solfege", "id:P1:solfege", "solfege")):
        job = _start_take(app, sid, job_id)
        audio_track = {**job["audioTrack"], "key": key, "mode": mode,
                       "take_signature": "original-music"}
        app.state.job_store.update_job(job_id, audioTrack=audio_track, voicebankName="Voice A")
        asyncio.run(app.state.sessions.record_take(sid, job_id, app.state.job_store.get_job(job_id)))
    mix = {
        "score_id": score_id,
        "vocals": [
            {"key": "id:P1:lyrics", "muted": True, "solo": False, "volume": 1.1},
            {"key": "id:P1:solfege", "muted": False, "solo": True, "volume": 0.8},
        ],
        "instruments": [{"raw_part_id": "P5", "muted": False, "solo": False,
                         "volume": 0.65, "gm_program": 0, "soundfont_bank": 0,
                         "preset_kind": None, "unassigned": False}],
        "bus": {"muted": False, "solo": False, "level_db": -3},
    }
    assert http.put(f"/songs/{sid}/player/mix", json=mix).status_code == 200
    first = http.post(f"/songs/{sid}/share", json={"notice_accepted": True})
    assert first.status_code == 200
    notice_ref = firestore.collection("users").document("test-user")
    accepted_at = notice_ref.get().to_dict()["shareNoticeAt"]
    assert http.post(f"/songs/{sid}/share", json={}).json() == first.json()
    assert notice_ref.get().to_dict()["shareNoticeAt"] == accepted_at
    another = _create_session(http)
    _upload_score(http, another)
    second = http.post(f"/songs/{another}/share", json={})
    assert second.status_code == 200 and second.json()["url"] != first.json()["url"]
    base = f"/public/s/{first.json()['url'].rsplit('/', 1)[-1]}"

    monkeypatch.setattr("src.backend.main.verify_id_token_claims",
                        lambda token: {"uid": "made-up-viewer"})
    page = http.get(base)
    payload = page.json()
    assert payload["is_owner"] is False and payload["song_id"] is None
    assert payload["score"]["summary"]["duration_seconds"] == 12.5
    assert set(payload["score"]["summary"]) == {
        "duration_seconds", "expanded_duration_seconds", "repeat_expansion_available",
        "performance_measure_map", "performance_midi",
    }
    assert payload["player"]["instruments"] == mix["instruments"]
    assert payload["player"]["bus"]["level_db"] == -3
    assert [(v["mode"], v["muted"], v["solo"], v["volume"])
            for v in payload["player"]["vocals"]] == [
                ("lyrics", True, False, 1.1), ("solfege", False, True, 0.8),
            ]
    assert payload["credits"]["voices"] == ["Voice A"]
    assert all(value not in page.text for value in (
        sid, "test-user", "made-up-lyrics", "made-up-solfege", "do-not-send",
    ))

    def broken(_):
        raise ValueError("bad token")
    monkeypatch.setattr("src.backend.main.verify_id_token_claims", broken)
    assert http.get(base).json()["is_owner"] is False
    monkeypatch.setattr("src.backend.main.verify_id_token_claims",
                        lambda token: {"uid": "test-user"})
    assert http.get(base).json()["song_id"] == sid
    # A score Replace keeps the link but removes takes of the old score.
    replaced = http.post(f"/sessions/{sid}/upload", data={"replace": "true"},
                         files={"file": ("replacement.xml", b"<score-partwise version='3.1'></score-partwise>", "application/xml")})
    assert replaced.status_code == 200
    assert http.get(base).json()["player"]["vocals"] == []


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on", "BACKEND_USE_STORAGE": "true"}], indirect=True)
def test_share_storage_audio_checks_job_folder_and_streams_ranges(
    client_with_env, firestore_emulator, monkeypatch,
):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    path = f"sessions/test-user/{sid}/jobs/made-up-storage-take/output.mp3"
    take = _start_take(app, sid, "made-up-storage-take", outputPath=path)
    asyncio.run(app.state.sessions.record_take(sid, "made-up-storage-take", take))
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    [vocal] = http.get(f"/public/s/{token}").json()["player"]["vocals"]
    streamed = http.get(vocal["audio_url"], headers={"range": "bytes=0-6"})
    assert streamed.status_code == 206 and streamed.content == b"storage"
    assert "content-disposition" not in streamed.headers
    app.state.job_store.update_job("made-up-storage-take",
                                   outputPath=f"sessions/test-user/{sid}/jobs/another-job/output.mp3")
    assert http.get(vocal["audio_url"]).status_code == 404
