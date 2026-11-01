"""Planner v2 wiring (SIG-164): the PREPROCESS_PLANNER switch, the chooser role, the v2 first attempt."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src.api import parse_score
from src.api import preprocess_assembler as assembler
from src.api.preprocess_chooser import build_request_context, build_requests
from src.api.preprocess_sheet import build_sheet
from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_prompt import PromptBundle
from src.backend.message_catalog import backend_message
from src.backend.orchestrator import PREPROCESS_PLANNING_ERROR_MESSAGE, _planner_v2_prompt
from tests.test_backend_api import (  # noqa: F401 - pytest fixture
    _create_session, _preprocess_plan_response, _upload_score, client,
)
from tests.test_preprocess_chooser import _two_staff_part
from tests.test_preprocess_sheet import SATB, _chord_score, _too_many_chord_claims

ROOT = Path(__file__).resolve().parents[1]
DONE_MESSAGE = json.dumps({"tool_calls": [], "final_message": "Done.", "include_score": True})


def _first_enum(schema):
    return json.dumps({key: prop["enum"][0] for key, prop in schema["properties"].items()})


class FakeLlm:
    """Records every call; chooser calls answer with each slot's first option."""

    def __init__(self, *, chooser_reply=None, chooser_barrier=None):
        self.calls: list[dict] = []
        self._chooser_reply = chooser_reply or _first_enum
        self._barrier = chooser_barrier
        self._lock = threading.Lock()

    def generate(self, prompt_bundle, history, *, role=LlmRole.DEFAULT, response_schema=None):
        with self._lock:
            self.calls.append({"role": role, "bundle": prompt_bundle, "history": list(history),
                               "schema": response_schema})
        if role == LlmRole.PREPROCESS_CHOOSER:
            if self._barrier is not None:
                self._barrier.wait()
            return self._chooser_reply(response_schema)
        if role == LlmRole.PREPROCESS:
            return _preprocess_plan_response(voice_part_id="voice part 1")
        return DONE_MESSAGE

    def roles(self):
        return [call["role"] for call in self.calls]


def _ready(arguments):
    return {"status": "ready", "score": arguments.get("score", {}), "part_index": 0}


def _job(client, monkeypatch, *, planner, llm, tool_result=_ready):
    """A session with a score, the planner switch set, and the preprocess tool stubbed."""
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    orchestrator = app.state.orchestrator
    monkeypatch.setattr(orchestrator, "_settings", replace(orchestrator._settings, preprocess_planner=planner))
    monkeypatch.setattr(orchestrator, "_llm_client", llm)
    executed: list[dict] = []

    def call_tool(name, arguments):
        if name == "preprocess_voice_parts":
            executed.append(arguments)
            return tool_result(arguments)
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(app.state.router, "call_tool", call_tool)
    job_id = f"job-{session_id[:8]}"
    app.state.job_store.create_job(job_id=job_id, user_id="test-user", session_id=session_id, status="queued")

    def run(score, planning_context):
        asyncio.run(orchestrator._run_preprocess_job(
            session_id, score, [], job_id, "test-user", "test@example.com",
            initial_message="Preparing the line.", initial_thought_summary="",
            planning_context=planning_context,
        ))
        _, job = app.state.job_store.get_job_by_id(job_id=job_id, user_id="test-user", session_id=session_id)
        return job

    return orchestrator, executed, run


# --- Settings -------------------------------------------------------------------

def test_planner_switch_defaults_to_v1_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv("PREPROCESS_PLANNER", raising=False)
    assert Settings.from_env().preprocess_planner == "v1"
    monkeypatch.setenv("PREPROCESS_PLANNER", "v2")
    assert Settings.from_env().preprocess_planner == "v2"
    monkeypatch.setenv("PREPROCESS_PLANNER", "v3")
    with pytest.raises(ValueError, match="PREPROCESS_PLANNER"):
        Settings.from_env()


def test_chooser_role_defaults_follow_the_preprocess_model_and_can_be_overridden(monkeypatch):
    for name in ("GEMINI_PREPROCESS_CHOOSER_MODEL", "GEMINI_PREPROCESS_CHOOSER_THINKING_LEVEL",
                 "GEMINI_PREPROCESS_CHOOSER_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_PREPROCESS_MODEL", "preprocess-model")
    chooser = Settings.from_env().gemini_preprocess_chooser
    assert (chooser.model, chooser.thinking_level, chooser.timeout_seconds) == ("preprocess-model", "medium", 120.0)
    monkeypatch.setenv("GEMINI_PREPROCESS_CHOOSER_MODEL", "chooser-model")
    monkeypatch.setenv("GEMINI_PREPROCESS_CHOOSER_THINKING_LEVEL", "low")
    monkeypatch.setenv("GEMINI_PREPROCESS_CHOOSER_TIMEOUT_SECONDS", "45")
    chooser = Settings.from_env().gemini_preprocess_chooser
    assert (chooser.model, chooser.thinking_level, chooser.timeout_seconds) == ("chooser-model", "low", 45.0)


def test_gemini_client_uses_the_chooser_role_config(monkeypatch):
    from src.backend.llm_gemini import GeminiRestClient

    monkeypatch.setenv("GEMINI_PREPROCESS_CHOOSER_MODEL", "chooser-model")
    monkeypatch.setenv("GEMINI_PREPROCESS_CHOOSER_TIMEOUT_SECONDS", "45")
    settings = Settings.from_env()

    class NoCache:
        def ensure_prompt_cache(self, **kwargs):
            return None

    gemini = GeminiRestClient(settings, api_key="test", cache_manager=NoCache())
    seen = {}

    def fake_generate(payload, *, model, timeout, thinking_level, role):
        seen.update(model=model, timeout=timeout, thinking_level=thinking_level, payload=payload)
        return "{}"

    monkeypatch.setattr(gemini, "_generate_with_payload", fake_generate)
    schema = {"type": "object", "properties": {"s1": {"type": "string", "enum": ["a"]}}}
    gemini.generate(PromptBundle("static", "dynamic"), [], role=LlmRole.PREPROCESS_CHOOSER, response_schema=schema)
    assert (seen["model"], seen["timeout"], seen["thinking_level"]) == ("chooser-model", 45.0, "medium")
    assert seen["payload"]["generationConfig"]["responseSchema"] == schema


# --- The preprocess job ---------------------------------------------------------

def test_v1_job_plans_with_the_preprocess_role_and_never_imports_the_chooser(client, monkeypatch, tmp_path):
    llm = FakeLlm()
    orchestrator, executed, run = _job(client, monkeypatch, planner="v1", llm=llm)
    monkeypatch.setitem(sys.modules, "src.api.preprocess_chooser", None)
    planned = []
    original = orchestrator._plan_preprocess_with_llm

    async def recording_plan(*args, **kwargs):
        planned.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_plan_preprocess_with_llm", recording_plan)
    score = _chord_score(tmp_path)
    job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "completed"
    assert len(planned) == 1
    assert LlmRole.PREPROCESS_CHOOSER not in llm.roles()
    assert llm.roles().count(LlmRole.PREPROCESS) == 1
    assert len(executed) == 1


def test_v2_job_asks_only_the_chooser_in_parallel_and_executes_the_assembled_plan(client, monkeypatch):
    score = _two_staff_part()
    context = {"target": {"part_id": "P1-Combined"}}
    sheet = build_sheet(score, context)
    requests = build_requests(sheet, build_request_context(score, context, sheet))
    assert len(requests) == 2
    llm = FakeLlm(chooser_barrier=threading.Barrier(2, timeout=5))
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    job = run(score, context)
    assert job["status"] == "completed", job
    chooser_calls = [call for call in llm.calls if call["role"] == LlmRole.PREPROCESS_CHOOSER]
    assert len(chooser_calls) == len(requests)
    assert LlmRole.PREPROCESS not in llm.roles()
    for call in chooser_calls:
        assert call["bundle"].static_prompt_text == _planner_v2_prompt()
        assert call["history"] == []
        assert call["schema"]["required"]
    assert sorted(call["bundle"].dynamic_prompt_text for call in chooser_calls) == sorted(
        request.dynamic_text for request in requests
    )
    expected = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert len(executed) == 1
    assert executed[0]["request"]["plan"] == expected


def test_v2_job_without_lyrics_stops_with_the_catalog_message_and_no_llm_call(client, monkeypatch):
    score = parse_score(SATB, verse_number=1)
    for part in score["parts"]:
        for note in part["notes"]:
            note["lyric"] = None
            note["lyric_is_extended"] = False
    llm = FakeLlm()
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    job = run(score, {"target": {"part_id": "P1-Staff1"}})
    assert job["status"] == "failed"
    assert job["errorMessage"] == backend_message("preprocess.score_needs_lyrics")
    assert llm.calls == [] and executed == []


def test_v2_freeform_segment_fails_with_the_planning_message_and_no_v1_fallback(client, monkeypatch, tmp_path, caplog):
    score = _too_many_chord_claims(tmp_path)
    llm = FakeLlm()
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    with caplog.at_level(logging.WARNING):
        job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "failed"
    assert job["errorMessage"] == PREPROCESS_PLANNING_ERROR_MESSAGE
    assert "planner_v2_freeform_segment" in caplog.text
    assert llm.calls == [] and executed == []


def test_v2_invalid_chooser_replies_fail_with_the_planning_message(client, monkeypatch, tmp_path, caplog):
    score = _chord_score(tmp_path)
    llm = FakeLlm(chooser_reply=lambda schema: "{}")
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    with caplog.at_level(logging.WARNING):
        job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "failed"
    assert job["errorMessage"] == PREPROCESS_PLANNING_ERROR_MESSAGE
    assert "planner_v2_chooser_error" in caplog.text
    assert llm.roles() == [LlmRole.PREPROCESS_CHOOSER] * 2
    assert executed == []


def test_v2_attempt_that_needs_action_stops_without_repair(client, monkeypatch, tmp_path):
    def needs_review(arguments):
        return {
            "status": "action_required",
            "action": "validation_failed_needs_review",
            "message": "Needs review.",
            "failed_validation_rules": [{
                "rule": "validation_failed_needs_review", "rule_name": "Coverage Needs Review",
                "rule_severity": "P1", "rule_domain": "LYRIC",
                "impacted_measures": [1], "impacted_ranges": [{"start": 1, "end": 1}],
            }],
        }

    llm = FakeLlm()
    orchestrator, executed, run = _job(client, monkeypatch, planner="v2", llm=llm, tool_result=needs_review)
    notices, stops = [], []
    monkeypatch.setattr(orchestrator, "_start_repair_notice", lambda *args, **kwargs: notices.append(1))
    render = orchestrator._render_selected_candidate_response

    async def recording_render(*args, **kwargs):
        stops.append(kwargs["stop_reason"])
        return await render(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_render_selected_candidate_response", recording_render)
    score = _chord_score(tmp_path)
    run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(executed) == 1
    assert LlmRole.PREPROCESS not in llm.roles()
    assert notices == []
    assert stops == ["planner_v2_repair_pending"]


# --- Replay ---------------------------------------------------------------------

def test_replay_runs_a_synthetic_job_through_the_v2_path(tmp_path):
    score = _chord_score(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    replies = [_first_enum(request.response_schema)
               for request in build_requests(sheet, build_request_context(score, context, sheet))]
    assert replies
    corpus = tmp_path / "corpus"
    (corpus / "job-1").mkdir(parents=True)
    (corpus / "job-1" / "input.xml").write_bytes((tmp_path / "synthetic.xml").read_bytes())
    (corpus / "job-1" / "job.json").write_text(json.dumps({
        "spec": {"toolCalls": [], "planningContext": context, "initialMessage": "Preparing."},
    }))
    static = tmp_path / "replies.json"
    static.write_text(json.dumps({"preprocess_chooser": replies}))
    out = tmp_path / "out"
    completed = subprocess.run(
        [sys.executable, "scripts/replay/run_preprocess_replay.py", "--corpus", str(corpus),
         "--out", str(out), "--label", "v2-smoke", "--planner", "v2", "--static-llm-responses", str(static)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = json.loads((out / "jobs" / "job-1.json").read_text())
    assert result["status"] == "completed", result
    roles = [call["role"] for call in result["llm_calls"]]
    assert "preprocess" not in roles
    assert roles.count("preprocess_chooser") == len(replies)
    assert json.loads((out / "run.json").read_text())["planner"] == "v2"
