import logging
import os
import shutil
import subprocess
import time
import uuid
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
import firebase_admin

from src.backend.main import create_app
from src.backend.firebase_app import get_firestore_client
from src.backend.llm_client import StaticLlmClient
from src.backend.llm_prompt import parse_llm_response
from src.backend.storage_client import blob_exists
from src.mcp.resolve import PROJECT_ROOT


VOICEBANK_ID = "Raine_Rena_2.01"
SCORE_PATH = PROJECT_ROOT / "assets/test_data/amazing-grace-satb-verse1.xml"
MULTI_VERSE_SCORE_PATH = PROJECT_ROOT / "assets/test_data/o-holy-night.xml"
VERSE_CHANGE_SCORE_PATH = (
    PROJECT_ROOT / "assets/test_data/amazing-grace-satb-zipped/lg-21486226.xml"
)
MY_TRIBUTE_SLICE_PATH = PROJECT_ROOT / "assets/test_data/my-tribute-bars19-36.xml"


class RecordingLlmClient:
    """Decorator that records every raw LLM response to a JSONL file."""

    def __init__(self, inner, output_path: Path):
        self._inner = inner
        self._output_path = output_path
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.touch(exist_ok=True)

    def generate(self, system_prompt, history):
        text = self._inner.generate(system_prompt, history)
        preprocess_context_text = _extract_prompt_section(
            system_prompt,
            start_marker="Preprocess-derived mapping context (if available):",
            end_marker="Available voicebanks (IDs):",
        )
        preprocess_context_summary = _summarize_preprocess_context(preprocess_context_text)
        parsed_response = parse_llm_response(text)
        entry = {
            "timestamp": time.time(),
            "history_length": len(history or []),
            "last_role": (history[-1].get("role") if history else None),
            "last_content": (history[-1].get("content") if history else None),
            "response_text": text,
            "prompt_preprocess_mapping_context_present": preprocess_context_summary["present"],
            "prompt_preprocess_mapping_targets_count": preprocess_context_summary["targets_count"],
            "prompt_preprocess_mapping_targets": preprocess_context_summary["targets"],
            "response_tool_calls": [
                {"name": call.name, "arguments": call.arguments}
                for call in (parsed_response.tool_calls if parsed_response else [])
            ],
            "response_final_message": parsed_response.final_message if parsed_response else None,
        }
        with self._output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return text


def _extract_prompt_section(system_prompt: str, *, start_marker: str, end_marker: str) -> str:
    start = system_prompt.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = system_prompt.find(end_marker, start)
    if end < 0:
        end = len(system_prompt)
    return system_prompt[start:end].strip()


def _summarize_preprocess_context(section_text: str) -> dict:
    text = (section_text or "").strip()
    if not text or text == "none":
        return {"present": False, "targets_count": 0, "targets": []}
    try:
        parsed = json.loads(text)
    except Exception:
        return {"present": True, "targets_count": -1, "targets": []}
    derived_mapping = parsed.get("derived_mapping") if isinstance(parsed, dict) else None
    targets = derived_mapping.get("targets") if isinstance(derived_mapping, dict) else None
    if not isinstance(targets, list):
        return {"present": True, "targets_count": 0, "targets": []}
    summary_targets = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        summary_targets.append(
            {
                "derived_part_index": target.get("derived_part_index"),
                "derived_part_id": target.get("derived_part_id"),
                "target_voice_part_id": target.get("target_voice_part_id"),
                "source_part_index": target.get("source_part_index"),
                "source_voice_part_id": target.get("source_voice_part_id"),
            }
        )
    return {
        "present": True,
        "targets_count": len(summary_targets),
        "targets": summary_targets,
    }


def _read_llm_log_entries(log_path: Path) -> list[dict]:
    entries: list[dict] = []
    if not log_path.exists():
        return entries
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


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
    firestore_host = os.getenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("FIREBASE_PROJECT_ID", "demo-test"))
    _ensure_emulator_available(firestore_host, "Firestore")
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash-preview")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", firestore_host)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", project_id)
    monkeypatch.setenv("MCP_CPU_DEVICE", os.getenv("MCP_CPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_GPU_DEVICE", os.getenv("MCP_GPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_DEBUG", "true")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr(
        "src.backend.main.verify_id_token_claims",
        lambda token: {"uid": "test-user", "email": "test-user@example.com"},
    )
    async def _allow_credits(_user_id: str, _user_email: str) -> None:
        return None
    monkeypatch.setattr("src.backend.main._require_active_credits", _allow_credits)

    class _UnlimitedCredits:
        overdrafted = False
        available_balance = 10**9

        @property
        def is_expired(self) -> bool:
            return False

    monkeypatch.setattr(
        "src.backend.credits.get_or_create_credits",
        lambda uid, email: _UnlimitedCredits(),
    )
    monkeypatch.setattr("src.backend.credits.reserve_credits", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.backend.credits.settle_credits", lambda *args, **kwargs: (0, False))
    monkeypatch.setattr("src.backend.credits.release_credits", lambda *args, **kwargs: True)

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

    _reset_firebase_admin()
    app = create_app()
    llm_log_path = data_dir / "llm_responses.jsonl"
    if app.state.llm_client is None:
        pytest.skip("LLM client is not configured.")
    recording_llm_client = RecordingLlmClient(app.state.llm_client, llm_log_path)
    app.state.llm_client = recording_llm_client
    app.state.orchestrator._llm_client = recording_llm_client
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
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
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
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash-preview")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
    monkeypatch.setenv("MCP_CPU_DEVICE", os.getenv("MCP_CPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_GPU_DEVICE", os.getenv("MCP_GPU_DEVICE", "cpu"))
    monkeypatch.setenv("MCP_DEBUG", "true")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "true")
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", f"http://{storage_host}")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")

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


def test_backend_e2e_gemini_my_tribute_parse_preprocess_synthesize(gemini_client):
    test_client, data_dir, startup_id, logger = gemini_client
    if not MY_TRIBUTE_SLICE_PATH.exists():
        pytest.skip(f"Test score not found at {MY_TRIBUTE_SLICE_PATH}")

    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    logger.info(
        "backend_e2e_my_tribute_start startup_id=%s session_id=%s data_dir=%s",
        startup_id,
        session_id,
        data_dir,
    )

    files = {
        "file": (
            "my-tribute-bars19-36.xml",
            MY_TRIBUTE_SLICE_PATH.read_bytes(),
            "application/xml",
        )
    }
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    assert upload_payload.get("parsed") is True

    chat_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "sing the soprano part of this song"},
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    assert chat_payload.get("type") == "chat_text", (
        "Expected preprocess-review response in first turn. "
        f"response={chat_payload}"
    )
    assert bool(chat_payload.get("review_required")) is True, (
        "Expected review_required=true after preprocess. "
        f"response={chat_payload}"
    )
    assert "current_score" in chat_payload, (
        "Expected current_score in review response for frontend score reload. "
        f"response={chat_payload}"
    )

    proceed_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Looks good. Please proceed with audio generation."},
    )
    assert proceed_response.status_code == 200
    proceed_payload = proceed_response.json()
    if proceed_payload.get("type") != "chat_progress":
        pytest.fail(
            "Expected synthesize to start after user confirmation. "
            f"response={proceed_payload}"
        )

    progress_payload = _wait_for_progress(test_client, proceed_payload["progress_url"])
    assert progress_payload.get("status") == "done", (
        "Synthesis job failed or aborted. "
        f"progress={progress_payload}"
    )
    audio_url = progress_payload.get("audio_url", "")
    assert audio_url.startswith(f"/sessions/{session_id}/audio")
    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0

    llm_log_path = data_dir / "llm_responses.jsonl"
    entries = _read_llm_log_entries(llm_log_path)
    assert entries, f"No LLM responses logged at {llm_log_path}"

    saw_preprocess = False
    saw_synthesize = False
    tool_call_sequence: list[str] = []
    for index, entry in enumerate(entries, start=1):
        response_text = str(entry.get("response_text", ""))
        logger.info("llm_response_%d %s", index, response_text)
        parsed = parse_llm_response(response_text)
        if parsed is None:
            continue
        if not parsed.tool_calls and "?" in parsed.final_message:
            pytest.fail(
                "LLM asked for clarification instead of completing workflow. "
                f"response={parsed.final_message}"
            )
        for call in parsed.tool_calls:
            tool_call_sequence.append(call.name)
            if call.name == "preprocess_voice_parts":
                saw_preprocess = True
                request_payload = call.arguments.get("request")
                if not isinstance(request_payload, dict) or not isinstance(
                    request_payload.get("plan"), dict
                ):
                    pytest.fail(
                        "preprocess_voice_parts must include request.plan in LLM tool call. "
                        f"arguments={call.arguments}"
                    )
                plan_payload = request_payload.get("plan") or {}
                targets = plan_payload.get("targets")
                if not isinstance(targets, list) or not targets:
                    pytest.fail(
                        "preprocess request.plan must contain non-empty targets list. "
                        f"plan={plan_payload}"
                    )
                for target in targets:
                    if not isinstance(target, dict) or not isinstance(target.get("sections"), list):
                        pytest.fail(
                            "preprocess plan target must use sections-only Mode B. "
                            f"target={target}"
                        )
                    if "actions" in target:
                        pytest.fail(
                            "preprocess plan target must not include actions (Mode A is not exposed). "
                            f"target={target}"
                        )
                if "voice_id" in call.arguments or "voice_id" in request_payload:
                    pytest.fail(
                        "Deprecated voice_id detected in preprocess_voice_parts tool call. "
                        f"arguments={call.arguments}"
                    )
            if call.name == "synthesize":
                saw_synthesize = True

    assert saw_preprocess, (
        "Expected preprocess_voice_parts tool call was not observed in LLM responses."
    )
    assert saw_synthesize, (
        "Expected synthesize tool call was not observed in LLM responses."
    )
    preprocess_indices = [
        idx for idx, name in enumerate(tool_call_sequence) if name == "preprocess_voice_parts"
    ]
    synth_indices = [idx for idx, name in enumerate(tool_call_sequence) if name == "synthesize"]
    assert synth_indices, "Expected at least one synthesize tool call in tool call sequence."
    first_synth_idx = synth_indices[0]
    preprocess_before_synth = [idx for idx in preprocess_indices if idx < first_synth_idx]
    assert preprocess_before_synth, (
        "Expected preprocess_voice_parts before synthesize for this complex score workflow."
    )
    assert len(preprocess_before_synth) <= 4, (
        "LLM exceeded max repair budget: expected at most 4 preprocess attempts "
        "(initial + three repairs) before synthesize. "
        f"tool_call_sequence={tool_call_sequence}"
    )


def test_backend_e2e_gemini_verse_change_reparse_preprocess_review_synthesize(gemini_client):
    test_client, data_dir, startup_id, logger = gemini_client
    if not VERSE_CHANGE_SCORE_PATH.exists():
        pytest.skip(f"Test score not found at {VERSE_CHANGE_SCORE_PATH}")

    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    logger.info(
        "backend_e2e_verse_change_start startup_id=%s session_id=%s data_dir=%s",
        startup_id,
        session_id,
        data_dir,
    )

    files = {
        "file": (
            "lg-21486226.xml",
            VERSE_CHANGE_SCORE_PATH.read_bytes(),
            "application/xml",
        )
    }
    upload_response = test_client.post(f"/sessions/{session_id}/upload", files=files)
    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    assert upload_payload.get("parsed") is True
    available_verses = (
        ((upload_payload.get("score_summary") or {}).get("available_verses")) or []
    )
    assert "2" in [str(v) for v in available_verses], (
        "Expected verse-change fixture to expose multiple verses. "
        f"available_verses={available_verses}"
    )

    # Turn 1: verse 1 -> preprocess -> review gate
    verse1_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Sing the soprano part of verse 1."},
    )
    assert verse1_response.status_code == 200
    verse1_payload = verse1_response.json()
    assert verse1_payload.get("type") == "chat_text", (
        "Expected preprocess-review response for verse 1 request. "
        f"response={verse1_payload}"
    )
    assert bool(verse1_payload.get("review_required")) is True, (
        "Expected review_required=true after first preprocess. "
        f"response={verse1_payload}"
    )

    # Turn 2: user changes verse -> reparse -> preprocess -> review gate
    verse2_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={
            "message": (
                "Change to verse 2 instead. Rebuild the part split and let me review "
                "before audio generation."
            )
        },
    )
    assert verse2_response.status_code == 200
    verse2_payload = verse2_response.json()
    assert verse2_payload.get("type") == "chat_text", (
        "Expected preprocess-review response after verse change. "
        f"response={verse2_payload}"
    )
    assert bool(verse2_payload.get("review_required")) is True, (
        "Expected review_required=true after verse-change reparse/preprocess. "
        f"response={verse2_payload}"
    )

    # Turn 3: confirm review -> synth
    proceed_response = test_client.post(
        f"/sessions/{session_id}/chat",
        json={"message": "Looks good now. Proceed to synthesis."},
    )
    assert proceed_response.status_code == 200
    proceed_payload = proceed_response.json()
    if proceed_payload.get("type") != "chat_progress":
        pytest.fail(
            "Expected synthesize to start after verse-change review confirmation. "
            f"response={proceed_payload}"
        )

    progress_payload = _wait_for_progress(test_client, proceed_payload["progress_url"])
    assert progress_payload.get("status") == "done", (
        "Synthesis job failed or aborted after verse-change flow. "
        f"progress={progress_payload}"
    )
    audio_url = progress_payload.get("audio_url", "")
    assert audio_url.startswith(f"/sessions/{session_id}/audio")
    audio_response = test_client.get(audio_url)
    assert audio_response.status_code == 200
    assert len(audio_response.content) > 0

    # Verify LLM tool-call sequence explicitly includes reparse -> preprocess -> synth.
    llm_log_path = data_dir / "llm_responses.jsonl"
    entries = _read_llm_log_entries(llm_log_path)
    assert entries, f"No LLM responses logged at {llm_log_path}"

    tool_call_sequence: list[str] = []
    for index, entry in enumerate(entries, start=1):
        response_text = str(entry.get("response_text", ""))
        logger.info("verse_change_llm_response_%d %s", index, response_text)
        parsed = parse_llm_response(response_text)
        if parsed is None:
            continue
        for call in parsed.tool_calls:
            tool_call_sequence.append(call.name)

    preprocess_indices = [
        idx for idx, name in enumerate(tool_call_sequence) if name == "preprocess_voice_parts"
    ]
    reparse_indices = [idx for idx, name in enumerate(tool_call_sequence) if name == "reparse"]
    synth_indices = [idx for idx, name in enumerate(tool_call_sequence) if name == "synthesize"]

    assert preprocess_indices, (
        "Expected preprocess_voice_parts call(s) in verse-change workflow. "
        f"tool_call_sequence={tool_call_sequence}"
    )
    assert reparse_indices, (
        "Expected reparse call after verse change. "
        f"tool_call_sequence={tool_call_sequence}"
    )
    assert synth_indices, (
        "Expected synthesize call after review confirmation. "
        f"tool_call_sequence={tool_call_sequence}"
    )

    assert len(preprocess_indices) >= 2, (
        "Expected at least two preprocess calls (initial + after verse change). "
        f"tool_call_sequence={tool_call_sequence}"
    )
    reparsed_with_preprocess_before_and_after = any(
        any(p < r for p in preprocess_indices) and any(p > r for p in preprocess_indices)
        for r in reparse_indices
    )
    assert reparsed_with_preprocess_before_and_after, (
        "Expected a reparse positioned between two preprocess calls in verse-change flow. "
        f"tool_call_sequence={tool_call_sequence}"
    )
    first_synth_idx = synth_indices[0]
    preprocess_before_synth = [idx for idx in preprocess_indices if idx < first_synth_idx]
    assert preprocess_before_synth, (
        "Expected at least one preprocess call before synthesize. "
        f"tool_call_sequence={tool_call_sequence}"
    )
    latest_preprocess_before_synth = max(preprocess_before_synth)
    assert latest_preprocess_before_synth < first_synth_idx, (
        "Expected synthesize only after reparse+preprocess and review. "
        f"tool_call_sequence={tool_call_sequence}"
    )


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
