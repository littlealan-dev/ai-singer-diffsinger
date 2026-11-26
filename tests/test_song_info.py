"""Song Info: the score's credits, the producer, the license and the description.

Titles, names and descriptions here are made up (docs/specs/song-info-spec.md).
"""

import asyncio
import uuid

import pytest

from src.backend import sing_all_package, songs
from src.backend.message_catalog import backend_message
from src.musicxml.score_info import score_info_from_musicxml
from tests.test_backend_api import (  # noqa: F401 - pytest fixtures
    _create_session,
    _upload_score,
    client,
    client_with_env,
)
from tests.test_score_job_history import workspace  # noqa: F401 - pytest fixture
from tests.test_share_backend import _share_firestore
from tests.test_songs_backend import _start_take

OWNER = "song-owner"


def _xml(body: str) -> str:
    return f"<?xml version='1.0'?><score-partwise version='3.1'>{body}<part-list/></score-partwise>"


def _credit(kind: str, *words: str) -> str:
    return ("<credit page='1'>" + f"<credit-type>{kind}</credit-type>"
            + "".join(f"<credit-words>{word}</credit-words>" for word in words) + "</credit>")


def _info(tmp_path, body: str) -> dict:
    path = tmp_path / f"{uuid.uuid4().hex}.xml"
    path.write_text(_xml(body), encoding="utf-8")
    return score_info_from_musicxml(path)


def test_score_info_reads_the_creators_and_rights_first(tmp_path):
    info = _info(tmp_path, (
        "<work><work-title>Made-up Tune</work-title></work>"
        "<movement-title>Second Movement</movement-title>"
        "<identification>"
        "<creator type='composer'>Ann Example</creator>"
        "<creator type='composer'>Ben Example</creator>"
        "<creator type='lyricist'>Cat Example</creator>"
        "<creator type='poet'>Not Used</creator>"
        "<creator type='arranger'>Dan  Example</creator>"
        "<rights>Made-up rights 1</rights><rights>Made-up rights 2</rights>"
        "</identification>"
        + _credit("composer", "Printed Composer") + _credit("subtitle", "For made-up voices")
    ))
    # Typed creators fill the fields; the printed composer box differs from them, so it goes
    # to credits as printed.
    assert info == {
        "subtitle": "For made-up voices",
        "composer": "Ann Example, Ben Example",
        "lyricist": "Cat Example",
        "arranger": "Dan Example",
        "credits": "Printed Composer",
        "scoreCopyright": "Made-up rights 1; Made-up rights 2",
    }


def test_printed_credit_boxes_go_to_credits_as_printed(tmp_path):
    """A printed box's label says where it sits on the page, not what its text means: the
    composer box may read "Arranged by ...". Its text goes to credits, never to a field."""
    info = _info(tmp_path, (
        "<work><work-title>Made-up Tune</work-title></work>"
        "<movement-title>Made-up Subtitle</movement-title>"
        "<identification><creator type='poet'>Eve Example</creator></identification>"
        + _credit("composer", "Arranged by Fay Example")
        + _credit("lyricist", "Words and Music by Gus Example")
        + _credit("arranger", "Eve Example")  # repeats a typed creator: left out
        + _credit("rights", "Made-up printed rights")
    ))
    assert info == {
        "subtitle": "Made-up Subtitle", "composer": "", "lyricist": "Eve Example", "arranger": "",
        "credits": "Arranged by Fay Example · Words and Music by Gus Example",
        "scoreCopyright": "Made-up printed rights",
    }


def test_score_info_is_empty_without_credits_or_for_a_bad_file(tmp_path):
    empty = {key: "" for key in songs.SCORE_INFO_FIELDS}
    # A movement title that is the only title is the title, not a subtitle.
    assert _info(tmp_path, "<movement-title>Only Title</movement-title>") == empty
    bad = tmp_path / "bad.xml"
    bad.write_text("<score-partwise>", encoding="utf-8")
    assert score_info_from_musicxml(bad) == empty


async def _upload(store, sid, *, score_info=None, producer=None, title="Made-up Tune"):
    score_id = uuid.uuid4().hex
    source = store.session_dir(sid) / f"{score_id}.xml"
    source.write_text("<score-partwise/>")
    await store.commit_uploaded_score(
        sid, score_id=score_id, score={"source_musicxml_path": str(source)},
        summary={"title": title, "parts": []},
        files={"musicxml_path": str(source.relative_to(store._project_root))},
        file_name="made-up.xml", score_info=score_info, producer=producer,
    )


def test_upload_fills_song_info_and_replace_keeps_the_owners_edits(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session(OWNER)).id
        await _upload(store, sid, score_info={"composer": "Ann Example", "subtitle": "First"},
                      producer="  Pat Producer ")
        info = (await store.get_snapshot(sid, OWNER))["song_info"]
        assert info == {
            "subtitle": "First", "composer": "Ann Example", "lyricist": "", "arranger": "",
            "credits": "", "scoreCopyright": "", "producer": "Pat Producer", "description": "",
            "license": "all_rights_reserved", "edited": [],
        }

        row = await store.update_song(sid, OWNER, info={"composer": "Own Composer",
                                                        "description": "Made-up notes."})
        assert row["info"]["composer"] == "Own Composer"
        await _upload(store, sid, score_info={"composer": "New Composer", "subtitle": "Second",
                                              "arranger": "New Arranger"}, producer="Someone Else")
        info = (await store.get_snapshot(sid, OWNER))["song_info"]
        # Unedited score fields refill; the owner's edits, the producer and the license stay.
        assert (info["subtitle"], info["arranger"]) == ("Second", "New Arranger")
        assert (info["composer"], info["description"]) == ("Own Composer", "Made-up notes.")
        assert (info["producer"], info["license"]) == ("Pat Producer", "all_rights_reserved")
        assert info["edited"] == ["composer", "description"]
        # The Share page reads the Song through read_shared: its info comes with it.
        shared = await store.read_shared(sid)
        assert (shared.song_info["composer"], shared.song_info["description"]) == (
            "Own Composer", "Made-up notes.")

        other = (await store.create_session(OWNER)).id
        await _upload(store, other)
        assert (await store.get_snapshot(other, OWNER))["song_info"]["producer"] == ""
        with pytest.raises((KeyError, PermissionError)):
            await store.update_song(sid, "someone-else", info={"composer": "Taken"})

    asyncio.run(scenario())


def test_reading_song_info_alone_keeps_the_songs_place_in_my_songs(workspace):
    """The Share dialog reads the info of any Song in My songs: that read is no activity."""
    store, _, _ = workspace
    # An owner of its own: a long-running emulator keeps other tests' Songs.
    owner = f"song-owner-{uuid.uuid4().hex}"

    async def scenario():
        sid = (await store.create_session(owner)).id
        await _upload(store, sid, score_info={"composer": "Ann Example"})
        before = {row["id"]: row["last_active_at"] for row in await store.list_songs(owner, 10)}
        data, _ = await store.read_song_info(sid, owner)
        assert (data["title"], data["songInfo"]["composer"]) == ("Made-up Tune", "Ann Example")
        after = {row["id"]: row["last_active_at"] for row in await store.list_songs(owner, 10)}
        assert after == before
        with pytest.raises((KeyError, PermissionError)):
            await store.read_song_info(sid, "someone-else")

    asyncio.run(scenario())


def test_the_info_route_answers_the_workspaces_info(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    take = _start_take(app, session_id, "sung-take", voicebankName="Made-up Voice")
    asyncio.run(app.state.sessions.record_take(session_id, "sung-take", take))
    test_client.patch(f"/songs/{session_id}", json={"info": {"composer": "Ann Example"}})
    opened = test_client.get(f"/songs/{session_id}/workspace").json()["song"]["info"]
    before = test_client.get("/songs").json()["songs"]

    info = test_client.get(f"/songs/{session_id}/info")
    assert info.status_code == 200
    assert info.json() == opened
    assert info.json()["voices"] == ["Made-up Voice"]
    assert test_client.get("/songs").json()["songs"] == before
    missing = test_client.get("/songs/no-such-song/info")
    assert missing.status_code == 404
    assert missing.json()["code"] == "song_not_found"


def test_patch_edits_song_info_and_refuses_a_bad_field(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    composer = "<identification><creator type='composer'>Ann Example</creator></identification>"
    _upload_score(test_client, session_id, content=_xml(composer).encode())
    info = test_client.get(f"/songs/{session_id}/workspace").json()["song"]["info"]
    assert info == {
        "title": "Test", "subtitle": "", "composer": "Ann Example", "lyricist": "", "arranger": "",
        "credits": "", "score_copyright": "", "producer": "", "description": "", "license": "all_rights_reserved",
        "voices": [],
    }

    saved = test_client.patch(f"/songs/{session_id}", json={
        "title": "Made-up Title",
        "info": {"lyricist": "  Cat   Example ", "license": "cc_by_4",
                 "description": "  Line one.\r\nLine two.  "},
    })
    assert saved.status_code == 200
    body = saved.json()
    assert (body["title"], body["info"]["title"]) == ("Made-up Title", "Made-up Title")
    assert (body["info"]["lyricist"], body["info"]["license"]) == ("Cat Example", "cc_by_4")
    assert body["info"]["description"] == "Line one.\nLine two."

    for bad, field in (({"license": "mit"}, "license"), ({"producer": "x" * 121}, "producer"),
                       ({"voices": "Made-up voice"}, "voices"), ({"composer": 5}, "composer")):
        refused = test_client.patch(f"/songs/{session_id}", json={"info": bad})
        assert refused.status_code == 400
        assert refused.json() == {"code": "invalid_song_info", "field": field,
                                  "message": backend_message("song.invalid_song_info")}
    after = test_client.get(f"/songs/{session_id}/workspace").json()["song"]["info"]
    assert (after["license"], after["producer"], after["composer"]) == ("cc_by_4", "", "Ann Example")


def test_the_upload_answers_the_songs_info_so_the_page_can_show_it(client):
    """First upload and Replace both answer the same info as opening the Song; a new score has no voices."""
    test_client, app = client
    session_id = _create_session(test_client)
    composer = "<identification><creator type='composer'>Ann Example</creator></identification>"
    uploaded = _upload_score(test_client, session_id, content=_xml(composer).encode()).json()["song"]
    assert uploaded["info"] == test_client.get(f"/songs/{session_id}/workspace").json()["song"]["info"]
    assert (uploaded["info"]["composer"], uploaded["info"]["voices"]) == ("Ann Example", [])

    take = _start_take(app, session_id, "sung-take", voicebankName="Made-up Voice")
    asyncio.run(app.state.sessions.record_take(session_id, "sung-take", take))
    test_client.patch(f"/songs/{session_id}", json={"info": {"lyricist": "Cat Example"}})
    replaced = _upload_score(test_client, session_id, content=_xml(composer).encode()).json()["song"]
    assert replaced["info"] == test_client.get(f"/songs/{session_id}/workspace").json()["song"]["info"]
    assert (replaced["info"]["lyricist"], replaced["info"]["voices"]) == ("Cat Example", [])


def test_workspace_info_lists_the_voices_of_the_sung_tracks(client):
    """The voices come from the same usable tracks the player shows, read after them."""
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    take = _start_take(app, session_id, "sung-take", voicebankName="Made-up Voice")
    asyncio.run(app.state.sessions.record_take(session_id, "sung-take", take))
    workspace = test_client.get(f"/songs/{session_id}/workspace").json()
    assert len(workspace["player"]["vocals"]) == 1
    assert workspace["song"]["info"]["voices"] == ["Made-up Voice"]


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_the_share_page_shows_song_info(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, _ = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    http.patch(f"/songs/{sid}", json={"info": {"composer": "Ann Example", "license": "public_domain"}})
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    page = http.get(f"/public/s/{token}", headers={"authorization": ""}).json()
    assert page["info"]["composer"] == "Ann Example"
    assert page["info"]["license"] == "public_domain"
    assert page["info"]["voices"] == page["credits"]["voices"] == []


def test_readme_lines_follow_the_spec_order_and_skip_empty_fields():
    info = {
        "subtitle": "For made-up voices", "composer": "Ann Example", "lyricist": "Cat Example",
        "arranger": "Dan Example", "credits": "Arranged by Made-up Name",
        "producer": "Pat Producer", "scoreCopyright": "Made-up rights",
        "description": "Line one.\nLine two.", "license": "cc_by_4", "edited": [],
    }
    assert sing_all_package._song_info_lines(info, ["Voice A", "Voice B"]) == [
        "Subtitle: For made-up voices", "Composer: Ann Example", "Lyrics by: Cat Example",
        "Arranger: Dan Example", "Credits: Arranged by Made-up Name", "Produced by: Pat Producer",
        "Voices: Voice A, Voice B",
        "License: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
        "Score copyright: Made-up rights", "Description:", "Line one.\nLine two.",
    ]
    # A Song from before Song Info: only the default license.
    assert sing_all_package._song_info_lines(None, []) == ["License: All rights reserved"]
