"""Share link previews and score thumbnails, with made-up scores, titles and accounts."""

import asyncio
import functools
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backend import share_preview
from src.backend.score_thumbnail import (
    HEIGHT, SMALL_HEIGHT, SMALL_WIDTH, WIDTH, load_thumbnail, store_thumbnail, stored_version,
    thumbnail_object_path,
)
from tests.test_backend_api import _create_session, _upload_score, client_with_env  # noqa: F401
from tests.test_share_backend import _share_firestore

FIXTURE = Path(__file__).parent / "fixtures" / "lyric_dash_melisma.musicxml"
SHELL = "<!doctype html><html><head><meta charset='utf-8'/><title>SightSinger.app</title></head><body><div id='root'></div></body></html>"


# --- The card's text ---


def test_description_is_song_info_with_spaces_for_line_breaks():
    assert share_preview.song_description({"description": "Line one\n\nline two"}) == "Line one line two"


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"composer": "Made-up Composer", "arranger": "Made-up Arranger"},
         f"Made-up Composer · Made-up Arranger · {share_preview.GENERIC_DESCRIPTION}"),
        ({"composer": "Made-up Composer"}, f"Made-up Composer · {share_preview.GENERIC_DESCRIPTION}"),
        ({}, share_preview.GENERIC_DESCRIPTION),
        (None, share_preview.GENERIC_DESCRIPTION),
    ],
)
def test_empty_description_is_built_from_the_credits(info, expected):
    assert share_preview.song_description(info) == expected


def test_long_description_is_cut_on_a_word():
    words = " ".join(["melody"] * 60)
    description = share_preview.song_description({"description": words})
    assert len(description) <= share_preview.DESCRIPTION_LIMIT + 1
    assert description.endswith("…") and not description[:-1].endswith(" ")
    assert description[:-1].split(" ")[-1] == "melody"


def test_tags_are_escaped_and_the_shell_title_is_replaced():
    tags = share_preview.song_tags(
        title='A "made-up" <song> & co', info={"description": "<b>bold</b>"},
        share_url="https://app.test/s/tok", image_url=None, base_url="https://app.test",
    )
    page = share_preview.inject(SHELL, tags)
    assert page.count("<title>") == 1
    assert "<title>A &quot;made-up&quot; &lt;song&gt; &amp; co · SightSinger</title>" in page
    assert 'content="&lt;b&gt;bold&lt;/b&gt;"' in page
    assert "<b>" not in page and "SightSinger.app" not in page
    assert f'content="https://app.test{share_preview.GENERIC_IMAGE_PATH}"' in page
    assert '<meta name="twitter:card" content="summary_large_image" />' in page
    assert page.index("og:title") < page.index("</head>")


def test_untitled_song_and_generic_card():
    assert share_preview.song_tags(title="  ", info={}, share_url="u", image_url="i",
                                   base_url="b")["title"] == "Untitled song · SightSinger"
    generic = share_preview.generic_tags(share_url="https://app.test/s/tok", base_url="https://app.test")
    assert generic["title"] == "SightSinger"


def test_app_shell_is_cached_for_a_minute(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            return SimpleNamespace(status_code=200, text=SHELL)

    monkeypatch.setattr(share_preview.httpx, "AsyncClient", FakeClient)
    monkeypatch.setitem(share_preview._shell_cache, "url", None)
    url = "https://app.test/app"
    assert asyncio.run(share_preview.app_shell(url, now=1000.0)) == SHELL
    assert asyncio.run(share_preview.app_shell(url, now=1030.0)) == SHELL
    assert len(calls) == 1
    asyncio.run(share_preview.app_shell(url, now=1061.0))
    assert len(calls) == 2


# --- The thumbnail ---


def test_thumbnail_storage_paths_and_version():
    assert thumbnail_object_path("song-a", 3, "large") == "thumbnails/song-a/v3.png"
    assert thumbnail_object_path("song-a", 3, "small") == "thumbnails/song-a/v3-small.png"
    assert stored_version({"thumbnail_version": "4"}) == 4
    assert stored_version({"thumbnail_version": "x"}) is None
    assert stored_version({}) is None and stored_version(None) is None


def test_thumbnail_round_trip_without_storage(tmp_path):
    settings = SimpleNamespace(backend_use_storage=False, storage_bucket="")
    store_thumbnail(settings, tmp_path, "song-a", 2, b"large-png", b"small-png")
    assert load_thumbnail(settings, tmp_path, "song-a", 2, "large") == b"large-png"
    assert load_thumbnail(settings, tmp_path, "song-a", 2, "small") == b"small-png"
    assert load_thumbnail(settings, tmp_path, "song-a", 3, "large") is None


def test_render_makes_both_sizes():
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    from PIL import Image
    import io

    from src.backend.score_thumbnail import render_thumbnail

    large, small = render_thumbnail(FIXTURE.read_text(encoding="utf-8"))
    assert Image.open(io.BytesIO(large)).size == (WIDTH, HEIGHT)
    assert Image.open(io.BytesIO(small)).size == (SMALL_WIDTH, SMALL_HEIGHT)


def test_render_works_off_the_main_thread():
    # The server renders in a worker thread, where Verovio's default resource path is wrong.
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    from src.backend.score_thumbnail import render_thumbnail

    large, _ = asyncio.run(asyncio.to_thread(render_thumbnail, FIXTURE.read_text(encoding="utf-8")))
    assert large.startswith(b"\x89PNG")


def test_concurrent_renders_match_and_convert_the_fonts_once():
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    from concurrent.futures import ThreadPoolExecutor

    import src.backend.score_thumbnail as score_thumbnail

    score_thumbnail._bundled_font_files.cache_clear()
    calls = []
    convert = score_thumbnail._bundled_font_files.__wrapped__

    @functools.lru_cache(maxsize=1)
    def counting():
        calls.append(1)
        return convert()

    original = score_thumbnail._bundled_font_files
    score_thumbnail._bundled_font_files = counting
    try:
        text = FIXTURE.read_text(encoding="utf-8")
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(score_thumbnail.render_thumbnail, [text] * 6))
    finally:
        score_thumbnail._bundled_font_files = original
    assert len(calls) == 1
    assert len({large for large, _ in results}) == 1


def test_local_writes_leave_no_half_files(tmp_path):
    settings = SimpleNamespace(backend_use_storage=False, storage_bucket="")
    store_thumbnail(settings, tmp_path, "song-a", 1, b"first", b"first-small")
    store_thumbnail(settings, tmp_path, "song-a", 1, b"second", b"second-small")
    folder = tmp_path / "thumbnails" / "song-a"
    assert sorted(path.name for path in folder.iterdir()) == ["v1-small.png", "v1.png"]
    assert (folder / "v1.png").read_bytes() == b"second"


def _long_score(measures: int) -> str:
    """The fixture with its first measure repeated, numbered 1..measures."""
    import copy
    import xml.etree.ElementTree as ET

    root = ET.fromstring(FIXTURE.read_bytes())
    part = root.find("part")
    first = part.find("measure")
    for measure in part.findall("measure"):
        part.remove(measure)
    for number in range(1, measures + 1):
        measure = copy.deepcopy(first)
        measure.set("number", str(number))
        if number > 1:
            for attributes in measure.findall("attributes"):
                measure.remove(attributes)
        part.append(measure)
    return ET.tostring(root, encoding="unicode")


def test_first_measures_keeps_the_limit_in_every_part():
    from src.backend.score_thumbnail import first_measures

    cut = first_measures(_long_score(120), 40)
    assert cut.count("<measure ") == 40
    assert first_measures("not xml") == "not xml"
    short = FIXTURE.read_text(encoding="utf-8")
    assert first_measures(short) == short  # nothing to cut: the file is passed on as it was
    timewise = "<score-timewise>" + "".join(f'<measure number="{n}"/>' for n in range(1, 61)) + "</score-timewise>"
    assert first_measures(timewise, 40).count("<measure ") == 40


def test_cutting_the_score_leaves_the_thumbnail_unchanged(monkeypatch):
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    import src.backend.score_thumbnail as score_thumbnail

    text = _long_score(120)
    cut_large, _ = score_thumbnail.render_thumbnail(text)
    monkeypatch.setattr(score_thumbnail, "MEASURE_LIMIT", 10_000)
    whole_large, _ = score_thumbnail.render_thumbnail(text)
    assert cut_large == whole_large


def test_render_refuses_a_file_that_is_not_a_score():
    pytest.importorskip("verovio")
    from src.backend.score_thumbnail import ThumbnailError, render_thumbnail

    with pytest.raises(ThumbnailError):
        render_thumbnail("not a score")


# --- The routes ---


def _shell(monkeypatch, html=SHELL):
    async def fake_shell(url, *, now=None):
        return html

    monkeypatch.setattr(share_preview, "app_shell", fake_shell)


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_upload_thumbnail_row_and_share_preview(client_with_env, firestore_emulator, monkeypatch):
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    _share_firestore(monkeypatch)
    _shell(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid, content=FIXTURE.read_bytes())

    row = http.get("/songs").json()["songs"][0]
    assert isinstance(row["thumbnail_version"], int)
    state = app.state.sessions._sessions[sid]
    before = state.last_active_at
    thumb = http.get(f"/songs/{sid}/thumbnail.png?size=small&v={row['thumbnail_version']}")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert thumb.headers["Cache-Control"] == "private, max-age=86400"
    assert state.last_active_at == before  # a thumbnail never moves the Song up My songs

    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]
    page = http.get(f"/s/{token}", headers={"authorization": ""})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert f'<meta property="og:title" content="{state.title} · SightSinger" />' in page.text
    image = f"/public/s/{token}/thumbnail.png?v={row['thumbnail_version']}"
    assert image in page.text and "<div id='root'></div>" in page.text
    public = http.get(image, headers={"authorization": ""})
    assert public.status_code == 200 and public.headers["content-type"] == "image/png"
    assert state.last_active_at == before

    http.delete(f"/songs/{sid}/share")
    off = http.get(f"/s/{token}", headers={"authorization": ""})
    assert off.status_code == 200 and state.title not in off.text
    assert '<meta property="og:title" content="SightSinger" />' in off.text
    assert http.get(image).status_code == 404


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_a_song_from_before_thumbnails_gets_one_on_first_request(client_with_env, firestore_emulator, monkeypatch):
    pytest.importorskip("verovio")
    pytest.importorskip("resvg_py")
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid, content=FIXTURE.read_bytes())
    state = app.state.sessions._sessions[sid]
    state.files.pop("thumbnail_version")
    assert http.get("/songs").json()["songs"][0]["thumbnail_version"] is None

    assert http.get(f"/songs/{sid}/thumbnail.png?size=small").status_code == 200
    assert http.get("/songs").json()["songs"][0]["thumbnail_version"] is not None


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_preview_without_the_app_page_falls_back(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    _shell(monkeypatch, html=None)
    http, _ = client_with_env
    page = http.get("/s/AAAAAAAAAAAAAAAAAAAAAA")
    assert page.status_code == 200
    assert '<meta property="og:title" content="SightSinger" />' in page.text
    assert "Open the song" in page.text


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_only_the_owner_gets_a_song_thumbnail(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid, content=FIXTURE.read_bytes())
    app.state.sessions._sessions[sid].user_id = "made-up-other-user"
    assert http.get(f"/songs/{sid}/thumbnail.png").status_code == 404


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on", "BACKEND_REQUIRE_APP_CHECK": "true"}],
                         indirect=True)
def test_share_page_and_preview_image_need_no_app_check(client_with_env, firestore_emulator, monkeypatch):
    # A browser opening the link, or a chat app fetching its preview, can't send App Check.
    monkeypatch.setattr("src.backend.main.initialize_firebase_app", lambda: None)
    _share_firestore(monkeypatch)
    _shell(monkeypatch)
    http, _ = client_with_env
    token = "AAAAAAAAAAAAAAAAAAAAAA"
    assert http.get(f"/s/{token}").status_code == 200
    assert http.get(f"/public/s/{token}/thumbnail.png").status_code == 404  # an unknown link, not 401
    # The page's data, score and audio are fetched by the app, which sends App Check.
    assert http.get(f"/public/s/{token}").status_code == 401
    assert http.get(f"/public/s/{token}/score").status_code == 401
    assert http.post(f"/s/{token}").status_code == 401
