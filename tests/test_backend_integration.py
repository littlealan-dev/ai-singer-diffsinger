import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.backend.main import create_app
from src.mcp.resolve import PROJECT_ROOT


VOICEBANK_ID = "Raine_Rena_2.01"
SCORE_PATH = PROJECT_ROOT / "assets/test_data/amazing-grace-satb-verse1.xml"


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
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("MCP_CPU_DEVICE", "cpu")
    monkeypatch.setenv("MCP_GPU_DEVICE", "cpu")
    app = create_app()
    app.state.orchestrator._call_gemini = lambda history, score_available: json.dumps(
        {
            "tool_calls": [
                {
                    "name": "modify_score",
                    "arguments": {
                        "code": (
                            "notes = score['parts'][0]['notes']\n"
                            "if notes:\n"
                            "    score['parts'][0]['notes'] = notes[: max(1, len(notes) // 2)]\n"
                        )
                    },
                },
                {"name": "synthesize", "arguments": {"voicebank": VOICEBANK_ID}},
            ],
            "final_message": "Rendered.",
            "include_score": True,
        }
    )
    with TestClient(app) as test_client:
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
    assert chat_payload["type"] == "chat_audio"
    assert chat_payload["message"] == "Rendered."
    assert chat_payload["audio_url"] == f"/sessions/{session_id}/audio"
    assert chat_payload["current_score"]["version"] == 2

    audio_response = test_client.get(f"/sessions/{session_id}/audio")
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0
