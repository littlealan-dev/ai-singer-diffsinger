import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
import firebase_admin

from src.backend.main import create_app
from src.backend.firebase_app import get_firestore_client
from src.backend.llm_client import StaticLlmClient
from src.backend.storage_client import blob_exists
from src.mcp.resolve import PROJECT_ROOT


VOICEBANK_ID = "Raine_Rena_2.01"
SCORE_PATH = PROJECT_ROOT / "assets/test_data/amazing-grace-satb-verse1.xml"
MULTI_VERSE_SCORE_PATH = PROJECT_ROOT / "assets/test_data/o-holy-night.xml"


def _auth_headers(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


def _reset_firebase_admin():
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = None
    if app is not None:
        firebase_admin.delete_app(app)
    from src.backend import firebase_app

    firebase_app._app = None
    firebase_app._firestore_client = None


def _ensure_emulator_available(host: str, name: str) -> None:
    try:
        httpx.get(f"http://{host}", timeout=1.0)
    except Exception:
        pytest.skip(f"{name} emulator not reachable at {host}")


def _is_emulator_up(host: str) -> bool:
    try:
        httpx.get(f"http://{host}", timeout=1.0)
        return True
    except Exception:
        return False


def _start_emulators(project_id: str, auth_host: str, firestore_host: str, storage_host: str):
    if shutil.which("firebase") is None:
        pytest.skip("Firebase CLI not found. Install firebase-tools to run emulators.")
    if _is_emulator_up(auth_host) and _is_emulator_up(firestore_host):
        return None, False
    log_dir = PROJECT_ROOT / "tests" / "output"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "firebase_emulator.log"
    log_handle = log_path.open("ab")
    firestore_log_dir = PROJECT_ROOT / "logs"
    firestore_log_dir.mkdir(parents=True, exist_ok=True)
    firestore_log_path = firestore_log_dir / "firestore-debug.log"
    cmd = [
        "firebase",
        "emulators:start",
        "--only",
        "auth,firestore,storage",
        "--project",
        project_id,
    ]
    env = os.environ.copy()
    env["FIRESTORE_EMULATOR_LOG"] = str(firestore_log_path)
    process = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=log_handle,
        env=env,
    )
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if _is_emulator_up(auth_host) and _is_emulator_up(firestore_host):
            return (process, log_handle), True
        time.sleep(0.5)
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
    log_handle.close()
    pytest.skip(f"Emulators failed to start. Check {log_path}")


def _create_emulator_user(auth_host: str) -> str:
    email = f"test-{uuid.uuid4().hex}@example.com"
    payload = {"email": email, "password": "test-password", "returnSecureToken": True}
    url = (
        f"http://{auth_host}"
        "/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fake-api-key"
    )
    response = httpx.post(url, json=payload, timeout=5.0)
    response.raise_for_status()
    data = response.json()
    token = data.get("idToken")
    if not token:
        raise AssertionError("Auth emulator did not return idToken.")
    return token


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
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr("src.backend.job_store.JobStore.create_job", lambda *_, **__: None)
    monkeypatch.setattr("src.backend.job_store.JobStore.update_job", lambda *_, **__: None)

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
        test_client.headers.update(_auth_headers())
        yield test_client, data_dir, startup_id, logger
    orchestrator_logger.removeHandler(handler)
    api_logger.removeHandler(handler)
    handler.close()


@pytest.fixture
def emulator_client(monkeypatch):
    if os.getenv("RUN_FIREBASE_EMULATOR_TESTS") != "1":
        pytest.skip("Set RUN_FIREBASE_EMULATOR_TESTS=1 to run emulator E2E test.")
    auth_host = os.getenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    firestore_host = os.getenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    storage_host = os.getenv("FIREBASE_STORAGE_EMULATOR_HOST", "127.0.0.1:9199")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "sightsinger-app")
    emulator_process, managed = _start_emulators(
        project_id, auth_host, firestore_host, storage_host
    )
    _ensure_emulator_available(auth_host, "Auth")
    _ensure_emulator_available(firestore_host, "Firestore")
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", auth_host)
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", firestore_host)
    monkeypatch.setenv("FIREBASE_STORAGE_EMULATOR_HOST", storage_host)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("STORAGE_BUCKET", f"{project_id}.appspot.com")
    monkeypatch.setenv("BACKEND_DATA_DIR", str(Path("tests/output/backend_emulator") / uuid.uuid4().hex))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("MCP_CPU_DEVICE", "cpu")
    monkeypatch.setenv("MCP_GPU_DEVICE", "cpu")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "true")
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", f"http://{storage_host}")
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)

    def call_tool(name, arguments):
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

    _reset_firebase_admin()
    app = create_app()
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"Dummy"}}],'
            '"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    app.state.router.call_tool = call_tool
    token = _create_emulator_user(auth_host)
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers(token))
        yield test_client, app
    if managed and emulator_process is not None:
        process, log_handle = emulator_process
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
        log_handle.close()


@pytest.fixture
def emulator_gemini_client(monkeypatch):
    if os.getenv("RUN_REAL_LLM_TESTS") != "1":
        pytest.skip("Set RUN_REAL_LLM_TESTS=1 to run real Gemini E2E test.")
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        pytest.skip("GEMINI_API_KEY is not set.")
    auth_host = os.getenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    firestore_host = os.getenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    storage_host = os.getenv("FIREBASE_STORAGE_EMULATOR_HOST", "127.0.0.1:9199")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "sightsinger-app")
    emulator_process, managed = _start_emulators(
        project_id, auth_host, firestore_host, storage_host
    )
    _ensure_emulator_available(auth_host, "Auth")
    _ensure_emulator_available(firestore_host, "Firestore")
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", auth_host)
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", firestore_host)
    monkeypatch.setenv("FIREBASE_STORAGE_EMULATOR_HOST", storage_host)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("STORAGE_BUCKET", f"{project_id}.appspot.com")
    monkeypatch.setenv("BACKEND_DATA_DIR", str(Path("tests/output/backend_emulator") / uuid.uuid4().hex))
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("MCP_CPU_DEVICE", os.getenv("MCP_CPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_GPU_DEVICE", os.getenv("MCP_GPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_DEBUG", "true")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "true")
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", f"http://{storage_host}")

    _reset_firebase_admin()
    app = create_app()
    token = _create_emulator_user(auth_host)
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers(token))
        yield test_client, app
    if managed and emulator_process is not None:
        process, log_handle = emulator_process
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
        log_handle.close()


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
        json={"message": "Please can you sing this song?"},
    )
    assert chat_response.status_code == 200
    payload = chat_response.json()
    assert payload.get("type") == "chat_progress", f"Unexpected response: {payload}"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload.get("audio_url", "").startswith(f"/sessions/{session_id}/audio")

    audio_response = test_client.get(progress_payload["audio_url"])
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0


def test_backend_e2e_gemini_contextual_flow(gemini_client):
    test_client, data_dir, startup_id, logger = gemini_client
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    logger.info(
        "backend_e2e_context_start startup_id=%s session_id=%s data_dir=%s",
        startup_id,
        session_id,
        data_dir,
    )

    files = {"file": ("score.xml", SCORE_PATH.read_bytes(), "application/xml")}
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200

    first_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={
            "message": (
                "Can you read this song first? Don't sing it yet. "
                "Do you know this song?"
            )
        },
    )
    assert first_response.status_code == 200
    first_payload = first_response.json()
    assert first_payload["type"] == "chat_text"

    audio_missing = test_client.get(f"/sessions/{session_id}/audio")
    assert audio_missing.status_code == 404

    second_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={
            "message": (
                "Then can you sing it, with the tempo a bit slower then written? "
            )
        },
    )
    assert second_response.status_code == 200
    second_payload = second_response.json()
    assert second_payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, second_payload["progress_url"])
    assert progress_payload.get("audio_url", "").startswith(f"/sessions/{session_id}/audio")
    assert "current_score" in second_payload
    assert second_payload["current_score"]["version"] >= 2


def test_backend_e2e_gemini_requests_verse_selection(gemini_client):
    test_client, data_dir, startup_id, logger = gemini_client
    if not MULTI_VERSE_SCORE_PATH.exists():
        pytest.skip(f"Test score not found at {MULTI_VERSE_SCORE_PATH}")
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    logger.info(
        "backend_e2e_multiverse_start startup_id=%s session_id=%s data_dir=%s",
        startup_id,
        session_id,
        data_dir,
    )

    files = {
        "file": ("o-holy-night.xml", MULTI_VERSE_SCORE_PATH.read_bytes(), "application/xml")
    }
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Please sing this song."},
    )
    assert chat_response.status_code == 200
    payload = chat_response.json()
    assert payload.get("type") == "chat_text", f"Unexpected response: {payload}"
    assert "audio_url" not in payload
    message = (payload.get("message") or "").lower()
    assert "verse" in message


def test_backend_e2e_emulator_synthesize(emulator_gemini_client):
    test_client, _ = emulator_gemini_client
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]

    if not SCORE_PATH.exists():
        pytest.skip(f"Test score not found at {SCORE_PATH}")
    files = {
        "file": ("score.xml", SCORE_PATH.read_bytes(), "application/xml"),
    }
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat", json={"message": "please sing this song"}
    )
    assert chat_response.status_code == 200
    payload = chat_response.json()
    assert payload["type"] == "chat_progress"
    progress_payload = _wait_for_progress(test_client, payload["progress_url"])
    assert progress_payload["status"] == "done"
    audio_url = progress_payload["audio_url"]
    assert audio_url.startswith(f"/sessions/{session_id}/audio")

    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert audio_response.content.startswith((b"RIFF", b"ID3"))

    job_id = progress_payload.get("job_id")
    assert job_id
    job_doc = get_firestore_client().collection("jobs").document(job_id).get()
    assert job_doc.exists
    job_data = job_doc.to_dict()
    assert job_data.get("status") == "completed"
    output_path = job_data.get("outputPath")
    assert output_path
    assert blob_exists(os.getenv("STORAGE_BUCKET", "sightsinger-app.appspot.com"), output_path)


def _wait_for_progress(test_client, progress_url, timeout_seconds=120.0):
    deadline = time.time() + timeout_seconds
    last_payload = None
    while time.time() < deadline:
        response = test_client.get(progress_url)
        assert response.status_code == 200
        payload = response.json()
        last_payload = payload
        if payload.get("status") in ("done", "error"):
            return payload
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for progress: {last_payload}")
