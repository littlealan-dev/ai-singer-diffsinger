import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.backend.main import create_app
from src.backend.llm_client import StaticLlmClient
from src.mcp.resolve import PROJECT_ROOT


VOICEBANK_ID = "Raine_Rena_2.01"
SCORE_PATH = PROJECT_ROOT / "assets/test_data/amazing-grace-satb-verse1.xml"


def _auth_headers(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def integration_client(monkeypatch):
    if not SCORE_PATH.exists():
        pytest.skip(f"Test score not found at {SCORE_PATH}")
    voicebank_path = PROJECT_ROOT / "assets/voicebanks" / VOICEBANK_ID
    if not voicebank_path.exists():
        pytest.skip(f"Voicebank not found at {voicebank_path}")
    data_dir = Path("tests/output/backend_integration") / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("MCP_CPU_DEVICE", "cpu")
    monkeypatch.setenv("MCP_GPU_DEVICE", "cpu")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr("src.backend.job_store.JobStore.create_job", lambda *_, **__: None)
    monkeypatch.setattr("src.backend.job_store.JobStore.update_job", lambda *_, **__: None)
    app = create_app()
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"modify_score","arguments":{"code":'
            '"notes = score[\'parts\'][0][\'notes\']\\nif notes:\\n'
            '    score[\'parts\'][0][\'notes\'] = notes[: max(1, len(notes) // 2)]\\n"}},'
            '{"name":"synthesize","arguments":{"voicebank":"'
            + VOICEBANK_ID
            + '"}}],"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        yield test_client, app


def test_backend_integration_modify_and_synthesize(integration_client):
    test_client, _ = integration_client
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]

    files = {"file": ("score.xml", SCORE_PATH.read_bytes(), "application/xml")}
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    assert upload_payload["parsed"] is True
    assert upload_payload["current_score"]["version"] == 1

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Render with changes"}
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload["type"] == "chat_progress"
    assert chat_payload["message"] == "Rendered."
    assert chat_payload["progress_url"].startswith(f"/sessions/{session_id}/progress")
    assert chat_payload["current_score"]["version"] == 2

    progress_payload = _wait_for_progress(test_client, chat_payload["progress_url"])
    assert progress_payload["status"] == "done"
    audio_url = progress_payload["audio_url"]
    assert audio_url.startswith(f"/sessions/{session_id}/audio")

    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0


def test_backend_integration_synthesize_soft_color(integration_client):
    test_client, app = integration_client
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"'
            + VOICEBANK_ID
            + '","voice_color":"02: soft"}}],"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client

    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]

    files = {"file": ("score.xml", SCORE_PATH.read_bytes(), "application/xml")}
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "Render with soft color"}
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, chat_payload["progress_url"])
    assert progress_payload["audio_url"].startswith(f"/sessions/{session_id}/audio")


def _wait_for_progress(test_client, progress_url, timeout_seconds=30.0):
    deadline = time.time() + timeout_seconds
    last_payload = None
    while time.time() < deadline:
        response = test_client.get(progress_url)
        assert response.status_code == 200
        payload = response.json()
        last_payload = payload
        if payload.get("status") in ("done", "error"):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for progress: {last_payload}")
