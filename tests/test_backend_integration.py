import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.backend.main import create_app
from src.backend.llm_client import StaticLlmClient
from src.backend.credits import (
    CompleteJobAndSettleCreditsResult,
    ReleaseCreditsResult,
    ReserveCreditsResult,
    UserCredits,
)
from src.mcp.resolve import PROJECT_ROOT


VOICEBANK_ID = "Raine_Rena_2.01"
INLINE_SCORE_XML = b"""<?xml version='1.0' encoding='UTF-8'?>
<score-partwise version='3.1'>
  <part-list>
    <score-part id='P1'><part-name>Soprano</part-name></score-part>
  </part-list>
  <part id='P1'>
    <measure number='1'>
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>1</duration>
        <type>quarter</type>
        <lyric><text>la</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""


def _make_router_call_tool():
    def _call_tool(name, arguments):
        if name == "parse_score":
            musicxml_path = arguments.get("musicxml_path")
            return {
                "title": "Integration Test",
                "version": 1,
                "tempos": [],
                "parts": [{"part_id": "P1", "part_name": "Soprano", "notes": []}],
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
                    "title": "Integration Test",
                    "composer": None,
                    "lyricist": None,
                    "parts": [{"part_id": "P1", "part_index": 0, "part_name": "Soprano"}],
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
        if name == "list_voicebanks":
            return [{"id": VOICEBANK_ID, "name": VOICEBANK_ID, "path": f"assets/voicebanks/{VOICEBANK_ID}"}]
        if name == "get_voicebank_info":
            return {
                "id": VOICEBANK_ID,
                "name": VOICEBANK_ID,
                "languages": [],
                "has_duration_model": False,
                "has_pitch_model": False,
                "has_variance_model": False,
                "speakers": [],
                "sample_rate": 44100,
                "hop_size": 512,
                "use_lang_id": False,
                "voice_colors": [{"name": "02: soft", "suffix": "soft"}],
                "default_voice_color": "02: soft",
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
        raise AssertionError(f"Unexpected tool call: {name}")

    return _call_tool


def _auth_headers(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def integration_client(monkeypatch):
    data_dir = Path("tests/output/backend_integration") / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    fake_jobs: dict[str, dict] = {}
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("MCP_CPU_DEVICE", "cpu")
    monkeypatch.setenv("MCP_GPU_DEVICE", "cpu")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "test-user")
    monkeypatch.setattr(
        "src.backend.main.verify_id_token_claims",
        lambda token: {"uid": "test-user", "email": "test-user@example.com"},
    )
    async def _allow_credits(_user_id: str, _user_email: str) -> None:
        return None
    monkeypatch.setattr("src.backend.main._require_active_credits", _allow_credits)
    monkeypatch.setattr(
        "src.backend.credits.get_or_create_credits",
        lambda user_id, user_email: UserCredits(
            balance=9999,
            reserved=0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            overdrafted=False,
        ),
    )
    monkeypatch.setattr(
        "src.backend.credits.reserve_credits",
        lambda *_, **__: ReserveCreditsResult(status="reserved", estimated_credits=1),
    )
    def _fake_complete_and_settle(
        user_id: str,
        job_id: str,
        session_id: str,
        duration_seconds: float,
        *,
        output_path: str | None = None,
        audio_url: str | None = None,
    ) -> CompleteJobAndSettleCreditsResult:
        payload = fake_jobs.setdefault(job_id, {})
        payload.update(
            {
                "userId": user_id,
                "sessionId": session_id,
                "status": "completed",
                "step": "done",
                "message": "Your take is ready.",
                "progress": 1.0,
                "audioUrl": audio_url,
                "outputPath": output_path,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        return CompleteJobAndSettleCreditsResult(
            status="completed_and_settled",
            actual_credits=1,
            overdrafted=False,
        )

    monkeypatch.setattr(
        "src.backend.credits.settle_credits_and_complete_job",
        _fake_complete_and_settle,
    )
    monkeypatch.setattr(
        "src.backend.credits.release_credits",
        lambda *_, **__: ReleaseCreditsResult(status="released"),
    )

    def _fake_create_job(self, *, job_id: str, user_id: str, session_id: str, status: str, **kwargs):
        fake_jobs[job_id] = {
            "jobId": job_id,
            "userId": user_id,
            "sessionId": session_id,
            "status": status,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }

    def _fake_update_job(self, job_id: str, **kwargs):
        payload = fake_jobs.setdefault(job_id, {"jobId": job_id})
        payload.update(kwargs)
        payload["updatedAt"] = datetime.now(timezone.utc).isoformat()

    def _fake_get_latest_job_by_session(self, *, user_id: str, session_id: str):
        matches = [
            (job_id, payload)
            for job_id, payload in fake_jobs.items()
            if payload.get("userId") == user_id and payload.get("sessionId") == session_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[1].get("updatedAt") or "")

    def _fake_clear_jobs_for_session(self, *, user_id: str, session_id: str):
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
    app = create_app()
    app.state.router.call_tool = _make_router_call_tool()
    llm_client = StaticLlmClient(
        response_text=(
            '{"tool_calls":[{"name":"synthesize","arguments":{"voicebank":"'
            + VOICEBANK_ID
            + '"}}],"final_message":"Rendered.","include_score":true}'
        )
    )
    app.state.llm_client = llm_client
    app.state.orchestrator._llm_client = llm_client
    with TestClient(app) as test_client:
        test_client.headers.update(_auth_headers())
        yield test_client, app


def test_backend_integration_synthesize(integration_client):
    test_client, _ = integration_client
    response = test_client.post("/sessions")
    assert response.status_code == 200
    session_id = response.json()["session_id"]

    files = {"file": ("score.xml", INLINE_SCORE_XML, "application/xml")}
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
    assert chat_payload["current_score"]["version"] >= 1

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

    files = {"file": ("score.xml", INLINE_SCORE_XML, "application/xml")}
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
