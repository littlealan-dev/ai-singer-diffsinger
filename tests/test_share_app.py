"""The share service serves a share exactly as the API does, without loading the API's heavy code."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.backend import share_app, share_preview
from tests.test_backend_api import _create_session, _upload_score, client_with_env  # noqa: F401
from tests.test_share_backend import _share_firestore
from tests.test_share_preview import SHELL
from tests.test_songs_backend import _start_take

ROOT = Path(__file__).resolve().parent.parent
HEAVY = ("music21", "nltk", "g2p_en", "scipy", "onnxruntime", "verovio", "src.backend.main",
         "src.backend.orchestrator", "src.backend.mcp_client", "src.backend.llm_client")


def test_share_service_loads_none_of_the_api_heavy_code():
    code = f"import sys, src.backend.share_app; print([m for m in {HEAVY!r} if m in sys.modules])"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "[]"


def _share_service(monkeypatch, app):
    """The share service, reading the same Song and job stores as this API."""
    monkeypatch.setattr(share_app, "SessionStore", lambda **_: app.state.sessions)
    monkeypatch.setattr(share_app, "JobStore", lambda: app.state.job_store)
    return TestClient(share_app.create_share_app())


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_service_serves_the_same_share_as_the_api(client_with_env, firestore_emulator, monkeypatch):
    _share_firestore(monkeypatch)
    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    audio = app.state.sessions.session_dir(sid) / "take.wav"
    audio.write_bytes(b"abcdefgh")
    take = _start_take(app, sid, "made-up-take", outputPath=str(audio.relative_to(app.state.settings.project_root)))
    app.state.sessions._sessions[sid].score_summary["parts"][0]["take_signature"] = "original-music"
    take["audioTrack"]["take_signature"] = "original-music"
    asyncio.run(app.state.sessions.record_take(sid, "made-up-take", take))
    token = http.post(f"/songs/{sid}/share", json={"notice_accepted": True}).json()["url"].rsplit("/", 1)[-1]

    async def shell(url, *, now=None):
        return SHELL

    monkeypatch.setattr(share_preview, "app_shell", shell)
    share = _share_service(monkeypatch, app)
    anonymous = {"authorization": ""}
    base = f"/public/s/{token}"
    page = share.get(base)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert page.json() == http.get(base, headers=anonymous).json()
    [vocal] = page.json()["player"]["vocals"]
    for path in (f"/s/{token}", f"{base}/score", f"{base}/midi", f"{base}/thumbnail.png", vocal["audio_url"]):
        ours, api = share.get(path), http.get(path, headers=anonymous)
        assert (ours.status_code, ours.content) == (api.status_code, api.content), path
    assert "og:title" in share.get(f"/s/{token}").text
    ranged = share.get(vocal["audio_url"], headers={"range": "bytes=2-4"})
    assert ranged.status_code == 206 and ranged.content == b"cde"

    http.delete(f"/songs/{sid}/share")
    off = share.get(base)
    assert off.status_code == 404 and off.json()["code"] == "share_off"


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on", "BACKEND_REQUIRE_APP_CHECK": "true"}],
                         indirect=True)
def test_share_service_asks_app_check_for_the_data_but_not_the_page(client_with_env, monkeypatch):
    _, app = client_with_env
    share = _share_service(monkeypatch, app)
    token = "a" * 22
    assert share.get(f"/public/s/{token}").status_code == 401
    assert share.get(f"/public/s/{token}/score").status_code == 401
    assert share.get(f"/public/s/{token}/thumbnail.png").json()["code"] == "share_off"
    assert share.get("/readyz").json()["ready"] is True
