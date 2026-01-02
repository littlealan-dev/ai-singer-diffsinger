import logging
import os
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
def gemini_client(monkeypatch):
    if os.getenv("RUN_REAL_LLM_TESTS") != "1":
        pytest.skip("Set RUN_REAL_LLM_TESTS=1 to run real Gemini E2E test.")
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        pytest.skip("GEMINI_API_KEY is not set.")
    if not SCORE_PATH.exists():
        pytest.skip(f"Test score not found at {SCORE_PATH}")
    voicebank_path = PROJECT_ROOT / "assets/voicebanks" / VOICEBANK_ID
    if not voicebank_path.exists():
        pytest.skip(f"Voicebank not found at {voicebank_path}")

    startup_id = uuid.uuid4().hex
    data_dir = Path("tests/output/backend_e2e_gemini") / startup_id
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("MCP_CPU_DEVICE", os.getenv("MCP_CPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_GPU_DEVICE", os.getenv("MCP_GPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_DEBUG", "true")

    log_path = data_dir / "backend_e2e.log"
    logger = logging.getLogger("backend_e2e")
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False

    orchestrator_logger = logging.getLogger("src.backend.orchestrator")
    orchestrator_logger.setLevel(logging.DEBUG)
    orchestrator_logger.addHandler(handler)
    orchestrator_logger.propagate = False

    api_logger = logging.getLogger("backend.api")
    api_logger.setLevel(logging.DEBUG)
    api_logger.addHandler(handler)
    api_logger.propagate = False

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, data_dir, startup_id, logger
    orchestrator_logger.removeHandler(handler)
    api_logger.removeHandler(handler)
    handler.close()


def test_backend_e2e_gemini_synthesize(gemini_client):
    test_client, data_dir, startup_id, logger = gemini_client
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    logger.info(
        "backend_e2e_start startup_id=%s session_id=%s data_dir=%s",
        startup_id,
        session_id,
        data_dir,
    )

    files = {"file": ("score.xml", SCORE_PATH.read_bytes(), "application/xml")}
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Please can you sing this score?"},
    )
    assert chat_response.status_code == 200
    payload = chat_response.json()
    assert payload.get("type") == "chat_audio", f"Unexpected response: {payload}"
    assert payload.get("audio_url") == f"/sessions/{session_id}/audio"

    audio_response = test_client.get(f"/sessions/{session_id}/audio")
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0
