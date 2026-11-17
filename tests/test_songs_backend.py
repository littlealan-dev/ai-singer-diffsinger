"""Songs: the Song fields, the player record, opening a Song, My songs and Replace.

Titles, part names and ids here are made up.
"""

import asyncio
import logging
import uuid

import pytest

from src.backend import session as module
from src.backend import songs
from src.backend.message_catalog import backend_message
from tests.test_backend_api import (  # noqa: F401 - pytest fixtures
    _create_session,
    _upload_score,
    client,
    client_with_env,
)
from tests.test_score_job_history import workspace  # noqa: F401 - pytest fixture


OWNER = "song-owner"


async def _upload(store, sid, *, title="Example Song", file_name="example.xml"):
    score_id = uuid.uuid4().hex
    source = store.session_dir(sid) / f"{score_id}.xml"
    source.write_text("<score-partwise/>")
    summary = {"title": title, "parts": [{"part_id": "P1", "take_signature": "sig-a"}]}
    await store.commit_uploaded_score(
        sid, score_id=score_id, score={"source_musicxml_path": str(source)}, summary=summary,
        files={"musicxml_path": str(source.relative_to(store._project_root))}, file_name=file_name,
    )
    return score_id


def _take(score_id, *, part="P1", repeats=False, seconds=12.5, signature="sig-a", voice="Voice A"):
    return {
        "userId": OWNER, "status": "completed", "scoreId": score_id, "expandRepeats": repeats,
        "actualDurationSeconds": seconds, "voicebankName": voice,
        "audioTrack": {"key": f"id:{part}", "part_id": part, "part_index": 0, "label": f"Part {part}",
                       "verse_number": 1, "take_signature": signature},
    }


def _mix(score_id, **overrides):
    body = {
        "score_id": score_id,
        "vocals": [{"key": "id:P1", "muted": True, "solo": False, "volume": 1.15}],
        "instruments": [{"raw_part_id": "P5", "muted": False, "solo": True, "volume": 0.4,
                         "gm_program": 4, "soundfont_bank": 0, "preset_kind": "melodic",
                         "unassigned": False}],
        "bus": {"muted": False, "solo": False, "level_db": -6},
    }
    body.update(overrides)
    return songs.PlayerMix.model_validate(body)


def test_lyrics_and_solfege_takes_of_one_part_keep_separate_tracks():
    player = songs.empty_player(None, "fake-score")
    old_lyrics = _take("fake-score")
    assert songs.apply_take(player, "old-lyrics", old_lyrics)
    solfege = _take("fake-score")
    solfege["audioTrack"].update(key="id:P1:solfege", mode="solfege")
    assert songs.apply_take(player, "solfege", solfege)
    assert [(track["mode"], track["sourceJobId"]) for track in player["vocals"]] == [
        ("lyrics", "old-lyrics"), ("solfege", "solfege")]
    new_lyrics = _take("fake-score")
    new_lyrics["audioTrack"]["key"] = "id:P1:lyrics"
    assert songs.apply_take(player, "new-lyrics", new_lyrics)
    assert len(player["vocals"]) == 2
    assert [track["sourceJobId"] for track in player["vocals"]] == ["new-lyrics", "solfege"]
    assert songs.vocal_response(player["vocals"][1])["mode"] == "solfege"


# ---- The stores (memory and Firestore behave the same) ----------------------


def test_first_upload_makes_a_song_and_a_title(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session(OWNER)).id
        assert (await store.get_session(sid, OWNER)).song() is None
        score_id = await _upload(store, sid, title="  Example Song  ")
        song = (await store.get_session(sid, OWNER)).song()
        assert song["title"] == "Example Song"
        assert song["title_edited"] is False
        assert song["created_at"]
        player = await store.get_player(sid, OWNER)
        assert player["scoreId"] == score_id
        assert player["vocals"] == [] and player["repeats"] is False
        assert [row["id"] for row in await store.list_songs(OWNER, 50)] == [sid]

        untitled = (await store.create_session(OWNER)).id
        await _upload(store, untitled, title="", file_name="made-up tune.musicxml")
        assert (await store.get_session(untitled, OWNER)).song()["title"] == "made-up tune"

    asyncio.run(scenario())


def test_a_settled_take_fills_the_player_record(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session(OWNER)).id
        score_id = await _upload(store, sid)
        await store.record_take(sid, "take-1", _take(score_id))
        player = await store.get_player(sid, OWNER)
        assert player["revision"] == 1
        [entry] = player["vocals"]
        assert entry == {
            "key": "id:P1", "mode": "lyrics", "partId": "P1", "partIndex": 0,
            "label": "Part P1", "verseNumber": 1,
            "takeSignature": "sig-a", "sourceJobId": "take-1", "expandRepeats": False,
            "durationSeconds": 12.5, "voiceName": "Voice A",
            "muted": False, "solo": False, "volume": 1.0,
        }

        # Recording the same take again changes nothing.
        await store.record_take(sid, "take-1", _take(score_id))
        assert (await store.get_player(sid, OWNER))["revision"] == 1

        # A second take of the part replaces it and keeps the owner's mixer.
        await store.save_mix(sid, OWNER, _mix(score_id))
        await store.record_take(sid, "take-2", _take(score_id, seconds=13.0))
        [entry] = (await store.get_player(sid, OWNER))["vocals"]
        assert entry["sourceJobId"] == "take-2" and entry["durationSeconds"] == 13.0
        assert (entry["muted"], entry["volume"]) == (True, 1.15)

        # Another part is added; a take in the other repeat order clears both.
        await store.record_take(sid, "take-3", _take(score_id, part="P2"))
        assert len((await store.get_player(sid, OWNER))["vocals"]) == 2
        await store.record_take(sid, "take-4", _take(score_id, repeats=True))
        player = await store.get_player(sid, OWNER)
        assert player["repeats"] is True
        assert [entry["sourceJobId"] for entry in player["vocals"]] == ["take-4"]

        # A take of a replaced score, or a job that isn't a take, changes nothing.
        before = await store.get_player(sid, OWNER)
        await store.record_take(sid, "old-take", _take("an-older-score"))
        await store.record_take(sid, "a-split", {**_take(score_id), "jobKind": "preprocess"})
        assert (await store.get_player(sid, OWNER))["vocals"] == before["vocals"]

    asyncio.run(scenario())


def test_the_mix_is_saved_and_a_late_mix_is_refused(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session(OWNER)).id
        score_id = await _upload(store, sid)
        await store.record_take(sid, "take-1", _take(score_id))
        unknown = {"key": "id:P9", "muted": True, "solo": True, "volume": 0.2}
        revision = await store.save_mix(sid, OWNER, _mix(score_id, vocals=[
            {"key": "id:P1", "muted": False, "solo": True, "volume": 0.5}, unknown,
        ]))
        player = await store.get_player(sid, OWNER)
        assert revision == player["revision"] == 2
        assert [(v["key"], v["solo"], v["volume"]) for v in player["vocals"]] == [("id:P1", True, 0.5)]
        assert player["instruments"] == [{
            "rawPartId": "P5", "muted": False, "solo": True, "volume": 0.4, "gmProgram": 4,
            "soundfontBank": 0, "presetKind": "melodic", "unassigned": False,
        }]
        assert player["bus"] == {"muted": False, "solo": False, "levelDb": -6.0}

        with pytest.raises(songs.SongScoreChangedError):
            await store.save_mix(sid, OWNER, _mix("an-older-score"))
        with pytest.raises(PermissionError):
            await store.save_mix(sid, "someone-else", _mix(score_id))

    asyncio.run(scenario())


def test_replace_resets_the_player_and_keeps_a_users_title(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session(OWNER)).id
        first = await _upload(store, sid, title="First Tune")
        await store.record_take(sid, "take-1", _take(first))
        second = await _upload(store, sid, title="Second Tune")
        assert (await store.get_session(sid, OWNER)).song()["title"] == "Second Tune"
        player = await store.get_player(sid, OWNER)
        assert (player["scoreId"], player["vocals"], player["revision"]) == (second, [], 0)

        await store.rename_song(sid, OWNER, "My Own Name")
        await _upload(store, sid, title="Third Tune")
        song = (await store.get_session(sid, OWNER)).song()
        assert (song["title"], song["title_edited"]) == ("My Own Name", True)

        # A take that finishes after the Replace changes nothing.
        await store.record_take(sid, "late-take", _take(first))
        assert (await store.get_player(sid, OWNER))["vocals"] == []

    asyncio.run(scenario())


def test_my_songs_rename_and_delete(workspace):
    store, _, _ = workspace
    # Its own user: the Firestore emulator keeps other tests' Songs.
    owner = f"song-owner-{uuid.uuid4().hex}"

    async def scenario():
        older = (await store.create_session(owner)).id
        score_id = await _upload(store, older, title="Older Tune")
        await store.record_take(older, "take-1", _take(score_id))
        newer = (await store.create_session(owner)).id
        await _upload(store, newer, title="Newer Tune")
        await store.create_session(owner)  # no upload: not a Song
        other = (await store.create_session("someone-else")).id
        await _upload(store, other, title="Not Yours")

        rows = await store.list_songs(owner, 50)
        assert [(row["title"], row["track_count"]) for row in rows] == [("Newer Tune", 0), ("Older Tune", 1)]
        assert len(await store.list_songs(owner, 1)) == 1

        row = await store.rename_song(older, owner, "Renamed Tune")
        assert (row["id"], row["title"], row["track_count"]) == (older, "Renamed Tune", 1)
        with pytest.raises(PermissionError):
            await store.rename_song(other, owner, "Taken")

        await store.delete_song(newer, owner)
        assert [row["id"] for row in await store.list_songs(owner, 50)] == [older]
        for read in (store.get_session, store.get_snapshot, store.get_player):
            with pytest.raises(KeyError):
                await read(newer, owner)
        with pytest.raises(KeyError):
            await store.delete_song(newer, owner)

    asyncio.run(scenario())


# ---- The endpoints ----------------------------------------------------------


def _start_take(app, session_id, job_id, *, turn="turn-1", status="completed", kind=None, **extra):
    """A take job as the orchestrator leaves it, in the API tests' job store."""
    score_id = app.state.sessions._sessions[session_id].score_id
    app.state.job_store.create_job(
        job_id=job_id, user_id="test-user", session_id=session_id, status="queued",
        originating_turn_id=turn,
        audio_track={"key": "id:P1", "part_id": "P1", "part_index": 0, "label": "Solo",
                     "verse_number": 1, "take_signature": None},
        expand_repeats=False, provenance={"scoreId": score_id, "scoreVersionNo": 1},
    )
    fields = {"status": status, **extra}
    if kind:
        fields["jobKind"] = kind
    if status == "completed":
        fields.setdefault("audioUrl", f"/sessions/{session_id}/audio?file=take.wav")
        fields.setdefault("outputPath", f"data/sessions/{session_id}/take.wav")
        fields.setdefault("actualDurationSeconds", 4.0)
    app.state.job_store.update_job(job_id, **fields)
    return app.state.job_store.get_job(job_id)


def test_a_session_becomes_a_song_at_its_first_upload(client):
    test_client, _app = client
    session_id = _create_session(test_client)
    before = test_client.get(f"/songs/{session_id}/workspace")
    assert before.status_code == 404
    assert before.json() == {"code": "song_not_found", "message": backend_message("song.not_found")}
    assert test_client.get("/songs").json() == {"songs": []}

    upload = _upload_score(test_client, session_id, filename="made-up.xml")
    assert upload.status_code == 200
    song = upload.json()["song"]
    # The test parser's score is titled "Test".
    assert (song["id"], song["title"], song["title_edited"]) == (session_id, "Test", False)
    assert set(song) == {"id", "title", "title_edited", "created_at"}

    [row] = test_client.get("/songs").json()["songs"]
    assert row == {
        "id": session_id, "title": "Test", "created_at": song["created_at"],
        "last_active_at": row["last_active_at"], "status": "idle", "track_count": 0,
    }


def test_workspace_returns_chat_score_jobs_and_usable_tracks(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload = _upload_score(test_client, session_id).json()
    sessions = app.state.sessions
    asyncio.run(sessions.append_history(session_id, "user", "sing the solo"))
    asyncio.run(sessions.append_history(session_id, "assistant", "Here it is.",
                                        sing_all_job_id="made-up-sing-all-parent"))

    take = _start_take(app, session_id, "done-take")
    _start_take(app, session_id, "failed-take", turn="turn-2", status="failed",
                errorMessage="Couldn't finish the take.")
    _start_take(app, session_id, "stopped-take", turn="turn-3", status="cancelled", cancelledBy="user")
    app.state.job_store.create_job(job_id="ended-mix", user_id="test-user", session_id=session_id,
                                   status="completed")
    app.state.job_store.update_job("ended-mix", jobKind="export_mix")
    app.state.job_store.create_job(job_id="running-split", user_id="test-user",
                                   session_id=session_id, status="running")
    app.state.job_store.update_job("running-split", jobKind="preprocess")
    app.state.job_store.create_job(job_id="sing-all-child", user_id="test-user",
                                   session_id=session_id, status="running")
    app.state.job_store.update_job("sing-all-child", jobKind="synthesis",
                                   parentJobId="made-up-sing-all-parent")
    asyncio.run(sessions.record_take(session_id, "done-take", take))

    response = test_client.get(f"/songs/{session_id}/workspace")
    assert response.status_code == 200
    workspace = response.json()
    assert workspace["session_id"] == session_id
    assert workspace["song"]["title"] == "Test"
    assert workspace["score"]["score_id"] == upload["score_id"]
    assert workspace["score"]["current_score"] == {"version": 1}
    assert workspace["score"]["solfege_settings"]["revision"] == 1
    assert workspace["score"]["score_summary"]["parts"][0]["part_id"] == "P1"
    assert [(m["role"], m["content"]) for m in workspace["messages"]] == [
        ("user", "sing the solo"), ("assistant", "Here it is."),
    ]
    assert workspace["messages"][1]["singAllJobId"] == "made-up-sing-all-parent"
    jobs = {job["job_id"]: job for job in workspace["jobs"]}
    assert list(jobs) == ["done-take", "failed-take", "running-split"]
    assert jobs["done-take"]["originating_turn_id"] == "turn-1"
    assert jobs["done-take"]["progress_url"] == f"/sessions/{session_id}/progress?job_id=done-take"
    assert "playback_token=" in jobs["done-take"]["audio_url"]
    assert jobs["failed-take"]["status"] == "error"
    [vocal] = workspace["player"]["vocals"]
    assert (vocal["key"], vocal["source_job_id"], vocal["volume"]) == ("id:P1", "done-take", 1.0)
    assert vocal["mode"] == "lyrics"
    assert "playback_token=" in vocal["audio_url"]
    assert vocal["progress_url"] == f"/sessions/{session_id}/progress?job_id=done-take"
    assert workspace["player"]["bus"] == {"muted": False, "solo": False, "level_db": 0.0}
    assert test_client.get(vocal["audio_url"]).status_code in {200, 404}  # the token is accepted


def test_workspace_leaves_out_a_track_whose_part_was_edited(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    state = app.state.sessions._sessions[session_id]
    state.score_summary["parts"][0]["take_signature"] = "music-now"
    take = _start_take(app, session_id, "old-take")
    take["audioTrack"]["take_signature"] = "music-then"
    asyncio.run(app.state.sessions.record_take(session_id, "old-take", take))

    workspace = test_client.get(f"/songs/{session_id}/workspace").json()
    assert workspace["player"]["vocals"] == []
    assert workspace["player"]["revision"] == 1


def test_mix_is_saved_validated_and_tied_to_the_score(client):
    test_client, app = client
    session_id = _create_session(test_client)
    score_id = _upload_score(test_client, session_id).json()["score_id"]
    take = _start_take(app, session_id, "done-take")
    asyncio.run(app.state.sessions.record_take(session_id, "done-take", take))
    body = {
        "score_id": score_id,
        "vocals": [{"key": "id:P1", "muted": True, "solo": False, "volume": 1.2},
                   {"key": "id:unknown", "muted": False, "solo": False, "volume": 1.0}],
        "instruments": [{"raw_part_id": "P5", "muted": False, "solo": False, "volume": 0.65,
                         "gm_program": 0, "soundfont_bank": 0, "preset_kind": None,
                         "unassigned": False}],
        "bus": {"muted": True, "solo": False, "level_db": -12},
    }
    saved = test_client.put(f"/songs/{session_id}/player/mix", json=body)
    assert saved.status_code == 200
    assert saved.json() == {"revision": 2}
    player = test_client.get(f"/songs/{session_id}/workspace").json()["player"]
    assert [(v["key"], v["muted"], v["volume"]) for v in player["vocals"]] == [("id:P1", True, 1.2)]
    assert player["instruments"] == body["instruments"]
    assert player["bus"] == {"muted": True, "solo": False, "level_db": -12.0}

    late = test_client.put(f"/songs/{session_id}/player/mix", json={**body, "score_id": "older"})
    assert late.status_code == 409
    assert late.json()["code"] == "score_changed"

    out_of_range = [
        {**body, "vocals": [{"key": "id:P1", "muted": False, "solo": False, "volume": 1.3}]},
        {**body, "instruments": [{**body["instruments"][0], "volume": 1.1}]},
        {**body, "instruments": [{**body["instruments"][0], "gm_program": 128}]},
        {**body, "instruments": [{**body["instruments"][0], "soundfont_bank": 16384}]},
        {**body, "bus": {"muted": False, "solo": False, "level_db": 7}},
        {**body, "vocals": [{"key": "k" * 201, "muted": False, "solo": False, "volume": 1}]},
        {**body, "instruments": [body["instruments"][0]] * 65},
        {"score_id": score_id},
    ]
    for bad in out_of_range:
        refused = test_client.put(f"/songs/{session_id}/player/mix", json=bad)
        assert refused.status_code == 400, bad
        assert refused.json()["code"] == "invalid_mix"


def test_rename_and_delete(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    renamed = test_client.patch(f"/songs/{session_id}", json={"title": "  A New Name  "})
    assert renamed.status_code == 200
    assert (renamed.json()["title"], renamed.json()["status"]) == ("A New Name", "idle")
    for bad in ({"title": "   "}, {"title": "x" * 121}, {"title": 5}, {}):
        refused = test_client.patch(f"/songs/{session_id}", json=bad)
        assert refused.status_code == 400
        assert refused.json()["code"] == "invalid_title"

    _start_take(app, session_id, "running-take", status="running")
    [row] = test_client.get("/songs").json()["songs"]
    assert row["status"] == "idle"  # the running take isn't the session's current job here
    busy = test_client.delete(f"/songs/{session_id}")
    assert busy.status_code == 409
    assert busy.json() == {"code": "work_running", "message": backend_message("song.work_running")}

    app.state.job_store.update_job("running-take", status="completed")
    assert test_client.delete(f"/songs/{session_id}").json() == {"deleted": True}
    assert test_client.get("/songs").json() == {"songs": []}
    assert test_client.get(f"/songs/{session_id}/workspace").status_code == 404
    assert test_client.post(f"/sessions/{session_id}/chat", json={"message": "hello"}).status_code == 404
    assert _upload_score(test_client, session_id).status_code == 404
    assert test_client.delete(f"/songs/{session_id}").status_code == 404


def test_my_songs_status_follows_the_current_job(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    _start_take(app, session_id, "current-take", status="running")
    app.state.sessions._sessions[session_id].current_job_id = "current-take"
    [row] = test_client.get("/songs").json()["songs"]
    assert row["status"] == "working"


def test_every_song_endpoint_hides_another_users_song(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    score_id = _upload_score(test_client, session_id).json()["score_id"]
    monkeypatch.setattr(
        "src.backend.main.verify_id_token_claims",
        lambda token: {"uid": "someone-else", "email": "other@example.com"},
    )
    mix = {"score_id": score_id, "vocals": [], "instruments": [],
           "bus": {"muted": False, "solo": False, "level_db": 0}}
    for response in (
        test_client.get(f"/songs/{session_id}/workspace"),
        test_client.patch(f"/songs/{session_id}", json={"title": "Taken"}),
        test_client.delete(f"/songs/{session_id}"),
        test_client.put(f"/songs/{session_id}/player/mix", json=mix),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "song_not_found"
    assert test_client.get("/songs").json() == {"songs": []}


def _replace_upload(test_client, session_id, content=None):
    xml = content if content is not None else b"<score-partwise version='3.1'></score-partwise>"
    return test_client.post(
        f"/sessions/{session_id}/upload",
        files={"file": ("second.xml", xml, "application/xml")},
        data={"replace": "true"},
    )


def test_replace_stops_a_running_take_and_uploads(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    first = _upload_score(test_client, session_id).json()["score_id"]
    take = _start_take(app, session_id, "done-take")
    asyncio.run(app.state.sessions.record_take(session_id, "done-take", take))
    _start_take(app, session_id, "running-take", status="running", kind="synthesis")
    stopped = []

    def stop_job(job_id):
        stopped.append(job_id)
        app.state.job_store.update_job(job_id, status="cancelled", cancelledBy="user")
        return "stopped"

    monkeypatch.setattr(app.state.job_store, "stop_job", stop_job)
    monkeypatch.setattr("src.backend.main._release_stopped_reservation",
                        lambda **_: asyncio.sleep(0, result=3))

    # Without replace, work running still refuses before the file is read.
    refused = _upload_score(test_client, session_id)
    assert refused.status_code == 409 and refused.json()["code"] == "work_running"
    assert stopped == []

    response = _replace_upload(test_client, session_id)
    assert response.status_code == 200
    assert stopped == ["running-take"]
    assert response.json()["score_id"] != first
    workspace = test_client.get(f"/songs/{session_id}/workspace").json()
    assert workspace["player"]["vocals"] == [] and workspace["player"]["score_id"] == response.json()["score_id"]
    assert "3 credits" in workspace["messages"][-1]["content"]


def test_replace_stops_a_running_split_and_uploads(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    first = _upload_score(test_client, session_id).json()["score_id"]
    _start_take(app, session_id, "running-split", status="running", kind="preprocess")

    def stop_job(job_id):
        app.state.job_store.update_job(job_id, status="cancelled", cancelledBy="user")
        return "stopped"

    monkeypatch.setattr(app.state.job_store, "stop_job", stop_job)
    monkeypatch.setattr(
        "src.backend.main._release_stopped_reservation",
        lambda **_: pytest.fail("a split has no take reservation to release"),
    )

    response = _replace_upload(test_client, session_id)
    assert response.status_code == 200
    assert response.json()["score_id"] != first
    assert app.state.job_store.get_job("running-split")["status"] == "cancelled"
    messages = test_client.get(f"/songs/{session_id}/workspace").json()["messages"]
    assert any(message["content"] == backend_message("job.stop_split_history") for message in messages)


@pytest.mark.parametrize("too_late", [False, True])
def test_replace_stops_a_sing_all_before_replacing_the_score(client, monkeypatch, too_late):
    test_client, _app = client
    session_id = _create_session(test_client)
    first = _upload_score(test_client, session_id).json()["score_id"]
    parent = {
        "jobId": "made-up-parent", "status": "running", "stage": "singing",
        "revision": 0, "items": [{"type": "take", "childJobId": "made-up-child",
                                  "status": "running" if too_late else "queued"}],
    }
    monkeypatch.setattr("src.backend.main.active_job_for_song", lambda *_: parent)
    monkeypatch.setattr("src.backend.main.load_sing_all_job", lambda *_: parent)
    def request_cancel(_job_id, fields, **_kwargs):
        parent.update(fields)
        return True
    monkeypatch.setattr("src.backend.main.apply_sing_all_job", request_cancel)
    async def reconcile(_self, _job_id):
        parent["status"] = "completed" if too_late else "cancelled"
    monkeypatch.setattr("src.backend.main.SingAllRunner.reconcile", reconcile)

    response = _replace_upload(test_client, session_id)
    if too_late:
        assert response.status_code == 409 and response.json()["code"] == "stop_too_late"
        assert _app.state.sessions._sessions[session_id].score_id == first
    else:
        assert response.status_code == 200
        assert response.json()["score_id"] != first
        assert parent["cancelRequested"] is True


@pytest.mark.parametrize("result,kind", [("too_late", "synthesis"), ("too_late", "preprocess")])
def test_replace_too_late_or_during_a_split_keeps_the_score(client, monkeypatch, result, kind):
    test_client, app = client
    session_id = _create_session(test_client)
    first = _upload_score(test_client, session_id).json()["score_id"]
    _start_take(app, session_id, "busy-job", status="running", kind=kind)
    monkeypatch.setattr(app.state.job_store, "stop_job", lambda job_id: result)

    response = _replace_upload(test_client, session_id)
    assert response.status_code == 409
    assert response.json() == {"code": "stop_too_late", "message": backend_message("upload.stop_too_late")}
    assert app.state.sessions._sessions[session_id].score_id == first
    assert app.state.job_store.get_job("busy-job")["status"] == "running"


def test_replace_with_a_bad_file_stops_nothing(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    _start_take(app, session_id, "running-take", status="running", kind="synthesis")
    monkeypatch.setattr(app.state.job_store, "stop_job",
                        lambda job_id: pytest.fail("a bad file must stop nothing"))

    response = test_client.post(
        f"/sessions/{session_id}/upload",
        files={"file": ("bad.txt", b"not a score", "text/plain")},
        data={"replace": "true"},
    )
    assert response.status_code == 400
    assert app.state.job_store.get_job("running-take")["status"] == "running"


# ---- The take hook ----------------------------------------------------------


def _run_take(app, session_id, job_id):
    asyncio.run(app.state.orchestrator._run_synthesis_job(
        session_id, {"parts": []}, {"voicebank": "Dummy"}, job_id, "test-user",
        input_path=None, storage_input_path=None, job_input_storage_path=None,
        output_storage_path=None,
    ))


def test_a_settled_take_is_recorded_without_any_page_following_it(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    _start_take(app, session_id, "inline-take", status="queued")

    # No chat stream and no progress polling: the take still lands in the player.
    _run_take(app, session_id, "inline-take")

    assert app.state.job_store.get_job("inline-take")["status"] == "completed"
    player = asyncio.run(app.state.sessions.get_player(session_id, "test-user"))
    assert [entry["sourceJobId"] for entry in player["vocals"]] == ["inline-take"]


def test_a_player_record_failure_never_fails_the_take(client, monkeypatch, caplog):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    _start_take(app, session_id, "take-with-a-broken-record", status="queued")

    async def broken(*args, **kwargs):
        raise RuntimeError("player record unavailable")

    monkeypatch.setattr(app.state.sessions, "record_take", broken)
    with caplog.at_level(logging.ERROR):
        _run_take(app, session_id, "take-with-a-broken-record")

    job = app.state.job_store.get_job("take-with-a-broken-record")
    assert job["status"] == "completed" and job["consumedCredits"] == 1
    assert "player_record_update_failed" in caplog.text


# ---- A job's audio file must be inside its own folder -----------------------


@pytest.mark.parametrize("client_with_env", [{"BACKEND_USE_STORAGE": "true"}], indirect=True)
def test_storage_audio_links_only_for_a_jobs_own_folder(client_with_env, caplog):
    test_client, app = client_with_env
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    own = f"sessions/test-user/{session_id}/jobs/good-take/output.mp3"
    take = _start_take(app, session_id, "good-take", outputPath=own,
                       audioUrl=f"/sessions/{session_id}/audio?file=output.mp3")
    asyncio.run(app.state.sessions.record_take(session_id, "good-take", take))
    outside = {
        "other-session": "sessions/test-user/another-song/jobs/other-session/output.mp3",
        "other-job": f"sessions/test-user/{session_id}/jobs/good-take/output.mp3",
        "dot-dot": f"sessions/test-user/{session_id}/jobs/dot-dot/../good-take/output.mp3",
    }
    for job_id, path in outside.items():
        _start_take(app, session_id, job_id, turn=f"turn-{job_id}", outputPath=path,
                    audioUrl=f"/sessions/{session_id}/audio?file=output.mp3")

    with caplog.at_level(logging.WARNING):
        for job_id in outside:
            progress = test_client.get(f"/sessions/{session_id}/progress?job_id={job_id}").json()
            assert "audio_url" not in progress, job_id
        good = test_client.get(f"/sessions/{session_id}/progress?job_id=good-take").json()
        assert "playback_token=" in good["audio_url"]
        workspace = test_client.get(f"/songs/{session_id}/workspace").json()
    jobs = {job["job_id"]: job for job in workspace["jobs"]}
    assert all("audio_url" not in jobs[job_id] for job_id in outside)
    assert "playback_token=" in jobs["good-take"]["audio_url"]
    assert [vocal["source_job_id"] for vocal in workspace["player"]["vocals"]] == ["good-take"]
    assert caplog.text.count("job_audio_path_rejected") >= len(outside)

    # The player leaves out a track whose job's path is rejected.
    bad = app.state.job_store.get_job("dot-dot")
    bad["audioTrack"]["key"] = "id:P2"
    bad["audioTrack"]["part_id"] = "P2"
    asyncio.run(app.state.sessions.record_take(session_id, "dot-dot", bad))
    workspace = test_client.get(f"/songs/{session_id}/workspace").json()
    assert [vocal["source_job_id"] for vocal in workspace["player"]["vocals"]] == ["good-take"]


def test_owned_job_path_rules():
    ids = {"user_id": "u", "session_id": "s", "job_id": "j"}
    assert songs.owned_job_path("sessions/u/s/jobs/j/output.mp3", **ids) == "sessions/u/s/jobs/j/output.mp3"
    for path in ("sessions/u/s/jobs/k/output.mp3", "sessions/u/t/jobs/j/output.mp3",
                 "sessions/u/s/jobs/j/../k/output.mp3", "sessions/u/s/jobs/j", None, 5):
        assert songs.owned_job_path(path, **ids) is None
    assert songs.owned_job_path("sessions/u/s/jobs/j/x", user_id="u", session_id="s/jobs", job_id="j") is None


@pytest.mark.parametrize(
    "summary,file_name,title",
    [
        ({"title": " A Title "}, "file.xml", "A Title"),
        ({"title": ""}, "My File.mxl", "My File"),
        ({}, None, songs.UNTITLED_SONG),
        ({"title": "x" * 200}, None, "x" * 120),
    ],
)
def test_song_title_from(summary, file_name, title):
    assert songs.song_title_from(summary, file_name) == title
