"""Planner v2 wiring (SIG-164): the PREPROCESS_PLANNER switch, the chooser role, the v2 first attempt."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import subprocess
import sys
import threading
import urllib.request
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
from src.backend import orchestrator as orchestrator_module
from src.backend.orchestrator import (
    PREPROCESS_PLANNING_ERROR_MESSAGE, _planner_v2_prompt, _without_thought_summary,
)
from tests.test_backend_api import (  # noqa: F401 - pytest fixture
    _create_session, _preprocess_plan_response, _upload_score, client,
)
from tests.test_preprocess_chooser import _two_staff_part
from tests.test_preprocess_sheet import SATB, _chord_score, _too_many_chord_claims

ROOT = Path(__file__).resolve().parents[1]
DONE_MESSAGE = json.dumps({"tool_calls": [], "final_message": "Done.", "include_score": True})


def _first_enum(schema, dynamic_text=None):
    return json.dumps({key: prop["enum"][0] for key, prop in schema["properties"].items()})


def _rest_when_repairing(schema, dynamic_text):
    """Answer rest for a mode slot asked again with findings; else the first option."""
    reply = {}
    for entry in json.loads(dynamic_text.split("\n")[0])["slots"]:
        enum = schema["properties"][entry["key"]].get("enum") or []
        if entry["kind"] == "freeform":
            start, end = (int(bar) for bar in entry["bars"].split("-"))
            reply[entry["key"]] = [{"lane": lane, "sections": [
                {"start_measure": start, "end_measure": end, "mode": "rest"}]} for lane in entry["lanes"]]
        elif entry["kind"] == "mode" and entry.get("findings") and "rest" in enum:
            reply[entry["key"]] = "rest"
        else:
            reply[entry["key"]] = enum[0]
    return json.dumps(reply)


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
            return self._chooser_reply(response_schema, prompt_bundle.dynamic_prompt_text)
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


def test_chooser_batch_size_defaults_to_200_and_must_be_positive(monkeypatch):
    monkeypatch.delenv("PREPROCESS_CHOOSER_MAX_SLOTS_PER_REQUEST", raising=False)
    assert Settings.from_env().preprocess_chooser_max_slots_per_request == 200
    monkeypatch.setenv("PREPROCESS_CHOOSER_MAX_SLOTS_PER_REQUEST", "50")
    assert Settings.from_env().preprocess_chooser_max_slots_per_request == 50
    monkeypatch.setenv("PREPROCESS_CHOOSER_MAX_SLOTS_PER_REQUEST", "0")
    with pytest.raises(ValueError, match="PREPROCESS_CHOOSER_MAX_SLOTS_PER_REQUEST"):
        Settings.from_env()


def test_chooser_evidence_limit_defaults_to_400000_and_must_be_positive(monkeypatch):
    monkeypatch.delenv("PREPROCESS_CHOOSER_EVIDENCE_MAX_CHARS", raising=False)
    assert Settings.from_env().preprocess_chooser_evidence_max_chars == 400000
    monkeypatch.setenv("PREPROCESS_CHOOSER_EVIDENCE_MAX_CHARS", "50")
    assert Settings.from_env().preprocess_chooser_evidence_max_chars == 50
    monkeypatch.setenv("PREPROCESS_CHOOSER_EVIDENCE_MAX_CHARS", "0")
    with pytest.raises(ValueError, match="PREPROCESS_CHOOSER_EVIDENCE_MAX_CHARS"):
        Settings.from_env()


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


def _with_budget(calls):
    budget = orchestrator_module._LlmTurnBudget(reference="test", started_at=0.0, calls=calls)
    return budget, orchestrator_module._llm_turn_budget.set(budget)


def test_v2_batches_follow_the_setting_and_the_chooser_step_counts_as_one_call(client, monkeypatch):
    score = _two_staff_part()
    context = {"target": {"part_id": "P1-Combined"}}
    sheet = build_sheet(score, context)
    small = build_requests(sheet, build_request_context(score, context, sheet), max_slots_per_request=1)
    assert len(small) > 2
    llm = FakeLlm()
    orchestrator, _, run = _job(client, monkeypatch, planner="v2", llm=llm)
    monkeypatch.setattr(orchestrator, "_settings",
                        replace(orchestrator._settings, preprocess_chooser_max_slots_per_request=1))
    budget, token = _with_budget(0)
    try:
        job = run(score, context)
    finally:
        orchestrator_module._llm_turn_budget.reset(token)
    assert job["status"] == "completed", job
    chooser_calls = llm.roles().count(LlmRole.PREPROCESS_CHOOSER)
    assert chooser_calls == len(small)
    # One charge for the whole chooser step, one per other call (the closing message).
    assert budget.calls == 1 + len(llm.calls) - chooser_calls


def test_v2_chooser_step_stops_when_the_turn_budget_is_used_up(client, monkeypatch):
    score = _two_staff_part()
    llm = FakeLlm()
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    budget, token = _with_budget(orchestrator_module.LLM_CALLS_PER_TURN_LIMIT)
    try:
        job = run(score, {"target": {"part_id": "P1-Combined"}})
    finally:
        orchestrator_module._llm_turn_budget.reset(token)
    assert job["status"] == "failed"
    assert job["errorMessage"] == backend_message("chat.llm_turn_limit_reached", reference="test")
    assert llm.calls == [] and executed == []


def test_v2_chooser_accepts_a_reply_the_gemini_client_added_a_thought_summary_to(
    client, monkeypatch, tmp_path, caplog,
):
    from src.backend.llm_gemini import GeminiRestClient

    class NoCache:
        def ensure_prompt_cache(self, **kwargs):
            return None

    class Reply:
        def __init__(self, text):
            self._text = text

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [
                {"thought": True, "text": "thinking it over"}, {"text": self._text},
            ]}}]}).encode("utf-8")

    chooser_requests = []

    def fake_urlopen(request, timeout=None):
        schema = json.loads(request.data.decode("utf-8"))["generationConfig"].get("responseSchema")
        if schema is None:
            return Reply(DONE_MESSAGE)
        chooser_requests.append(schema)
        return Reply(_first_enum(schema))

    monkeypatch.setenv("GEMINI_INCLUDE_THOUGHT_SUMMARY", "1")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    gemini = GeminiRestClient(Settings.from_env(), api_key="dummy", cache_manager=NoCache())
    score = _chord_score(tmp_path)
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=gemini)
    with caplog.at_level(logging.INFO):
        job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "completed", job
    assert len(chooser_requests) == 1
    assert "retries=0" in caplog.text
    assert "planner_v2_evidence_chars count=" in caplog.text
    assert len(executed) == 1


@pytest.mark.parametrize("unavailable", ["too_large", "unreadable_path"])
def test_v2_job_continues_without_xml_when_evidence_is_unavailable(
    client, monkeypatch, tmp_path, caplog, unavailable,
):
    score = _chord_score(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    llm = FakeLlm()
    orchestrator, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    if unavailable == "too_large":
        monkeypatch.setattr(orchestrator, "_settings", replace(
            orchestrator._settings, preprocess_chooser_evidence_max_chars=1,
        ))
    else:
        score["source_musicxml_path"] = str(tmp_path / "missing.xml")
    with caplog.at_level(logging.INFO):
        job = run(score, context)
    assert job["status"] == "completed", job
    chooser_calls = [call for call in llm.calls if call["role"] == LlmRole.PREPROCESS_CHOOSER]
    assert len(chooser_calls) == 1
    assert "score_xml" not in json.loads(chooser_calls[0]["bundle"].dynamic_prompt_text)
    assert "planner_v2_evidence_chars count=0" in caplog.text
    assert f"planner_v2_evidence_unavailable reason={unavailable}" in caplog.text
    assert len(executed) == 1


def test_thought_summary_is_dropped_only_from_json_objects():
    assert json.loads(_without_thought_summary('{"s1": "a", "thought_summary": "x"}')) == {"s1": "a"}
    assert _without_thought_summary('{"s1": "a"}') == '{"s1": "a"}'
    assert _without_thought_summary("not json") == "not json"
    assert _without_thought_summary("[1]") == "[1]"


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


def test_v2_freeform_segment_is_written_by_the_chooser_and_runs(client, monkeypatch, tmp_path, caplog):
    score = _too_many_chord_claims(tmp_path)
    llm = FakeLlm(chooser_reply=_rest_when_repairing)
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    with caplog.at_level(logging.INFO):
        job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "completed", job
    assert "planner_v2_freeform_segment" in caplog.text
    assert LlmRole.PREPROCESS not in llm.roles()
    targets = executed[0]["request"]["plan"]["targets"]
    assert targets and all(target["sections"] == [{"start_measure": 1, "end_measure": 1, "mode": "rest"}]
                           for target in targets)


def test_v2_invalid_chooser_replies_fail_with_the_planning_message(client, monkeypatch, tmp_path, caplog):
    score = _chord_score(tmp_path)
    llm = FakeLlm(chooser_reply=lambda schema, dynamic_text: "{}")
    _, executed, run = _job(client, monkeypatch, planner="v2", llm=llm)
    with caplog.at_level(logging.WARNING):
        job = run(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert job["status"] == "failed"
    assert job["errorMessage"] == PREPROCESS_PLANNING_ERROR_MESSAGE
    assert "planner_v2_chooser_error" in caplog.text
    assert llm.roles() == [LlmRole.PREPROCESS_CHOOSER] * 2
    assert executed == []


_SILENT_BAR_XML = """<score-partwise version="3.1"><part-list>
<score-part id="P1"><part-name>Choir</part-name></score-part>
<score-part id="P2"><part-name>Lead</part-name></score-part></part-list>
<part id="P1">
  <measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
    <clef><sign>G</sign><line>2</line></clef></attributes>
    <note><pitch><step>G</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><lyric><text>one</text></lyric></note>
    <backup><duration>4</duration></backup>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration><voice>2</voice><type>whole</type><lyric><text>one</text></lyric></note>
  </measure>
  <measure number="2">
    <note><rest/><duration>4</duration><voice>1</voice><type>whole</type></note>
    <backup><duration>4</duration></backup>
    <note><rest/><duration>4</duration><voice>2</voice><type>whole</type></note>
  </measure>
  <measure number="3">
    <note><pitch><step>G</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><lyric><text>three</text></lyric></note>
    <backup><duration>4</duration></backup>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration><voice>2</voice><type>whole</type><lyric><text>three</text></lyric></note>
  </measure>
</part>
<part id="P2">
  <measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
    <clef><sign>G</sign><line>2</line></clef></attributes>
    <note><pitch><step>E</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><lyric><text>a</text></lyric></note>
  </measure>
  <measure number="2">
    <note><pitch><step>E</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><lyric><text>b</text></lyric></note>
  </measure>
  <measure number="3">
    <note><pitch><step>E</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><lyric><text>c</text></lyric></note>
  </measure>
</part></score-partwise>"""


def _silent_bar_score(tmp_path):
    from tests.test_preprocess_sheet import _parse_inline

    return _parse_inline(tmp_path, _SILENT_BAR_XML)


def _section_at(plan, target_index, measure):
    return assembler.expand(plan["targets"][target_index]["sections"])[measure]


def _failing(part_index, voice_part_id, measure, *, severity="P1", domain="LYRIC"):
    return {
        "status": "action_required",
        "action": "validation_failed_needs_review",
        "message": "Needs review.",
        "part_index": part_index,
        "target_voice_part_id": voice_part_id,
        "failed_validation_rules": [{
            "rule": "partial_lyric_coverage", "rule_name": "Partial Lyric Coverage",
            "rule_severity": severity, "rule_domain": domain,
            "impacted_measures": [measure], "impacted_ranges": [{"start": measure, "end": measure}],
        }],
    }


def _record_stops(orchestrator, monkeypatch):
    notices, stops = [], []
    monkeypatch.setattr(orchestrator, "_start_repair_notice", lambda *args, **kwargs: notices.append(1))
    render = orchestrator._render_selected_candidate_response

    async def recording_render(*args, **kwargs):
        stops.append((kwargs["stop_reason"], args[3].attempt_number if args[3] is not None else None))
        return await render(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_render_selected_candidate_response", recording_render)
    return notices, stops


def test_v2_repair_rests_a_silent_bar_and_the_next_attempt_passes(client, monkeypatch, tmp_path):
    score = _silent_bar_score(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    top = sheet.lanes[0]

    def rest_in_bar_two_passes(arguments):
        plan = arguments["request"]["plan"]
        if _section_at(plan, 0, 2)["mode"] == "rest":
            return _ready(arguments)
        return _failing(0, top.source_voice_part_id, 2)

    llm = FakeLlm(chooser_reply=_rest_when_repairing)
    orchestrator, executed, run = _job(client, monkeypatch, planner="v2", llm=llm,
                                       tool_result=rest_in_bar_two_passes)
    notices, _ = _record_stops(orchestrator, monkeypatch)
    published = []
    update_job = orchestrator._job_store.update_job

    def recording_update(job_id, **fields):
        published.extend(item.get("message") for item in (fields.get("details") or {}).get("attempt_messages") or [])
        return update_job(job_id, **fields)

    monkeypatch.setattr(orchestrator._job_store, "update_job", recording_update)
    job = run(score, context)
    assert job["status"] == "completed", job
    assert len(executed) == 2
    first, second = (call["request"]["plan"] for call in executed)
    assert _section_at(first, 0, 2)["mode"] == "derive" and _section_at(second, 0, 2)["mode"] == "rest"
    for index in range(len(first["targets"])):
        before = assembler.expand(first["targets"][index]["sections"])
        after = assembler.expand(second["targets"][index]["sections"])
        assert {measure for measure in before if before[measure] != after[measure]} <= ({2} if index == 0 else set())
    assert LlmRole.PREPROCESS not in llm.roles()
    assert notices == []
    assert backend_message("preprocess.v2_repairing") in published


def _lint_failed(top, measure):
    return {
        "status": "action_required",
        "action": "plan_lint_failed",
        "code": "plan_lint_failed",
        "message": "Plan lint failed.",
        "lint_findings": [{
            "rule": "weak_lyric_source_with_better_alternative", "rule_severity": "P1",
            "rule_domain": "LYRIC", "severity": "error", "message": "A better lyric source exists.",
            "part_index": top.part_index, "target_index": 0,
            "target_voice_part_id": top.source_voice_part_id,
            "section": {"start_measure": measure, "end_measure": measure},
        }],
    }


def _section_quality_failed(top, measure):
    return {
        "status": "action_required",
        "action": "section_lyric_quality_failed",
        "message": "Section-level lyric quality check failed.",
        "part_index": top.part_index,
        "target_voice_part": top.source_voice_part_id,
        "section_quality_issues": [{
            "code": "extension_only_output_with_word_source_available",
            "start_measure": measure, "end_measure": measure,
        }],
    }


@pytest.mark.parametrize("failure, rule", [
    (_lint_failed, "weak_lyric_source_with_better_alternative"),
    (_section_quality_failed, "extension_only_output_with_word_source_available"),
])
def test_v2_repair_reads_the_failing_bar_from_each_payload_shape(client, monkeypatch, tmp_path, failure, rule):
    score = _silent_bar_score(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    top = build_sheet(score, context).lanes[0]

    def rest_in_bar_two_passes(arguments):
        plan = arguments["request"]["plan"]
        return _ready(arguments) if _section_at(plan, 0, 2)["mode"] == "rest" else failure(top, 2)

    asked = []

    def recording_reply(schema, dynamic_text):
        asked.append(dynamic_text)
        return _rest_when_repairing(schema, dynamic_text)

    llm = FakeLlm(chooser_reply=recording_reply)
    orchestrator, executed, run = _job(client, monkeypatch, planner="v2", llm=llm,
                                       tool_result=rest_in_bar_two_passes)
    _, stops = _record_stops(orchestrator, monkeypatch)
    job = run(score, context)
    assert job["status"] == "completed", job
    assert len(executed) == 2 and "planner_v2_nothing_to_rechoose" not in [reason for reason, _ in stops]
    assert _section_at(executed[1]["request"]["plan"], 0, 2)["mode"] == "rest"
    repair_slots = [slot for text in asked for slot in json.loads(text)["slots"] if slot.get("findings")]
    assert repair_slots and all(finding["rule"] == rule and finding["bars"] == "2-2" and finding["message"]
                                for slot in repair_slots for finding in slot["findings"])


def test_v2_failures_list_a_finding_given_at_both_levels_once(client, monkeypatch, tmp_path):
    score = _silent_bar_score(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    top = sheet.lanes[0]
    orchestrator, _, _ = _job(client, monkeypatch, planner="v2", llm=FakeLlm())
    payload = _section_quality_failed(top, 2)
    payload["targets"] = [{**payload, "target_index": 0}]
    failing, findings = orchestrator._planner_v2_failures(sheet, payload)
    assert failing == {top.lane_id: [(2, 2)]} and len(findings[top.lane_id]) == 1


def test_v2_repair_with_nothing_to_rechoose_stops_without_an_llm_call(client, monkeypatch):
    score = parse_score(SATB, verse_number=1)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    llm = FakeLlm()
    orchestrator, executed, run = _job(
        client, monkeypatch, planner="v2", llm=llm,
        tool_result=lambda arguments: _failing(0, sheet.lanes[0].source_voice_part_id, 1),
    )
    _, stops = _record_stops(orchestrator, monkeypatch)
    run(score, {"target": {"part_id": "P1-Staff1"}})
    assert len(executed) == 1
    assert LlmRole.PREPROCESS_CHOOSER not in llm.roles() and LlmRole.PREPROCESS not in llm.roles()
    assert [reason for reason, _ in stops] == ["planner_v2_nothing_to_rechoose"]


def test_v2_repairs_stay_within_the_attempt_budget_and_keep_the_better_candidate(client, monkeypatch, tmp_path):
    score = _silent_bar_score(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    top = build_sheet(score, context).lanes[0]
    # Attempt 1 has only a P2 (quality class 2); both repairs come back worse, with a P1 (class 1).
    outcomes = iter([
        _failing(0, top.source_voice_part_id, 2, severity="P2"),
        _failing(0, top.source_voice_part_id, 2),
        _failing(0, top.source_voice_part_id, 2),
    ])
    llm = FakeLlm(chooser_reply=_rest_when_repairing)
    orchestrator, executed, run = _job(client, monkeypatch, planner="v2", llm=llm,
                                       tool_result=lambda arguments: next(outcomes))
    monkeypatch.setattr(orchestrator, "_settings", replace(orchestrator._settings, preprocess_max_attempts=3))
    _, stops = _record_stops(orchestrator, monkeypatch)
    run(score, context)
    assert len(executed) == 3
    # The first attempt's chooser step, then one per repair round.
    assert llm.roles().count(LlmRole.PREPROCESS_CHOOSER) == 3
    assert stops == [("attempt_budget_exhausted", 1)]


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
