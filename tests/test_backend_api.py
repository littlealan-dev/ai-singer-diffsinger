import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.backend.main import create_app
from src.backend.llm_client import StaticLlmClient
from src.mcp.resolve import PROJECT_ROOT


def _make_router_call_tool():
    def _call_tool(name, arguments):
        if name == "parse_score":
            return {"title": "Test", "tempos": [], "parts": [{"notes": []}], "structure": {}}
        if name == "modify_score":
            return arguments.get("score", {})
        if name == "synthesize":
            return {"waveform": [0.0, 0.1, 0.0], "sample_rate": 44100}
        if name == "save_audio":
            rel_path = arguments["output_path"]
            abs_path = PROJECT_ROOT / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(b"RIFFTESTDATA")
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


@pytest.fixture
def client(monkeypatch):
    data_dir = Path("tests/output/backend_data") / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    app = create_app()
    app.state.router.call_tool = _make_router_call_tool()
    with TestClient(app) as test_client:
        yield test_client, app
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


def test_create_session_returns_id(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    assert isinstance(session_id, str)
    assert len(session_id) > 0


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
    assert payload["type"] == "chat_audio"
    assert payload["message"] == "Rendered."
    assert payload["audio_url"] == f"/sessions/{session_id}/audio"
    assert "current_score" in payload

    audio_response = test_client.get(f"/sessions/{session_id}/audio")
    assert audio_response.status_code == 200
    assert audio_response.content.startswith(b"RIFF")


def test_get_audio_returns_404_without_audio(client):
    test_client, _ = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    response = test_client.get(f"/sessions/{session_id}/audio")
    assert response.status_code == 404
