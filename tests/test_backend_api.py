import os
import shutil
import time
import uuid
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.score import parse_score
from src.backend.main import create_app
from src.backend.llm_client import StaticLlmClient
from src.mcp.resolve import PROJECT_ROOT, resolve_project_path


def _make_router_call_tool():
    def _call_tool(name, arguments):
        if name == "parse_score":
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
                    "available_verses": [],
                },
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
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr("src.backend.job_store.JobStore.create_job", lambda *_, **__: None)
    monkeypatch.setattr("src.backend.job_store.JobStore.update_job", lambda *_, **__: None)
    if overrides:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
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
    files = {"file": (filename, xml, "application/xml")}
    return test_client.post(f"/sessions/{session_id}/upload", files=files)


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
    assert "2" in summary.get("available_verses", [])


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
    score_path = app.state.settings.data_dir / "sessions" / session_id / "score.mxl"
    assert score_path.exists()


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

    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert audio_response.content.startswith(b"RIFF")


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
    assert payload["type"] == "chat_text"
    assert payload["message"] == "LLM request failed. Please try again."


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
    assert payload["type"] == "chat_text"
    assert bool(payload.get("review_required")) is True
    assert preprocess_attempts["count"] == 2

    proceed = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Looks good, proceed."}
    )
    assert proceed.status_code == 200
    proceed_payload = proceed.json()
    assert proceed_payload["type"] == "chat_progress"


def test_get_audio_returns_404_without_audio(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    response = test_client.get(f"/sessions/{session_id}/audio")
    assert response.status_code == 404


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
    file_name = audio_url.split("file=")[-1]
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
    file_name = audio_url.split("file=")[-1]
    audio_path = app.state.settings.sessions_dir / session_id / file_name
    assert audio_path.suffix == ".mp3"
    assert audio_path.exists()
    assert audio_path.with_suffix(".wav").exists()
