import asyncio
import copy
import io
import os
import shutil
import time
import uuid
import json
import zipfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.score import parse_score
from src.backend.main import _require_app_check, create_app
from src.backend.llm_client import StaticLlmClient
from src.backend.orchestrator import (
    MISSING_ORIGINAL_SCORE_MESSAGE,
    ToolExecutionResult,
    WorkflowCandidate,
)
from src.backend.llm_prompt import LlmResponse, ToolCall
from src.backend.session import SessionStore
from src.mcp.resolve import PROJECT_ROOT, resolve_project_path
from src.backend.credits import UserCredits
from src.backend.credits import (
    ReleaseCreditsResult,
    ReserveCreditsResult,
    SettleCreditsResult,
)


def _make_router_call_tool():
    def _call_tool(name, arguments):
        if name == "parse_score":
            musicxml_path = arguments.get("musicxml_path")
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"part_id": "P1", "part_name": "Solo", "notes": []}],
                "structure": {},
                "voice_part_signals": {
                    "has_multi_voice_parts": False,
                    "has_missing_lyric_voice_parts": False,
                    "parts": [
                        {
                            "part_index": 0,
                            "multi_voice_part": False,
                            "missing_lyric_voice_parts": [],
                        }
                    ],
                },
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0, "part_name": "Solo"}],
                    "available_verses": [],
                },
                "source_musicxml_path": str(musicxml_path) if musicxml_path else None,
            }
        if name == "preprocess_voice_parts":
            return {
                "status": "ready",
                "score": arguments.get("score", {}),
                "part_index": 0,
            }
        if name == "synthesize":
            return {"waveform": [0.0, 0.1, 0.0], "sample_rate": 44100}
        if name == "save_audio":
            rel_path = arguments["output_path"]
            abs_path = PROJECT_ROOT / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(b"RIFFTESTDATA")
            if arguments.get("format") == "mp3" and arguments.get("keep_wav"):
                wav_path = abs_path.with_suffix(".wav")
                wav_path.write_bytes(b"RIFFTESTDATA")
            return {
                "audio_base64": "",
                "duration_seconds": 0.01,
                "sample_rate": arguments.get("sample_rate", 44100),
            }
        if name == "list_voicebanks":
            return [{"id": "Dummy", "name": "Dummy", "path": "assets/voicebanks/Dummy"}]
        if name == "get_voicebank_info":
            return {
                "name": "Dummy",
                "languages": [],
                "has_duration_model": False,
                "has_pitch_model": False,
                "has_variance_model": False,
                "speakers": [],
                "sample_rate": 44100,
                "hop_size": 512,
                "use_lang_id": False,
            }
        raise AssertionError(f"Unexpected tool call: {name}")

    return _call_tool


def _auth_headers(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


def _prepare_app(monkeypatch, overrides=None):
    data_dir = Path("tests/output/backend_data") / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    fake_jobs: dict[str, dict] = {}
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    import src.backend.main as backend_main
    backend_main._PLAYBACK_SECRET_CACHE.clear()
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr(
        "src.backend.main.verify_id_token_claims",
        lambda token: {"uid": "test-user", "email": "test@example.com"},
    )
    monkeypatch.setattr(
        "src.backend.credits.get_or_create_credits",
        lambda user_id, user_email: UserCredits(
            balance=9999,
            reserved=0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            overdrafted=False,
        ),
    )
    monkeypatch.setattr(
        "src.backend.credits.reserve_credits",
        lambda *_, **__: ReserveCreditsResult(status="reserved", estimated_credits=1),
    )
    monkeypatch.setattr(
        "src.backend.credits.release_credits",
        lambda *_, **__: ReleaseCreditsResult(status="released"),
    )
    monkeypatch.setattr(
        "src.backend.credits.settle_credits",
        lambda *_, **__: SettleCreditsResult(status="settled", actual_credits=1, overdrafted=False),
    )
    monkeypatch.setattr("src.backend.main.upload_file", lambda *_, **__: None)
    monkeypatch.setattr(
        "src.backend.main.download_bytes",
        lambda bucket, storage_path: f"storage:{storage_path}".encode("utf-8"),
    )
    monkeypatch.setattr("src.backend.orchestrator.upload_file", lambda *_, **__: None)
    monkeypatch.setattr("src.backend.orchestrator.copy_blob", lambda *_, **__: None)

    def _fake_create_job(
        self,
        *,
        job_id: str,
        user_id: str,
        session_id: str,
        status: str,
        input_path: str | None = None,
        render_type: str | None = None,
    ) -> None:
        payload = {
            "userId": user_id,
            "sessionId": session_id,
            "status": status,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        if input_path:
            payload["inputPath"] = input_path
        if render_type:
            payload["renderType"] = render_type
        fake_jobs[job_id] = payload

    def _fake_update_job(self, job_id: str, **fields) -> None:
        payload = fake_jobs.setdefault(job_id, {})
        payload.update(fields)
        payload["updatedAt"] = datetime.now(timezone.utc).isoformat()

    def _fake_get_latest_job_by_session(self, *, user_id: str, session_id: str):
        matches = [
            (job_id, payload)
            for job_id, payload in fake_jobs.items()
            if payload.get("userId") == user_id and payload.get("sessionId") == session_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[1].get("updatedAt", ""))

    def _fake_clear_jobs_for_session(self, *, user_id: str, session_id: str) -> None:
        to_delete = [
            job_id
            for job_id, payload in fake_jobs.items()
            if payload.get("userId") == user_id and payload.get("sessionId") == session_id
        ]
        for job_id in to_delete:
            fake_jobs.pop(job_id, None)

    monkeypatch.setattr("src.backend.job_store.JobStore.create_job", _fake_create_job)
    monkeypatch.setattr("src.backend.job_store.JobStore.update_job", _fake_update_job)
    monkeypatch.setattr(
        "src.backend.job_store.JobStore.get_latest_job_by_session",
        _fake_get_latest_job_by_session,
    )
    monkeypatch.setattr(
        "src.backend.job_store.JobStore.clear_jobs_for_session",
        _fake_clear_jobs_for_session,
    )
    if overrides:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
    if overrides and str(overrides.get("APP_ENV", "")).lower() == "prod":
        monkeypatch.setattr("src.backend.main.FirestoreSessionStore", SessionStore)
    app = create_app()
    app.state.router.call_tool = _make_router_call_tool()
    return app, data_dir


@pytest.fixture
def client(monkeypatch):
    app, data_dir = _prepare_app(monkeypatch)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        yield test_client, app
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def client_with_env(monkeypatch, request):
    overrides = getattr(request, "param", {}) or {}
    app, data_dir = _prepare_app(monkeypatch, overrides=overrides)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        yield test_client, app
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


def _create_session(test_client):
    response = test_client.post("/sessions")
    assert response.status_code == 200
    payload = response.json()
    assert "session_id" in payload
    return payload["session_id"]


def _upload_score(test_client, session_id, filename="score.xml"):
    xml = b"<score-partwise version='3.1'></score-partwise>"
    content = xml
    content_type = "application/xml"
    if filename.endswith(".mxl"):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(
                "META-INF/container.xml",
                (
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    "<container version='1.0' "
                    "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                    "<rootfiles><rootfile full-path='score.xml' "
                    "media-type='application/vnd.recordare.musicxml+xml'/>"
                    "</rootfiles></container>"
                ),
            )
            zf.writestr("score.xml", xml)
        content = archive.getvalue()
        content_type = "application/vnd.recordare.musicxml"
    files = {"file": (filename, content, content_type)}
    return test_client.post(f"/sessions/{session_id}/upload", files=files)


def _build_mxl_archive(
    *,
    score_xml: bytes = b"<score-partwise version='3.1'></score-partwise>",
    container_xml: bytes | None = None,
) -> bytes:
    if container_xml is None:
        container_xml = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<container version='1.0' "
            b"xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
            b"<rootfiles><rootfile full-path='score.xml' "
            b"media-type='application/vnd.recordare.musicxml+xml'/>"
            b"</rootfiles></container>"
        )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("score.xml", score_xml)
    return archive.getvalue()


def _wait_for_progress(test_client, progress_url, timeout_seconds=10.0):
    deadline = time.time() + timeout_seconds
    last_payload = None
    while time.time() < deadline:
        response = test_client.get(progress_url)
        assert response.status_code == 200
        payload = response.json()
        last_payload = payload
        if payload.get("status") in ("done", "error"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for progress: {last_payload}")


def _resolve_review_response(test_client, payload):
    if payload["type"] == "chat_progress":
        resolved = _wait_for_progress(test_client, payload["progress_url"])
        assert bool(resolved.get("review_required")) is True
        return resolved
    assert payload["type"] == "chat_text"
    assert bool(payload.get("review_required")) is True
    return payload


def test_create_session_returns_id(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    assert isinstance(session_id, str)
    assert len(session_id) > 0


def test_missing_auth_header_returns_401(monkeypatch):
    app, data_dir = _prepare_app(monkeypatch)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        response = test_client.post("/sessions")
        assert response.status_code == 401
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_query_token_auth_is_rejected(monkeypatch):
    app, data_dir = _prepare_app(monkeypatch)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        response = test_client.post("/sessions?id_token=test-token")
        assert response.status_code == 401
        assert response.json()["detail"] == "Missing Authorization header."
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.mark.parametrize(
    "client_with_env",
    [{"LLM_MAX_MESSAGE_CHARS": "5"}],
    indirect=True,
)
def test_rejects_too_long_message(client_with_env):
    test_client, _ = client_with_env
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    _upload_score(test_client, session_id)
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "too long"}
    )
    assert response.status_code == 400


def test_upload_musicxml_parses_and_saves(client):
    test_client, app = client
    session_id = _create_session(test_client)
    response = _upload_score(test_client, session_id)
    assert response.status_code == 200
    payload = response.json()
    assert payload["parsed"] is True
    assert payload["session_id"] == session_id
    current_score = payload["current_score"]
    assert current_score["version"] == 1
    assert "score" in current_score
    score_path = app.state.settings.data_dir / "sessions" / session_id / "score.xml"
    assert score_path.exists()
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert snapshot["original_score"] == current_score["score"]


def test_upload_resets_previous_score_specific_state(client):
    test_client, app = client
    session_id = _create_session(test_client)

    first_upload = _upload_score(test_client, session_id)
    assert first_upload.status_code == 200

    asyncio.run(app.state.sessions.append_history(session_id, "user", "old message"))
    asyncio.run(
        app.state.sessions.append_preprocess_plan(session_id, {"attempt": 1, "song": "old"})
    )
    asyncio.run(
        app.state.sessions.append_preprocess_attempt_summary(
            session_id, {"attempt_number": 1, "quality_class": 2}
        )
    )
    asyncio.run(
        app.state.sessions.set_last_preprocess_plan(
            session_id, {"targets": [{"target": {"part_index": 0, "voice_part_id": "old"}}]}
        )
    )
    session_dir = app.state.sessions.session_dir(session_id)
    audio_path = session_dir / "old.wav"
    audio_path.write_bytes(b"RIFFTESTDATA")
    asyncio.run(app.state.sessions.set_audio(session_id, audio_path, 0.01))
    derived_path = session_dir / "score.derived_old.xml"
    derived_path.write_text("<score-partwise version='3.1'></score-partwise>", encoding="utf-8")
    progress_path = app.state.sessions.progress_path(session_id)
    progress_path.write_text('{"status":"running"}', encoding="utf-8")
    app.state.job_store.create_job(
        job_id="old-job",
        user_id="test-user",
        session_id=session_id,
        status="running",
    )

    second_upload = _upload_score(test_client, session_id, filename="replacement.xml")
    assert second_upload.status_code == 200

    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert snapshot["history"] == []
    assert snapshot["preprocess_plan_history"] == []
    assert snapshot["preprocess_attempt_history"] == []
    assert snapshot["last_preprocess_plan"] is None
    assert snapshot["current_audio"] is None
    assert snapshot["score_summary"]["title"] == "Test"
    assert snapshot["current_score"]["version"] == 1
    assert set(snapshot["files"].keys()) >= {
        "musicxml_path",
        "uploaded_musicxml_path",
        "musicxml_name",
    }

    assert not derived_path.exists()
    assert not audio_path.exists()
    assert not progress_path.exists()
    assert (session_dir / "score.xml").exists()

    latest = app.state.job_store.get_latest_job_by_session(
        user_id="test-user",
        session_id=session_id,
    )
    assert latest is None


def test_upload_rejects_invalid_extension_without_resetting_session(client):
    test_client, app = client
    session_id = _create_session(test_client)

    first_upload = _upload_score(test_client, session_id)
    assert first_upload.status_code == 200

    asyncio.run(app.state.sessions.append_history(session_id, "user", "keep this"))
    app.state.job_store.create_job(
        job_id="existing-job",
        user_id="test-user",
        session_id=session_id,
        status="running",
    )

    response = test_client.post(
        f"/sessions/{session_id}/upload",
        files={"file": ("notes.txt", b"not musicxml", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only .xml or .mxl files are supported."

    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert snapshot["history"] == [{"role": "user", "content": "keep this"}]
    assert snapshot["current_score"]["version"] == 1
    assert snapshot["files"]["musicxml_name"] == "score.xml"

    latest = app.state.job_store.get_latest_job_by_session(
        user_id="test-user",
        session_id=session_id,
    )
    assert latest is not None
    assert latest[0] == "existing-job"


def test_get_score_returns_derived_musicxml_after_preprocess_review(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    derived_path = app.state.settings.data_dir / "sessions" / session_id / "derived.xml"
    derived_xml = "<score-partwise version=\"4.0\"><part-list/></score-partwise>"
    derived_path.write_text(derived_xml, encoding="utf-8")

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            score = dict(arguments.get("score", {}))
            score["source_musicxml_path"] = str(derived_path)
            return {
                "status": "ready",
                "score": score,
                "part_index": 0,
                "modified_musicxml_path": str(derived_path),
            }
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"preprocess_voice_parts","arguments":{"request":{"plan":{"targets":[{"target":{"part_index":0,"voice_part_id":"soprano"},"sections":[{"start_measure":1,"end_measure":1,"mode":"derive","melody_source":{"part_index":0,"voice_part_id":"soprano"}}]}]}}}}],'
            '"final_message":"Please review the derived score.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "sing soprano"}
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, chat_payload["progress_url"])
    assert progress_payload["status"] == "done"
    assert bool(progress_payload.get("review_required")) is True

    score_response = test_client.get(f"/sessions/{session_id}/score")
    assert score_response.status_code == 200
    assert score_response.text == derived_xml


def test_get_score_returns_derived_musicxml_after_reviewable_validation_candidate(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    derived_path = app.state.settings.data_dir / "sessions" / session_id / "derived-review.xml"
    derived_xml = "<score-partwise version=\"4.0\"><part-list/><part id=\"P_DERIVED\"/></score-partwise>"
    derived_path.write_text(derived_xml, encoding="utf-8")

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            return {
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Please review the best attempt so far.",
                "part_index": 0,
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_name": "Lyric Coverage Needs Review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [7, 18],
                        "impacted_ranges": [{"start": 7, "end": 7}, {"start": 18, "end": 18}],
                    }
                ],
                "validation": {
                    "word_lyric_coverage_ratio": 0.6,
                    "missing_lyric_sung_note_count": 4,
                    "unresolved_measures": [7, 18],
                },
                "failing_ranges": [{"start": 7, "end": 7}, {"start": 18, "end": 18}],
                "review_materialization": {"candidate": "best-valid"},
            }
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    import src.backend.orchestrator as orchestrator_module

    original_finalize = orchestrator_module.finalize_review_materialization

    def fake_finalize_review_materialization(payload):
        assert payload == {"candidate": "best-valid"}
        return {
            "status": "ready",
            "score": {
                "title": "Test",
                "parts": [{"notes": [], "part_id": "P_DERIVED", "part_name": "Tenor"}],
                "source_musicxml_path": str(derived_path),
            },
            "modified_musicxml_path": str(derived_path),
            "part_index": 0,
        }

    orchestrator_module.finalize_review_materialization = fake_finalize_review_materialization
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"preprocess_voice_parts","arguments":{"request":{"plan":{"targets":[{"target":{"part_index":0,"voice_part_id":"tenor"},"sections":[{"start_measure":1,"end_measure":1,"mode":"derive","melody_source":{"part_index":0,"voice_part_id":"tenor"}}]}]}}}}],'
            '"final_message":"I\\u0027ll prepare the tenor line and stop for review if coverage is incomplete.","include_score":false}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    try:
        chat_response = test_client.post(
            f"/sessions/{session_id}/chat", json={"message": "sing tenor"}
        )
        assert chat_response.status_code == 200
        chat_payload = chat_response.json()
        assert chat_payload["type"] == "chat_progress"

        progress_payload = _wait_for_progress(test_client, chat_payload["progress_url"])
        assert progress_payload["status"] == "done"
        assert bool(progress_payload.get("review_required")) is True

        score_response = test_client.get(f"/sessions/{session_id}/score")
        assert score_response.status_code == 200
        assert score_response.text == derived_xml
    finally:
        orchestrator_module.finalize_review_materialization = original_finalize


def test_get_score_rejects_out_of_root_payload_path(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    forbidden_path = Path("/tmp/sig14-outside.xml")
    forbidden_path.write_text("<score-partwise version=\"4.0\"/>", encoding="utf-8")
    try:
        snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
        current_score = dict(snapshot["current_score"]["score"])
        current_score["source_musicxml_path"] = str(forbidden_path)
        asyncio.run(app.state.sessions.set_score(session_id, current_score))

        score_response = test_client.get(f"/sessions/{session_id}/score")
        assert score_response.status_code == 403
        assert score_response.json()["detail"] == "Score path is outside allowed roots."
    finally:
        forbidden_path.unlink(missing_ok=True)


def test_get_score_rejects_out_of_root_fallback_path(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    forbidden_path = Path("/tmp/sig14-fallback-outside.xml")
    forbidden_path.write_text("<score-partwise version=\"4.0\"/>", encoding="utf-8")
    try:
        snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
        current_score = dict(snapshot["current_score"]["score"])
        current_score.pop("source_musicxml_path", None)
        asyncio.run(app.state.sessions.set_score(session_id, current_score))
        asyncio.run(app.state.sessions.set_metadata(session_id, "musicxml_path", str(forbidden_path)))

        score_response = test_client.get(f"/sessions/{session_id}/score")
        assert score_response.status_code == 403
        assert score_response.json()["detail"] == "Score path is outside allowed roots."
    finally:
        forbidden_path.unlink(missing_ok=True)


def test_get_score_allows_relative_fallback_within_data_dir(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    current_score = dict(snapshot["current_score"]["score"])
    current_score.pop("source_musicxml_path", None)
    asyncio.run(app.state.sessions.set_score(session_id, current_score))
    safe_path = app.state.settings.data_dir / "safe-score.xml"
    rel_safe_path = str(safe_path.relative_to(app.state.settings.project_root))
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    safe_xml = "<score-partwise version=\"4.0\"><part-list/></score-partwise>"
    safe_path.write_text(safe_xml, encoding="utf-8")
    try:
        asyncio.run(app.state.sessions.set_metadata(session_id, "musicxml_path", rel_safe_path))
        score_response = test_client.get(f"/sessions/{session_id}/score")
        assert score_response.status_code == 200
        assert score_response.text == safe_xml
    finally:
        safe_path.unlink(missing_ok=True)


def test_chat_returns_progress_immediately_for_preprocess(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"preprocess_voice_parts","arguments":{"request":{"plan":{"targets":[{"target":{"part_index":0,"voice_part_id":"soprano"},"sections":[{"start_measure":1,"end_measure":1,"mode":"derive","melody_source":{"part_index":0,"voice_part_id":"soprano"}}]}]}}}}],'
            '"final_message":"I\\u0027m splitting the requested part now and will let you know when the derived score is ready to review.","include_score":false}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "sing soprano"}
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload["type"] == "chat_progress"
    assert "splitting the requested part" in chat_payload["message"].lower()
    assert chat_payload["progress_url"].startswith(f"/sessions/{session_id}/progress")


def test_repreprocess_uses_original_uploaded_score_context(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    original_path = app.state.settings.data_dir / "sessions" / session_id / "score.xml"
    derived_path = app.state.settings.data_dir / "sessions" / session_id / "derived.xml"
    asyncio.run(
        app.state.sessions.set_original_score(
            session_id,
            {
                "title": "Test",
                "parts": [{"notes": []}],
                "source_musicxml_path": str(original_path),
            },
        )
    )
    derived_path.write_text("<score-partwise version=\"4.0\"><part-list/></score-partwise>", encoding="utf-8")
    parse_score_calls = []

    def call_tool(name, arguments):
        if name == "parse_score":
            parse_score_calls.append(dict(arguments))
            verse_number = arguments.get("verse_number", "1")
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"notes": []}],
                "structure": {},
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [],
                    "available_verses": ["1"],
                },
                "selected_verse_number": str(verse_number),
                "source_musicxml_path": str(original_path),
            }
        if name == "preprocess_voice_parts":
            score = dict(arguments.get("score", {}))
            derived_score = dict(score)
            derived_score["source_musicxml_path"] = str(derived_path)
            return {
                "status": "ready",
                "score": derived_score,
                "part_index": 0,
                "modified_musicxml_path": str(derived_path),
            }
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"preprocess_voice_parts","arguments":{"request":{"plan":{"targets":[{"target":{"part_index":0,"voice_part_id":"soprano"},"sections":[{"start_measure":1,"end_measure":1,"mode":"derive","melody_source":{"part_index":0,"voice_part_id":"soprano"}}]}]}}}}],'
            '"final_message":"Please review the derived score.","include_score":false}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    first_chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "sing soprano"}
    )
    assert first_chat_response.status_code == 200
    first_payload = first_chat_response.json()
    assert first_payload["type"] == "chat_progress"
    first_progress = _wait_for_progress(test_client, first_payload["progress_url"])
    assert bool(first_progress.get("review_required")) is True

    second_chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "regenerate soprano with revised lyrics"}
    )
    assert second_chat_response.status_code == 200
    second_payload = second_chat_response.json()
    assert second_payload["type"] == "chat_progress"
    second_progress = _wait_for_progress(test_client, second_payload["progress_url"])
    assert bool(second_progress.get("review_required")) is True

    assert len(parse_score_calls) == 0
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert snapshot["original_score"]["source_musicxml_path"] == str(original_path)
    assert snapshot["current_score"]["score"]["source_musicxml_path"] == str(derived_path)
    assert len(snapshot["preprocess_plan_history"]) == 0
    assert snapshot["last_preprocess_plan"] is not None
    latest_plan = snapshot["last_preprocess_plan"]
    assert latest_plan["targets"][0]["target"]["voice_part_id"] == "soprano"


def test_orchestrator_uses_original_score_for_preprocess_planning(client):
    _, app = client
    orchestrator = app.state.orchestrator
    original_score = {
        "source_musicxml_path": "/tmp/original.xml",
        "voice_part_signals": {"source": "original"},
    }
    current_score = {
        "source_musicxml_path": "/tmp/derived.xml",
        "voice_part_signals": {"source": "derived"},
        "voice_part_transforms": {"x": {}},
    }
    snapshot = {
        "original_score": original_score,
        "current_score": {"score": current_score, "version": 2},
    }

    planning_score = orchestrator._resolve_llm_planning_score(snapshot, current_score)

    assert planning_score == original_score


def test_orchestrator_errors_when_original_score_missing_for_preprocess_planning(client):
    _, app = client
    orchestrator = app.state.orchestrator
    current_score = {
        "source_musicxml_path": "/tmp/derived.xml",
        "voice_part_signals": {"source": "derived"},
    }
    snapshot = {
        "current_score": {"score": current_score, "version": 2},
    }

    with pytest.raises(ValueError, match="original parsed score baseline"):
        orchestrator._resolve_llm_planning_score(snapshot, current_score)


def test_chat_returns_explicit_error_when_original_score_missing_for_repreprocess(client):
    test_client, app = client
    session_id = _create_session(test_client)
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200

    asyncio.run(app.state.sessions.set_original_score(session_id, None))
    asyncio.run(
        app.state.sessions.set_score(
            session_id,
            {
                "title": "Derived",
                "parts": [{"notes": []}],
                "voice_part_transforms": {"x": {}},
                "source_musicxml_path": "/tmp/derived.xml",
            },
        )
    )

    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"preprocess_voice_parts","arguments":{"request":{"plan":{"targets":[{"target":{"part_index":0,"voice_part_id":"soprano"},"sections":[{"start_measure":1,"end_measure":1,"mode":"derive","melody_source":{"part_index":0,"voice_part_id":"soprano"}}]}]}}}}],'
            '"final_message":"Please review the derived score.","include_score":false}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "regenerate soprano"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_error"
    assert payload["message"] == MISSING_ORIGINAL_SCORE_MESSAGE


def test_orchestrator_stores_latest_preprocess_plan_in_prompt_context(client):
    _, app = client
    orchestrator = app.state.orchestrator
    original_score = {
        "source_musicxml_path": "/tmp/original.xml",
        "voice_part_signals": {"source": "original"},
    }
    current_score = {
        "source_musicxml_path": "/tmp/derived.xml",
        "voice_part_signals": {"source": "derived"},
        "voice_part_transforms": {"x": {}},
    }
    snapshot = {
        "original_score": original_score,
        "current_score": {"score": current_score, "version": 2},
        "last_preprocess_plan": {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [{"start_measure": 1, "end_measure": 6, "mode": "derive"}],
                }
            ]
        },
        "score_summary": {"title": "Test"},
        "history": [],
    }

    prompt = None

    class CapturingClient:
        def generate(self, system_prompt, history):
            nonlocal prompt
            prompt = system_prompt
            return '{"tool_calls":[],"final_message":"ok","include_score":false}'

    orchestrator._llm_client = CapturingClient()
    response, error = asyncio.run(orchestrator._decide_with_llm(snapshot, score_available=True))

    assert error is None
    assert response is not None
    assert prompt is not None
    assert "Latest attempted preprocess plan (if available):" in prompt
    assert '"voice_part_id": "voice part 1"' in prompt


def test_upload_rejects_invalid_extension(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    response = _upload_score(test_client, session_id, filename="score.txt")
    assert response.status_code == 400


def test_upload_accepts_mxl_extension(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    response = _upload_score(test_client, session_id, filename="score.mxl")
    assert response.status_code == 200


def test_upload_dispatches_mxl_normalization_via_to_thread(client, monkeypatch):
    test_client, _ = client
    session_id = _create_session(test_client)
    dispatched_calls: list[str] = []

    import src.backend.main as backend_main

    original_to_thread = backend_main.asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        dispatched_calls.append(getattr(func, "__name__", repr(func)))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(backend_main.asyncio, "to_thread", recording_to_thread)

    mxl_response = _upload_score(test_client, session_id, filename="score.mxl")
    assert mxl_response.status_code == 200
    assert "_normalize_uploaded_mxl" in dispatched_calls

    dispatched_calls.clear()
    xml_response = _upload_score(test_client, session_id, filename="score.xml")
    assert xml_response.status_code == 200
    assert "_normalize_uploaded_mxl" not in dispatched_calls


def test_upload_rejects_malformed_mxl_archive(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    response = test_client.post(
        f"/sessions/{session_id}/upload",
        files={"file": ("score.mxl", b"not-a-zip-file", "application/vnd.recordare.musicxml")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid MXL archive."


@pytest.mark.parametrize(
    "client_with_env",
    [{"BACKEND_MAX_MXL_UNCOMPRESSED_MB": "1"}],
    indirect=True,
)
def test_upload_rejects_oversized_mxl_archive(client_with_env):
    test_client, _ = client_with_env
    session_id = _create_session(test_client)
    large_score = b"<score-partwise>" + (b"A" * (1024 * 1024 + 1)) + b"</score-partwise>"
    response = test_client.post(
        f"/sessions/{session_id}/upload",
        files={
            "file": (
                "score.mxl",
                _build_mxl_archive(score_xml=large_score),
                "application/vnd.recordare.musicxml",
            )
        },
    )
    assert response.status_code == 413
    assert "exceeds" in response.json()["detail"]


def test_upload_returns_score_summary_with_verses(client):
    test_client, app = client
    score_path = PROJECT_ROOT / "assets/test_data/o-holy-night.xml"
    if not score_path.exists():
        pytest.skip(f"Test score not found at {score_path}")

    def call_tool(name, arguments):
        if name == "parse_score":
            file_path = resolve_project_path(arguments["file_path"])
            return parse_score(
                file_path,
                part_id=arguments.get("part_id"),
                part_index=arguments.get("part_index"),
                expand_repeats=arguments.get("expand_repeats", False),
            )
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    session_id = _create_session(test_client)
    files = {"file": ("o-holy-night.xml", score_path.read_bytes(), "application/xml")}
    response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert response.status_code == 200
    payload = response.json()
    summary = payload.get("score_summary")
    assert summary is not None
    assert summary.get("parts")
    assert any(part.get("has_lyrics") for part in summary["parts"])
    assert "1" in summary.get("available_verses", [])


def test_build_workflow_candidate_classifies_reviewable_postflight_result(client):
    _, app = client
    orchestrator = app.state.orchestrator
    tool_result = ToolExecutionResult(
        score={"title": "Derived"},
        audio_response={"type": "chat_text", "message": ""},
        action_required_payload={
            "status": "action_required",
            "action": "validation_failed_needs_review",
            "message": "Coverage still needs review.",
            "failed_validation_rules": [
                {
                    "rule": "validation_failed_needs_review",
                    "rule_name": "Coverage Needs Review",
                    "rule_severity": "P1",
                    "rule_domain": "LYRIC",
                    "impacted_measures": [7, 8, 9],
                    "impacted_ranges": [{"start": 7, "end": 9}],
                }
            ],
        },
    )

    candidate = orchestrator._build_workflow_candidate(
        attempt_number=2,
        tool_result=tool_result,
        fallback_message="fallback",
    )

    assert candidate is not None
    assert candidate.structurally_valid is True
    assert candidate.review_required is True
    assert candidate.quality_class == 1
    assert candidate.structural_p1_measures == 0
    assert candidate.lyric_p1_measures == 3
    assert candidate.p2_measures == 0
    assert candidate.message == "Coverage still needs review."
    assert candidate.target_results == []


def test_build_workflow_candidate_uses_visible_targets_for_quality(client):
    _, app = client
    orchestrator = app.state.orchestrator
    tool_result = ToolExecutionResult(
        score={"title": "Derived"},
        audio_response={"type": "chat_text", "message": ""},
        action_required_payload={
            "status": "action_required",
            "action": "validation_failed_needs_review",
            "message": "Visible tenor still needs review.",
            "targets": [
                {
                    "target_voice_part_id": "voice part 1",
                    "visible": True,
                    "hidden_default_lane": False,
                    "issues": [
                        {
                            "rule": "validation_failed_needs_review",
                            "rule_severity": "P1",
                            "rule_domain": "LYRIC",
                            "impacted_measures": [7, 8, 9],
                            "impacted_ranges": [{"start": 7, "end": 9}],
                        }
                    ],
                },
                {
                    "target_voice_part_id": "voice part 3",
                    "visible": False,
                    "hidden_default_lane": True,
                    "issues": [],
                },
            ],
        },
    )

    candidate = orchestrator._build_workflow_candidate(
        attempt_number=2,
        tool_result=tool_result,
        fallback_message="fallback",
    )

    assert candidate is not None
    assert candidate.quality_class == 1
    assert candidate.lyric_p1_measures == 3
    assert candidate.structurally_valid is True
    assert len(candidate.target_results) == 2
    assert candidate.target_results[0]["quality_class"] == 1
    assert candidate.target_results[1]["quality_class"] == 3


def test_candidate_ranking_prefers_higher_quality_then_smaller_impact(client):
    _, app = client
    orchestrator = app.state.orchestrator
    class2_candidate = orchestrator._build_workflow_candidate(
        attempt_number=2,
        tool_result=ToolExecutionResult(
            score={"title": "Derived"},
            audio_response=None,
            followup_prompt=json.dumps(
                {
                    "status": "ready_with_warnings",
                    "warnings": [
                        {
                            "code": "partial_lyric_coverage",
                            "rule_metadata": {
                                "rule": "partial_lyric_coverage",
                                "rule_name": "Partial Lyric Coverage",
                                "rule_severity": "P2",
                                "rule_domain": "LYRIC",
                                "impacted_measures": [18],
                                "impacted_ranges": [{"start": 18, "end": 18}],
                            },
                        }
                    ],
                }
            ),
            review_required=True,
        ),
        fallback_message="fallback",
    )
    class1_candidate = orchestrator._build_workflow_candidate(
        attempt_number=1,
        tool_result=ToolExecutionResult(
            score={"title": "Derived"},
            audio_response={"type": "chat_text", "message": ""},
            action_required_payload={
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Needs review.",
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_name": "Coverage Needs Review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [7, 8],
                        "impacted_ranges": [{"start": 7, "end": 8}],
                    }
                ],
            },
        ),
        fallback_message="fallback",
    )

    assert class2_candidate is not None
    assert class1_candidate is not None
    assert orchestrator._candidate_is_better(class2_candidate, class1_candidate) is True
    assert orchestrator._candidate_is_better(class1_candidate, class2_candidate) is False


def test_workflow_returns_best_valid_candidate_when_followup_step_fails(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}

    async def fake_execute_tool_calls(*args, **kwargs):
        return ToolExecutionResult(
            score={"title": "Derived"},
            audio_response={"type": "chat_text", "message": ""},
            action_required_payload={
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Please review the best attempt so far.",
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_name": "Coverage Needs Review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [7, 18],
                        "impacted_ranges": [{"start": 7, "end": 7}, {"start": 18, "end": 18}],
                    }
                ],
            },
            followup_prompt=json.dumps({"status": "action_required"}),
        )

    async def fake_decide_followup_with_llm(*args, **kwargs):
        return None, "LLM request failed. Please try again."

    async def fake_get_snapshot(*args, **kwargs):
        return {"current_score": {"score": {"title": "Derived"}, "version": 2}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-1",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert response["type"] == "chat_text"
    assert response["message"] == "Please review the best attempt so far."
    assert response["review_required"] is True
    assert response["details"]["quality_class"] == 1
    assert response["details"]["issues"][0]["rule"] == "validation_failed_needs_review"
    assert response["current_score"] == {"score": {"title": "Derived"}, "version": 2}


def test_workflow_returns_best_invalid_error_when_no_structurally_valid_candidate_exists(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}

    async def fake_execute_tool_calls(*args, **kwargs):
        return ToolExecutionResult(
            score={"title": "Derived"},
            audio_response={"type": "chat_text", "message": ""},
            action_required_payload={
                "status": "action_required",
                "action": "structural_validation_failed",
                "message": "Derived section output is not synthesis-safe.",
                "validation": {
                    "structural": {
                        "max_simultaneous_notes": 2,
                        "simultaneous_conflict_count": 1,
                        "overlap_conflict_count": 0,
                    }
                },
                "failing_ranges": [{"start": 12, "end": 12}],
                "failed_validation_rules": [
                    {
                        "rule": "structural_validation_failed",
                        "rule_name": "Structural Validation Failed",
                        "rule_severity": "P0",
                        "rule_domain": "STRUCTURAL",
                        "impacted_measures": [12],
                        "impacted_ranges": [{"start": 12, "end": 12}],
                    }
                ],
            },
            followup_prompt=json.dumps({"status": "action_required"}),
        )

    async def fake_decide_followup_with_llm(*args, **kwargs):
        return None, "LLM request failed. Please try again."

    async def fake_get_snapshot(*args, **kwargs):
        return {"current_score": {"score": {"title": "Derived"}, "version": 2}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-2",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert response["type"] == "chat_error"
    assert response["message"] == "Unable to produce synthesis-safe monophonic output after 3 attempts."
    assert response["details"]["best_invalid_candidate"]["attempt_number"] == 1
    assert response["details"]["best_invalid_candidate"]["failing_ranges"] == [{"start": 12, "end": 12}]
    assert response["details"]["failed_validation_rules"][0]["rule"] == "structural_validation_failed"


def test_build_workflow_candidate_normalizes_postflight_severity_domain_keys(client):
    _, app = client
    orchestrator = app.state.orchestrator

    tool_result = ToolExecutionResult(
        score={"title": "Derived"},
        audio_response={"type": "chat_text", "message": ""},
        action_required_payload={
            "status": "action_required",
            "action": "validation_failed_needs_review",
            "message": "Lyric propagation did not meet minimum coverage.",
            "failed_validation_rules": [
                {
                    "rule": "validation_failed_needs_review",
                    "severity": "P1",
                    "domain": "LYRIC",
                    "impacted_measures": [1, 2, 3],
                }
            ],
        },
        followup_prompt=json.dumps({"status": "action_required"}),
    )

    candidate = orchestrator._build_workflow_candidate(
        attempt_number=1,
        tool_result=tool_result,
        fallback_message="fallback",
    )

    assert candidate is not None
    assert candidate.quality_class == 1
    assert candidate.lyric_p1_measures == 3
    assert candidate.structural_p1_measures == 0


def test_format_llm_error_returns_plain_timeout_message(client):
    _, app = client
    orchestrator = app.state.orchestrator

    assert (
        orchestrator._format_llm_error(RuntimeError("Gemini request timed out."))
        == "Gemini request timed out."
    )


def test_format_llm_error_returns_plain_non_json_message(client):
    _, app = client
    orchestrator = app.state.orchestrator

    assert (
        orchestrator._format_llm_error(RuntimeError("Gemini service unavailable."))
        == "Gemini service unavailable."
    )


def test_workflow_keeps_better_later_valid_candidate_when_followup_fails(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}
    attempts = {"count": 0}

    async def fake_execute_tool_calls(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ToolExecutionResult(
                score={"title": "Derived-1"},
                audio_response={"type": "chat_text", "message": ""},
                action_required_payload={
                    "status": "action_required",
                    "action": "validation_failed_needs_review",
                    "message": "First attempt review.",
                    "failed_validation_rules": [
                        {
                            "rule": "validation_failed_needs_review",
                            "rule_name": "Coverage Needs Review",
                            "rule_severity": "P1",
                            "rule_domain": "LYRIC",
                            "impacted_measures": [7, 8],
                            "impacted_ranges": [{"start": 7, "end": 8}],
                        }
                    ],
                },
                followup_prompt=json.dumps({"status": "action_required", "attempt": 1}),
            )
        return ToolExecutionResult(
            score={"title": "Derived-2"},
            audio_response=None,
            followup_prompt=json.dumps(
                {
                    "status": "ready_with_warnings",
                    "message": "Second attempt review.",
                    "warnings": [
                        {
                            "code": "partial_lyric_coverage",
                            "rule_metadata": {
                                "rule": "partial_lyric_coverage",
                                "rule_name": "Partial Lyric Coverage",
                                "rule_severity": "P2",
                                "rule_domain": "LYRIC",
                                "impacted_measures": [18],
                                "impacted_ranges": [{"start": 18, "end": 18}],
                            },
                        }
                    ],
                }
            ),
            review_required=True,
        )

    followup_calls = {"count": 0}

    async def fake_decide_followup_with_llm(*args, **kwargs):
        followup_calls["count"] += 1
        if followup_calls["count"] == 1:
            return (
                LlmResponse(
                    tool_calls=[ToolCall(name="preprocess_voice_parts", arguments={})],
                    final_message="Trying one more repair.",
                    include_score=False,
                ),
                None,
            )
        return None, "LLM request failed. Please try again."

    async def fake_get_snapshot(*args, **kwargs):
        return {"current_score": {"score": {"title": "Derived-2"}, "version": 3}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-3",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert response["type"] == "chat_text"
    assert response["message"] == "Second attempt review."
    assert response["review_required"] is True
    assert response["details"]["quality_class"] == 2
    assert response["details"]["warnings"][0]["code"] == "partial_lyric_coverage"
    assert response["current_score"] == {"score": {"title": "Derived-2"}, "version": 3}


def test_workflow_stops_after_class3_candidate_even_if_followup_returns_tool_calls(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}

    async def fake_execute_tool_calls(*args, **kwargs):
        return ToolExecutionResult(
            score={"title": "Derived-clean"},
            audio_response=None,
            followup_prompt=json.dumps(
                {
                    "status": "ready",
                    "message": "Clean derived score ready.",
                    "warnings": [],
                }
            ),
            review_required=True,
        )

    async def fake_decide_followup_with_llm(*args, **kwargs):
        return (
            LlmResponse(
                tool_calls=[ToolCall(name="preprocess_voice_parts", arguments={"request": {"plan": {}}})],
                final_message="The derived score is ready for review.",
                include_score=False,
            ),
            None,
        )

    async def fake_get_snapshot(*args, **kwargs):
        return {"current_score": {"score": {"title": "Derived-clean"}, "version": 4}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-4",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert response["type"] == "chat_text"
    assert response["message"] == "The derived score is ready for review."
    assert response["review_required"] is True
    assert response["details"]["quality_class"] == 3
    assert response["details"]["issues"] == []
    assert response["current_score"] == {"score": {"title": "Derived-clean"}, "version": 4}


def test_workflow_publishes_attempt_messages_during_preprocess_repairs(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}
    attempts = {"count": 0}
    published: list[list[dict[str, object]]] = []

    async def fake_execute_tool_calls(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ToolExecutionResult(
                score={"title": "Derived-1"},
                audio_response={"type": "chat_text", "message": ""},
                action_required_payload={
                    "status": "action_required",
                    "action": "validation_failed_needs_review",
                    "message": "First attempt review.",
                    "failed_validation_rules": [
                        {
                            "rule": "validation_failed_needs_review",
                            "rule_name": "Coverage Needs Review",
                            "rule_severity": "P1",
                            "rule_domain": "LYRIC",
                            "impacted_measures": [7],
                            "impacted_ranges": [{"start": 7, "end": 7}],
                        }
                    ],
                },
                followup_prompt=json.dumps({"status": "action_required", "attempt": 1}),
            )
        return ToolExecutionResult(
            score={"title": "Derived-2"},
            audio_response={"type": "chat_text", "message": ""},
            action_required_payload={
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Second attempt review.",
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_name": "Coverage Needs Review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [8],
                        "impacted_ranges": [{"start": 8, "end": 8}],
                    }
                ],
            },
            followup_prompt=json.dumps({"status": "action_required", "attempt": 2}),
        )

    followup_calls = {"count": 0}

    async def fake_decide_followup_with_llm(*args, **kwargs):
        followup_calls["count"] += 1
        if followup_calls["count"] == 1:
            return (
                LlmResponse(
                    tool_calls=[ToolCall(name="preprocess_voice_parts", arguments={})],
                    final_message="Trying one more repair.",
                    include_score=False,
                    thought_summary="Repair thought summary",
                ),
                None,
            )
        return None, "Gemini request timed out."

    async def fake_progress_callback(attempt_messages):
        published.append(copy.deepcopy(attempt_messages))

    async def fake_get_snapshot(*args, **kwargs):
        return {"current_score": {"score": {"title": "Derived-2"}, "version": 3}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-progress",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="Initial thought summary",
            user_id="test-user",
            user_email="test@example.com",
            progress_callback=fake_progress_callback,
        )
    )

    assert response["type"] == "chat_text"
    assert published[0][0]["attempt_number"] == 1
    assert published[0][0]["message"] == "Starting preprocess"
    assert published[1][1]["attempt_number"] == 2
    assert published[1][1]["message"] == "Trying one more repair."
    assert published[1][1]["thought_summary"] == "Repair thought summary"


def test_workflow_reprompts_when_class1_candidate_stops_early(client):
    _, app = client
    orchestrator = app.state.orchestrator
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}
    execute_calls = {"count": 0}
    followup_calls = {"count": 0}

    async def fake_execute_tool_calls(*args, **kwargs):
        execute_calls["count"] += 1
        if execute_calls["count"] == 1:
            return ToolExecutionResult(
                score={"title": "Derived-reviewable"},
                audio_response=None,
                action_required_payload={
                    "status": "action_required",
                    "action": "validation_failed_needs_review",
                    "message": "Coverage still needs review.",
                    "failed_validation_rules": [
                        {
                            "rule": "validation_failed_needs_review",
                            "rule_name": "Coverage Needs Review",
                            "rule_severity": "P1",
                            "rule_domain": "LYRIC",
                            "impacted_measures": [7, 18],
                            "impacted_ranges": [{"start": 7, "end": 7}, {"start": 18, "end": 18}],
                        }
                    ],
                },
                followup_prompt=json.dumps({"status": "action_required"}),
            )
        return ToolExecutionResult(
            score={"title": "Derived-clean"},
            audio_response=None,
            followup_prompt=json.dumps(
                {
                    "status": "ready",
                    "message": "Clean derived score ready.",
                    "warnings": [],
                }
            ),
            review_required=True,
        )

    async def fake_decide_followup_with_llm(*args, **kwargs):
        followup_calls["count"] += 1
        if followup_calls["count"] == 1:
            return (
                LlmResponse(
                    tool_calls=[
                        ToolCall(
                            name="preprocess_voice_parts",
                            arguments={"request": {"plan": {"targets": []}}},
                        )
                    ],
                    final_message="Retrying the failing lyric ranges.",
                    include_score=False,
                ),
                None,
            )
        return (
            LlmResponse(
                tool_calls=[],
                final_message="The derived score is ready for review.",
                include_score=False,
            ),
            None,
        )

    async def fake_get_snapshot(*args, **kwargs):
        if execute_calls["count"] >= 2:
            return {"current_score": {"score": {"title": "Derived-clean"}, "version": 5}}
        return {"current_score": {"score": {"title": "Original"}, "version": 1}}

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm
    app.state.sessions.get_snapshot = fake_get_snapshot
    app.state.sessions.append_preprocess_attempt_summary = lambda *args, **kwargs: asyncio.sleep(0)

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            "session-4b",
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert execute_calls["count"] == 2
    assert followup_calls["count"] == 2
    assert response["type"] == "chat_text"
    assert response["message"] == "The derived score is ready for review."
    assert response["review_required"] is True
    assert response["details"]["quality_class"] == 3
    assert response["current_score"] == {"score": {"title": "Derived-clean"}, "version": 5}


def test_build_repair_planning_prompt_returns_structured_json_envelope(client):
    _, app = client
    orchestrator = app.state.orchestrator
    candidate = WorkflowCandidate(
        attempt_number=1,
        score={"title": "Derived"},
        message="Coverage still needs review.",
        review_required=True,
        quality_class=1,
        structurally_valid=True,
        structural_p1_measures=0,
        lyric_p1_measures=2,
        p2_measures=0,
        issues=[
            {
                "rule": "validation_failed_needs_review",
                "rule_severity": "P1",
                "rule_domain": "LYRIC",
                "impacted_measures": [7, 18],
            }
        ],
        target_results=[],
        result_payload={"status": "action_required", "action": "validation_failed_needs_review"},
    )

    prompt = orchestrator._build_repair_planning_prompt(
        candidate,
        {"status": "action_required", "action": "validation_failed_needs_review"},
        attempt_number=1,
        max_attempts=3,
    )
    payload = json.loads(prompt)

    assert payload["tool"] == "preprocess_voice_parts"
    assert payload["phase"] == "preprocess_repair_planning"
    assert payload["tool_result"]["action"] == "validation_failed_needs_review"
    assert payload["repair_context"]["attempt_number"] == 1
    assert payload["repair_context"]["max_attempts"] == 3
    assert payload["repair_context"]["quality_class"] == 1


def test_workflow_persists_preprocess_attempt_summary(client):
    _, app = client
    orchestrator = app.state.orchestrator
    session = asyncio.run(app.state.sessions.create_session("test-user"))
    asyncio.run(app.state.sessions.set_score(session.id, {"title": "Original"}))
    snapshot = {"score_summary": {"title": "Test"}}
    current_score = {"title": "Original"}

    async def fake_execute_tool_calls(*args, **kwargs):
        return ToolExecutionResult(
            score={"title": "Derived"},
            audio_response={"type": "chat_text", "message": ""},
            action_required_payload={
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Please review the attempt.",
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_name": "Coverage Needs Review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [7, 18],
                        "impacted_ranges": [{"start": 7, "end": 7}, {"start": 18, "end": 18}],
                    }
                ],
            },
            followup_prompt=json.dumps({"status": "action_required"}),
        )

    async def fake_decide_followup_with_llm(*args, **kwargs):
        return None, "LLM request failed. Please try again."

    orchestrator._execute_tool_calls = fake_execute_tool_calls
    orchestrator._decide_followup_with_llm = fake_decide_followup_with_llm

    response = asyncio.run(
        orchestrator._run_llm_tool_workflow(
            session.id,
            snapshot,
            current_score,
            [ToolCall(name="preprocess_voice_parts", arguments={})],
            initial_response_message="Starting preprocess",
            initial_include_score=False,
            initial_thought_block="",
            initial_thought_summary="",
            user_id="test-user",
            user_email="test@example.com",
        )
    )

    assert response["type"] == "chat_text"
    stored_snapshot = asyncio.run(app.state.sessions.get_snapshot(session.id, "test-user"))
    history = stored_snapshot["preprocess_attempt_history"]
    assert len(history) == 1
    assert history[0]["attempt_number"] == 1
    assert history[0]["candidate_present"] is True
    assert history[0]["structurally_valid"] is True
    assert history[0]["quality_class"] == 1
    assert history[0]["replaced_best_valid"] is True
    assert history[0]["replaced_best_invalid"] is False
    assert history[0]["issue_codes"] == ["validation_failed_needs_review"]


def test_orchestrator_selection_matches_current_uses_selected_verse(client):
    test_client, app = client
    orchestrator = app.state.orchestrator
    score = {
        "parts": [{"part_id": "P1"}],
        "selected_verse_number": "1",
    }
    assert orchestrator._selection_matches_current(score, None, None, "1") is True
    assert orchestrator._selection_matches_current(score, None, None, "2") is False


def test_orchestrator_builds_verse_change_action_required(client):
    test_client, app = client
    orchestrator = app.state.orchestrator
    action = orchestrator._build_verse_change_requires_repreprocess_action(
        score={},
        requested_verse_number="2",
        selected_verse_number="1",
        part_index=0,
        reparse_applied=True,
        reparsed_selected_verse_number="2",
    )
    assert action.get("status") == "action_required"
    assert action.get("action") == "preprocessing_required"
    assert action.get("reason") == "verse_change_requires_repreprocess"
    diagnostics = action.get("diagnostics") or {}
    assert diagnostics.get("requested_verse_number") == "2"
    assert diagnostics.get("selected_verse_number") == "1"
    assert diagnostics.get("reparse_applied") is True
    assert diagnostics.get("reparsed_selected_verse_number") == "2"


def test_upload_parses_zipped_musicxml(client):
    test_client, app = client
    mxl_path = PROJECT_ROOT / "assets/test_data/amazing-grace-satb-zipped.mxl"
    if not mxl_path.exists():
        pytest.skip(f"Test score not found at {mxl_path}")

    def call_tool(name, arguments):
        if name == "parse_score":
            file_path = resolve_project_path(arguments["file_path"])
            return parse_score(
                file_path,
                part_id=arguments.get("part_id"),
                part_index=arguments.get("part_index"),
                expand_repeats=arguments.get("expand_repeats", False),
            )
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    session_id = _create_session(test_client)
    files = {
        "file": (
            "amazing-grace-satb-zipped.mxl",
            mxl_path.read_bytes(),
            "application/vnd.recordare.musicxml",
        )
    }
    response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert response.status_code == 200
    payload = response.json()
    assert payload["parsed"] is True
    current_score = payload["current_score"]["score"]
    assert current_score["parts"]
    session_dir = app.state.settings.data_dir / "sessions" / session_id
    assert (session_dir / "score.mxl").exists()
    canonical_path = session_dir / "score.xml"
    assert canonical_path.exists()
    assert current_score["source_musicxml_path"] == str(canonical_path)


def test_chat_text_response_with_llm(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text='{"tool_calls": [], "final_message": "All set.", "include_score": false}'
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "hello"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_text"
    assert payload["message"] == "All set."


def test_chat_audio_response_with_llm_and_get_audio(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    assert payload["message"] == "Rendered."
    assert payload["progress_url"].startswith(f"/sessions/{session_id}/progress")
    assert "current_score" in payload

    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    audio_url = progress_payload["audio_url"]
    assert audio_url.startswith(f"/sessions/{session_id}/audio")
    query = parse_qs(urlsplit(audio_url).query)
    assert "playback_token" in query
    assert query["playback_token"][0]

    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert audio_response.content.startswith(b"RIFF")


def test_app_check_rejects_query_param_only(monkeypatch):
    calls = {"count": 0}
    app, data_dir = _prepare_app(
        monkeypatch,
        overrides={"BACKEND_REQUIRE_APP_CHECK": "true"},
    )
    monkeypatch.setattr("src.backend.main.initialize_firebase_app", lambda: None)

    def _verify_token(token):
        calls["count"] += 1
        return True

    monkeypatch.setattr("src.backend.main.app_check.verify_token", _verify_token)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    fake_request = type(
        "FakeRequest",
        (),
        {
            "app": app,
            "headers": {},
            "query_params": {"app_check": "query-only-token"},
        },
    )()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_require_app_check(fake_request))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Missing App Check token."
    assert calls["count"] == 0
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_audio_playback_bypasses_app_check_when_signed(monkeypatch):
    app, data_dir = _prepare_app(
        monkeypatch,
        overrides={"BACKEND_REQUIRE_APP_CHECK": "true"},
    )
    monkeypatch.setattr("src.backend.main.initialize_firebase_app", lambda: None)
    monkeypatch.setattr("src.backend.main.app_check.verify_token", lambda token: True)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        test_client.headers.update({"X-Firebase-AppCheck": "header-token"})
        session_id = _create_session(test_client)
        _upload_score(test_client, session_id)
        llm_client = StaticLlmClient(
            response_text=(
                '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
                '"final_message":"Rendered.","include_score":true}'
            )
        )
        app.state.llm_client = llm_client
        app.state.orchestrator._llm_client = llm_client
        response = test_client.post(
            f"/sessions/{session_id}/chat", json={"message": "render audio"}
        )
        assert response.status_code == 200
        progress_payload = _wait_for_progress(test_client, response.json()["progress_url"])
        audio_url = progress_payload["audio_url"]
        audio_response = test_client.get(audio_url, headers={})
        assert audio_response.status_code == 200
        assert audio_response.content.startswith(b"RIFF")
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_audio_playback_token_rejects_tampering(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    progress_payload = _wait_for_progress(test_client, response.json()["progress_url"])
    audio_url = progress_payload["audio_url"]
    parts = urlsplit(audio_url)
    query = parse_qs(parts.query)
    query["playback_token"] = ["tampered"]
    tampered_query = "&".join(
        f"{key}={value}"
        for key, values in query.items()
        for value in values
    )
    tampered_url = parts._replace(query=tampered_query).geturl()

    audio_response = test_client.get(tampered_url)
    assert audio_response.status_code == 401


@pytest.mark.parametrize(
    "client_with_env",
    [{"BACKEND_USE_STORAGE": "true"}],
    indirect=True,
)
def test_storage_backed_audio_uses_signed_resource_identity(client_with_env):
    test_client, app = client_with_env
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    progress_payload = _wait_for_progress(test_client, response.json()["progress_url"])
    audio_url = progress_payload["audio_url"]
    original_audio = test_client.get(audio_url)
    assert original_audio.status_code == 200
    assert original_audio.content.startswith(b"storage:")
    assert b"/jobs/" in original_audio.content

    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    current_audio = snapshot["current_audio"]
    audio_path = app.state.settings.project_root / current_audio["path"]
    asyncio.run(
        app.state.sessions.set_audio(
            session_id,
            audio_path,
            current_audio["duration_s"],
            storage_path="jobs/test-user/other-job/newer.mp3",
        )
    )

    replay_audio = test_client.get(audio_url)
    assert replay_audio.status_code == 200
    assert replay_audio.content == original_audio.content


@pytest.mark.parametrize(
    "overrides",
    [{"APP_ENV": "prod"}],
)
def test_playback_secret_is_cached_on_hot_path(overrides, monkeypatch):
    calls = {"count": 0}

    def _fake_read_secret(settings, secret_name, version):
        calls["count"] += 1
        return "prod-playback-secret"

    monkeypatch.setattr("src.backend.main.read_secret", _fake_read_secret)
    app, data_dir = _prepare_app(monkeypatch, overrides=overrides)
    keep_outputs = os.environ.get("KEEP_TEST_OUTPUT", "1").lower() not in ("0", "false", "no")
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        session_id = _create_session(test_client)
        _upload_score(test_client, session_id)
        llm_client = StaticLlmClient(
            response_text=(
                '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
                '"final_message":"Rendered.","include_score":true}'
            )
        )
        app.state.llm_client = llm_client
        app.state.orchestrator._llm_client = llm_client

        response = test_client.post(
            f"/sessions/{session_id}/chat", json={"message": "render audio"}
        )
        assert response.status_code == 200
        progress_payload = _wait_for_progress(test_client, response.json()["progress_url"])
        audio_response = test_client.get(progress_payload["audio_url"])
        assert audio_response.status_code == 200
        assert calls["count"] == 1
    if not keep_outputs:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_chat_returns_error_when_llm_fails(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    class FailingClient:
        def generate(self, system_prompt, history):
            raise RuntimeError("rate limit")

    failing_client = FailingClient()
    app.state.llm_client = failing_client
    app.state.orchestrator._llm_client = failing_client

    def call_tool(name, arguments):
        raise AssertionError(f"Tool should not be called when LLM fails: {name}")

    app.state.router.call_tool = call_tool

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_error"
    assert payload["message"] in {"LLM request failed. Please try again.", "rate limit"}


def test_chat_executes_followup_tool_calls_same_turn(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    preprocess_attempts = {"count": 0}

    def call_tool(name, arguments):
        if name == "parse_score":
            return _make_router_call_tool()(name, arguments)
        if name == "preprocess_voice_parts":
            preprocess_attempts["count"] += 1
            if preprocess_attempts["count"] == 1:
                return {
                    "status": "action_required",
                    "action": "plan_lint_failed",
                    "code": "plan_lint_failed",
                    "message": "Preflight plan lint failed.",
                    "lint_findings": [{"rule": "dummy"}],
                }
            return {
                "status": "ready",
                "score": arguments.get("score", {}),
                "part_index": 0,
            }
        return _make_router_call_tool()(name, arguments)

    class RepairThenSynthesizeClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith("Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"):
                return json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "preprocess_voice_parts",
                                "arguments": {
                                    "request": {
                                        "plan": {
                                            "targets": [
                                                {
                                                    "target": {
                                                        "part_index": 0,
                                                        "voice_part_id": "soprano",
                                                    },
                                                    "sections": [
                                                        {
                                                            "start_measure": 1,
                                                            "end_measure": 1,
                                                            "mode": "derive",
                                                            "melody_source": {
                                                                "part_index": 0,
                                                                "voice_part_id": "soprano",
                                                            },
                                                        }
                                                    ],
                                                }
                                            ]
                                        }
                                    }
                                },
                            },
                            {
                                "name": "synthesize",
                                "arguments": {"voicebank": "Dummy"},
                            },
                        ],
                        "final_message": "Plan repaired; rendering now.",
                        "include_score": True,
                    }
                )
            if isinstance(last, str) and "proceed" in last.lower():
                return json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "synthesize",
                                "arguments": {"voicebank": "Dummy"},
                            }
                        ],
                        "final_message": "Rendering now.",
                        "include_score": True,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {
                                                    "part_index": 0,
                                                    "voice_part_id": "soprano",
                                                },
                                                "sections": [
                                                    {
                                                        "start_measure": 1,
                                                        "end_measure": 1,
                                                        "mode": "derive",
                                                        "melody_source": {
                                                            "part_index": 0,
                                                            "voice_part_id": "soprano",
                                                        },
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "final_message": "Preparing render.",
                    "include_score": True,
                }
            )

    app.state.router.call_tool = call_tool
    llm_client = RepairThenSynthesizeClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "sing soprano"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    assert bool(progress_payload.get("review_required")) is True
    assert preprocess_attempts["count"] == 2

    proceed = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Looks good, proceed."}
    )
    assert proceed.status_code == 200
    proceed_payload = proceed.json()
    assert proceed_payload["type"] == "chat_progress"


def test_chat_reparse_allows_direct_synthesis_same_turn(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    tool_calls: list[tuple[str, dict]] = []

    def call_tool(name, arguments):
        tool_calls.append((name, dict(arguments)))
        if name == "parse_score":
            selected_verse = str(arguments.get("verse_number") or "1")
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"part_id": "P1", "voice_part_id": "soprano", "notes": []}],
                "structure": {},
                "selected_verse_number": selected_verse,
                "voice_part_signals": {"requested_verse_number": selected_verse},
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0}],
                    "available_verses": ["1", "2"],
                    "selected_verse_number": selected_verse,
                },
            }
        if name == "preprocess_voice_parts":
            return {
                "status": "ready",
                "score": arguments.get("score", {}),
                "part_index": 0,
                "modified_musicxml_path": "tests/output/derived.xml",
            }
        return _make_router_call_tool()(name, arguments)

    class ReparseThenSynthesizeClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(
                "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
            ):
                return json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "synthesize",
                                "arguments": {
                                    "part_index": 0,
                                    "voice_id": "voicebank-1",
                                },
                            }
                        ],
                        "final_message": "Singing verse 2 now.",
                        "include_score": False,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "reparse",
                            "arguments": {"verse_number": "2"},
                        }
                    ],
                    "final_message": "Switching to verse 2.",
                    "include_score": True,
                }
            )

    app.state.router.call_tool = call_tool
    import src.backend.orchestrator as orchestrator_module
    original_precheck = orchestrator_module.synthesize_preflight_action_required
    orchestrator_module.synthesize_preflight_action_required = lambda score, part_index: None
    llm_client = ReparseThenSynthesizeClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    async def fake_start_synthesis_job(*args, **kwargs):
        return {"type": "chat_text", "message": "Singing verse 2 now."}
    app.state.orchestrator._start_synthesis_job = fake_start_synthesis_job

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Change to verse 2 and sing soprano"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat_text"

    reparses_to_verse_2 = [
        args for name, args in tool_calls if name == "parse_score" and str(args.get("verse_number")) == "2"
    ]
    preprocess_calls = [args for name, args in tool_calls if name == "preprocess_voice_parts"]
    orchestrator_module.synthesize_preflight_action_required = original_precheck
    assert reparses_to_verse_2, f"Expected parse_score reparse with verse 2. tool_calls={tool_calls}"
    assert not preprocess_calls, f"Did not expect forced preprocess after reparse. tool_calls={tool_calls}"
    assert body["message"] == "Singing verse 2 now."


def test_chat_blocks_preprocess_without_explicit_verse_for_multiverse_score(client):
    test_client, app = client
    session_id = _create_session(test_client)

    tool_calls: list[tuple[str, dict]] = []

    def call_tool(name, arguments):
        tool_calls.append((name, dict(arguments)))
        if name == "parse_score":
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"part_id": "P1", "voice_part_id": "soprano", "notes": []}],
                "structure": {},
                "selected_verse_number": "1",
                "voice_part_signals": {"requested_verse_number": "1"},
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0}],
                    "available_verses": ["1", "2", "3", "4"],
                    "selected_verse_number": "1",
                },
            }
        if name == "preprocess_voice_parts":
            raise AssertionError("preprocess_voice_parts should be blocked until verse is selected")
        return _make_router_call_tool()(name, arguments)

    class PreprocessNeedsVerseClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(
                "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
            ):
                return json.dumps(
                    {
                        "tool_calls": [],
                        "final_message": "This score has multiple verses. Which verse should I sing?",
                        "include_score": False,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {"part_index": 0, "voice_part_id": "soprano"},
                                                "sections": [{"start_measure": 1, "end_measure": 1, "mode": "derive"}],
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "final_message": "Preparing your requested part.",
                    "include_score": False,
                }
            )

    app.state.router.call_tool = call_tool
    _upload_score(test_client, session_id)
    asyncio.run(
        app.state.sessions.set_score_summary(
            session_id,
            {
                "title": "Test",
                "composer": None,
                "lyricist": None,
                "parts": [{"part_id": "P1", "part_index": 0}],
                "available_verses": ["1", "2", "3", "4"],
                "selected_verse_number": "1",
            },
        )
    )
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    seeded_score = dict(snapshot["current_score"]["score"])
    seeded_score["selected_verse_number"] = "1"
    asyncio.run(app.state.sessions.set_score(session_id, seeded_score))
    asyncio.run(app.state.sessions.set_metadata(session_id, "explicit_verse_number", ""))
    refreshed = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert app.state.orchestrator._tool_calls_require_verse_selection(
        [
            ToolCall(
                name="preprocess_voice_parts",
                arguments={
                    "request": {
                        "plan": {
                            "targets": [
                                {
                                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                                    "sections": [{"start_measure": 1, "end_measure": 1, "mode": "derive"}],
                                }
                            ]
                        }
                    }
                },
            )
        ],
        score=refreshed["current_score"]["score"],
        score_summary=refreshed["score_summary"],
        explicit_verse_number=None,
    )
    llm_client = PreprocessNeedsVerseClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "can you sing in male voice?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat_text"
    assert "verse" in body["message"].lower()
    action_required = body.get("action_required") or {}
    assert action_required.get("action") == "verse_selection_required"
    assert action_required.get("available_verses") == ["1", "2", "3", "4"]
    preprocess_calls = [args for name, args in tool_calls if name == "preprocess_voice_parts"]
    assert not preprocess_calls


def test_chat_blocks_synthesize_without_explicit_verse_for_multiverse_score(client):
    test_client, app = client
    session_id = _create_session(test_client)

    tool_calls: list[tuple[str, dict]] = []

    def call_tool(name, arguments):
        tool_calls.append((name, dict(arguments)))
        if name == "parse_score":
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"part_id": "P1", "voice_part_id": "soprano", "notes": []}],
                "structure": {},
                "selected_verse_number": "1",
                "voice_part_signals": {"requested_verse_number": "1"},
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0}],
                    "available_verses": ["1", "2"],
                    "selected_verse_number": "1",
                },
            }
        if name == "synthesize":
            raise AssertionError("synthesize should be blocked until verse is selected")
        return _make_router_call_tool()(name, arguments)

    class SynthesizeNeedsVerseClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(
                "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
            ):
                return json.dumps(
                    {
                        "tool_calls": [],
                        "final_message": "Please choose which verse to sing first.",
                        "include_score": False,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "synthesize",
                            "arguments": {"part_index": 0, "voicebank": "Dummy"},
                        }
                    ],
                    "final_message": "Starting synthesis.",
                    "include_score": False,
                }
            )

    app.state.router.call_tool = call_tool
    _upload_score(test_client, session_id)
    asyncio.run(
        app.state.sessions.set_score_summary(
            session_id,
            {
                "title": "Test",
                "composer": None,
                "lyricist": None,
                "parts": [{"part_id": "P1", "part_index": 0}],
                "available_verses": ["1", "2"],
                "selected_verse_number": "1",
            },
        )
    )
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    seeded_score = dict(snapshot["current_score"]["score"])
    seeded_score["selected_verse_number"] = "1"
    asyncio.run(app.state.sessions.set_score(session_id, seeded_score))
    asyncio.run(app.state.sessions.set_metadata(session_id, "explicit_verse_number", ""))
    llm_client = SynthesizeNeedsVerseClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "please sing this song"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat_text"
    assert "verse" in body["message"].lower()
    action_required = body.get("action_required") or {}
    assert action_required.get("action") == "verse_selection_required"
    assert action_required.get("available_verses") == ["1", "2"]
    synth_calls = [args for name, args in tool_calls if name == "synthesize"]
    assert not synth_calls


def test_chat_selection_verse_number_allows_preprocess_for_multiverse_score(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    asyncio.run(
        app.state.sessions.set_score_summary(
            session_id,
            {
                "title": "Test",
                "composer": None,
                "lyricist": None,
                "parts": [{"part_id": "P1", "part_index": 0}],
                "available_verses": ["1", "2"],
                "selected_verse_number": "1",
            },
        )
    )
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    seeded_score = dict(snapshot["current_score"]["score"])
    seeded_score["selected_verse_number"] = "1"
    asyncio.run(app.state.sessions.set_score(session_id, seeded_score))

    preprocess_called = {"count": 0}

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            preprocess_called["count"] += 1
            return {
                "status": "ready",
                "score": arguments.get("score", {}),
                "part_index": 0,
                "modified_musicxml_path": "tests/output/derived.xml",
            }
        return _make_router_call_tool()(name, arguments)

    class SelectionAwareClient:
        def generate(self, system_prompt, history):
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {"part_index": 0, "voice_part_id": "alto"},
                                                "sections": [{"start_measure": 1, "end_measure": 1, "mode": "derive"}],
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "final_message": "Setting up Alto verse 1.",
                    "include_score": False,
                }
            )

    app.state.router.call_tool = call_tool
    llm_client = SelectionAwareClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={
            "message": "Please sing the Alto part, verse 1.",
            "selection": {"verse_number": "1", "part_index": 0},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    assert bool(progress_payload.get("review_required")) is True
    assert preprocess_called["count"] == 1
    updated = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    assert updated["files"]["explicit_verse_number"] == "1"


def test_chat_text_verse_without_selection_still_blocks_multiverse_preprocess(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    asyncio.run(
        app.state.sessions.set_score_summary(
            session_id,
            {
                "title": "Test",
                "composer": None,
                "lyricist": None,
                "parts": [{"part_id": "P1", "part_index": 0}],
                "available_verses": ["1", "2"],
                "selected_verse_number": "1",
            },
        )
    )
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    seeded_score = dict(snapshot["current_score"]["score"])
    seeded_score["selected_verse_number"] = "1"
    asyncio.run(app.state.sessions.set_score(session_id, seeded_score))

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            raise AssertionError("preprocess_voice_parts should be blocked without structured selection")
        return _make_router_call_tool()(name, arguments)

    class TextOnlyClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(
                "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
            ):
                return json.dumps(
                    {
                        "tool_calls": [],
                        "final_message": "Please pick a verse from the selector.",
                        "include_score": False,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {"part_index": 0, "voice_part_id": "alto"},
                                                "sections": [{"start_measure": 1, "end_measure": 1, "mode": "derive"}],
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "final_message": "Setting up Alto verse 1.",
                    "include_score": False,
                }
            )

    app.state.router.call_tool = call_tool
    llm_client = TextOnlyClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Please sing the Alto part, verse 1."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat_text"
    action_required = body.get("action_required") or {}
    assert action_required.get("action") == "verse_selection_required"


def test_preprocess_with_explicit_verse_argument_bypasses_selection_blocker(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    asyncio.run(
        app.state.sessions.set_score_summary(
            session_id,
            {
                "title": "Test",
                "composer": None,
                "lyricist": None,
                "parts": [{"part_id": "P1", "part_index": 0}],
                "available_verses": ["1", "2"],
                "selected_verse_number": "1",
            },
        )
    )
    snapshot = asyncio.run(app.state.sessions.get_snapshot(session_id, "test-user"))
    seeded_score = dict(snapshot["current_score"]["score"])
    seeded_score["selected_verse_number"] = "1"
    asyncio.run(app.state.sessions.set_score(session_id, seeded_score))
    asyncio.run(app.state.sessions.set_metadata(session_id, "explicit_verse_number", ""))

    preprocess_called = {"count": 0}

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            preprocess_called["count"] += 1
            return {
                "status": "ready",
                "score": arguments.get("score", {}),
                "part_index": 0,
                "modified_musicxml_path": "tests/output/derived.xml",
            }
        return _make_router_call_tool()(name, arguments)

    class PreprocessWithVerseClient:
        def generate(self, system_prompt, history):
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "verse_number": 1,
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {"part_index": 0, "voice_part_id": "alto"},
                                                "sections": [{"start_measure": 1, "end_measure": 1, "mode": "derive"}],
                                            }
                                        ]
                                    },
                                }
                            },
                        }
                    ],
                    "final_message": "Setting up Alto verse 1.",
                    "include_score": False,
                }
            )

    app.state.router.call_tool = call_tool
    llm_client = PreprocessWithVerseClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Please sing the Alto part, verse 1."},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    assert bool(progress_payload.get("review_required")) is True
    assert preprocess_called["count"] == 1


def test_chat_reparse_same_verse_noop_allows_direct_synthesis(client):
    test_client, app = client
    session_id = _create_session(test_client)

    parse_score_calls = {"count": 0}
    def call_tool(name, arguments):
        if name == "parse_score":
            parse_score_calls["count"] += 1
            selected_verse = str(arguments.get("verse_number") or "1")
            return {
                "title": "Test",
                "tempos": [],
                "parts": [{"part_id": "P1", "voice_part_id": "soprano", "notes": []}],
                "structure": {},
                "selected_verse_number": selected_verse,
                "voice_part_signals": {"requested_verse_number": selected_verse},
                "score_summary": {
                    "title": "Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0}],
                    "available_verses": ["1", "2"],
                    "selected_verse_number": selected_verse,
                },
            }
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool
    upload_response = _upload_score(test_client, session_id)
    assert upload_response.status_code == 200
    assert parse_score_calls["count"] == 1

    import src.backend.orchestrator as orchestrator_module
    original_precheck = orchestrator_module.synthesize_preflight_action_required
    orchestrator_module.synthesize_preflight_action_required = lambda score, part_index: None

    class SameVerseReparseClient:
        def generate(self, system_prompt, history):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(
                "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
            ):
                return json.dumps(
                    {
                        "tool_calls": [
                            {
                                "name": "synthesize",
                                "arguments": {
                                    "part_index": 0,
                                    "voice_id": "voicebank-1",
                                },
                            }
                        ],
                        "final_message": "Proceeding to sing the current verse.",
                        "include_score": False,
                    }
                )
            return json.dumps(
                {
                    "tool_calls": [
                        {"name": "reparse", "arguments": {"verse_number": "1"}},
                    ],
                    "final_message": "Refreshing verse context.",
                    "include_score": True,
                }
            )

    llm_client = SameVerseReparseClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    async def fake_start_synthesis_job(*args, **kwargs):
        return {"type": "chat_text", "message": "Proceeding to sing the current verse."}
    app.state.orchestrator._start_synthesis_job = fake_start_synthesis_job

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Use verse 1 and proceed"}
    )
    assert response.status_code == 200
    body = response.json()
    orchestrator_module.synthesize_preflight_action_required = original_precheck
    assert parse_score_calls["count"] == 1, "Same-verse reparse should be treated as no-op."
    assert body["type"] == "chat_text"
    assert body["message"] == "Proceeding to sing the current verse."


def test_preprocess_returns_last_validation_review_after_repair_cap(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    preprocess_calls = {"count": 0}

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            preprocess_calls["count"] += 1
            return {
                "status": "action_required",
                "action": "validation_failed_needs_review",
                "message": "Lyric propagation did not meet minimum coverage.",
                "part_index": 0,
                "target_voice_part": "soprano",
                "validation": {
                    "word_lyric_coverage_ratio": 0.6,
                    "missing_lyric_sung_note_count": 4,
                    "unresolved_measures": [7, 18],
                },
                "failed_validation_rules": [
                    {
                        "rule": "validation_failed_needs_review",
                        "rule_severity": "P1",
                        "rule_domain": "LYRIC",
                        "impacted_measures": [7, 18],
                    }
                ],
            }
        return _make_router_call_tool()(name, arguments)

    app.state.router.call_tool = call_tool

    class RepairLoopClient:
        def __init__(self):
            self.calls = 0

        def generate(self, system_prompt, history):
            self.calls += 1
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "preprocess_voice_parts",
                            "arguments": {
                                "request": {
                                    "plan": {
                                        "targets": [
                                            {
                                                "target": {
                                                    "part_index": 0,
                                                    "voice_part_id": "soprano",
                                                },
                                                "sections": [
                                                    {
                                                        "start_measure": 1,
                                                        "end_measure": 1,
                                                        "mode": "derive",
                                                        "melody_source": {
                                                            "part_index": 0,
                                                            "voice_part_id": "soprano",
                                                        },
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "final_message": "Trying preprocess again.",
                    "include_score": False,
                }
            )

    llm_client = RepairLoopClient()
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Sing soprano"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    assert bool(progress_payload.get("review_required")) is True
    assert preprocess_calls["count"] == 3


def test_get_audio_requires_playback_token(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    response = test_client.get(f"/sessions/{session_id}/audio")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing playback token."


def test_chat_releases_reserved_credits_when_synthesis_job_start_fails(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)

    release_calls: list[str] = []

    monkeypatch.setattr(
        "src.backend.credits.reserve_credits",
        lambda *_, **__: ReserveCreditsResult(status="reserved", estimated_credits=1),
    )
    monkeypatch.setattr(
        "src.backend.credits.release_credits",
        lambda _uid, job_id: release_calls.append(job_id) or ReleaseCreditsResult(status="released"),
    )

    class SynthesisClient:
        def generate(self, system_prompt, history):
            return json.dumps(
                {
                    "tool_calls": [
                        {"name": "synthesize", "arguments": {"part_index": 0, "voicebank": "Dummy"}}
                    ],
                    "final_message": "Starting synthesis.",
                    "include_score": False,
                }
            )

    import src.backend.orchestrator as orchestrator_module

    original_precheck = orchestrator_module.synthesize_preflight_action_required
    orchestrator_module.synthesize_preflight_action_required = lambda score, part_index: None
    app.state.llm_client = SynthesisClient()
    app.state.orchestrator._llm_client = app.state.llm_client

    async def fake_start_synthesis_job(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    app.state.orchestrator._start_synthesis_job = fake_start_synthesis_job
    try:
        response = test_client.post(
            f"/sessions/{session_id}/chat",
            json={"message": "please sing this song"},
        )
    finally:
        orchestrator_module.synthesize_preflight_action_required = original_precheck

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat_text"
    assert "Couldn't start the take" in body["message"]
    assert len(release_calls) == 1


def test_progress_hides_audio_until_credit_settlement_succeeds(client, monkeypatch):
    test_client, app = client
    session_id = _create_session(test_client)
    job_id = "job-credit-reconcile"

    monkeypatch.setattr(
        "src.backend.credits.settle_credits",
        lambda *_, **__: SettleCreditsResult(
            status="reconciliation_required",
            actual_credits=2,
            overdrafted=False,
        ),
    )

    app.state.job_store.create_job(
        job_id=job_id,
        user_id="test-user",
        session_id=session_id,
        status="queued",
    )

    async def fake_synthesize(*args, **kwargs):
        return {
            "audio_url": f"/sessions/{session_id}/audio?file=audio-test.wav",
            "output_path": "tests/output/audio-test.wav",
            "duration_seconds": 61.0,
        }

    app.state.orchestrator._synthesize = fake_synthesize

    asyncio.run(
        app.state.orchestrator._run_synthesis_job(
            session_id,
            {},
            {},
            job_id,
            "test-user",
            input_path=None,
            storage_input_path=None,
            job_input_storage_path=None,
            output_storage_path=None,
        )
    )

    progress = test_client.get(
        f"/sessions/{session_id}/progress",
        headers=_auth_headers(),
    )
    assert progress.status_code == 200
    payload = progress.json()
    assert payload["status"] == "error"
    assert "billing finalization failed" in payload["message"].lower()
    assert "audio_url" not in payload


@pytest.mark.parametrize(
    "client_with_env",
    [{"BACKEND_AUDIO_FORMAT": "mp3", "BACKEND_DEBUG": "0"}],
    indirect=True,
)
def test_chat_audio_outputs_mp3_only_when_not_debug(client_with_env):
    test_client, app = client_with_env
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    audio_url = progress_payload.get("audio_url", "")
    file_name = parse_qs(urlsplit(audio_url).query)["file"][0]
    audio_path = app.state.settings.sessions_dir / session_id / file_name
    assert audio_path.suffix == ".mp3"
    assert audio_path.exists()
    assert not audio_path.with_suffix(".wav").exists()


@pytest.mark.parametrize(
    "client_with_env",
    [{"BACKEND_AUDIO_FORMAT": "mp3", "BACKEND_DEBUG": "1"}],
    indirect=True,
)
def test_chat_audio_outputs_wav_when_debug(client_with_env):
    test_client, app = client_with_env
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "render audio"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    audio_url = progress_payload.get("audio_url", "")
    file_name = parse_qs(urlsplit(audio_url).query)["file"][0]
    audio_path = app.state.settings.sessions_dir / session_id / file_name
    assert audio_path.suffix == ".mp3"
    assert audio_path.exists()
    assert audio_path.with_suffix(".wav").exists()
