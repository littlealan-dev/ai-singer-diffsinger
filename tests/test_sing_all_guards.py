"""A running Sing All blocks score edits only in its own Song."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.backend import main, orchestrator as orchestrator_module
from src.backend.llm_prompt import ToolCall


_EDIT_TOOLS = [
    "reparse", "preprocess_voice_parts", "prepare_synthesis_quote", "synthesize",
    "add_solfege_lyric_verse", "modify_solfege_settings", "replace_birthday_name",
]


class _ReachedTool(Exception):
    pass


def _guarded_orchestrator(monkeypatch):
    async def no_stop():
        return None

    async def reached_snapshot(*_args):
        raise _ReachedTool

    async def reached_quote(*_args, **_kwargs):
        raise _ReachedTool

    def reached(*_args, **_kwargs):
        raise _ReachedTool

    monkeypatch.setattr(
        orchestrator_module, "active_job_for_song",
        lambda _uid, session_id: {"status": "running"} if session_id == "active-song" else None,
    )
    orchestrator = object.__new__(orchestrator_module.Orchestrator)
    orchestrator._logger = logging.getLogger(__name__)
    orchestrator._settings = SimpleNamespace(session_store="firestore")
    orchestrator._check_chat_turn_stopped = no_stop
    orchestrator._sessions = SimpleNamespace(get_snapshot=reached_snapshot)
    orchestrator._quote_synthesis = reached_quote
    orchestrator._tool_calls_require_verse_selection = reached
    orchestrator._without_ignored_synthesis_target_fields = reached
    return orchestrator


def _run_tool(orchestrator, song_id, name):
    return asyncio.run(orchestrator._execute_tool_calls(
        song_id, {"parts": [{"part_id": "P1"}]},
        [ToolCall(name=name, arguments={})], user_id="made-up-owner",
        score_summary={"parts": [{"part_id": "P1"}]}, user_email="",
        explicit_verse_number=None,
    ))


@pytest.mark.parametrize("name", _EDIT_TOOLS)
def test_chat_edit_tools_refuse_active_song_and_reach_handler_in_other_song(monkeypatch, name):
    orchestrator = _guarded_orchestrator(monkeypatch)
    refused = _run_tool(orchestrator, "active-song", name)
    assert refused.action_required_payload == {
        "status": "action_required", "reason": "sing_all_already_running"}
    with pytest.raises(_ReachedTool):
        _run_tool(orchestrator, "other-song", name)


def _route(path, method):
    return next(route.endpoint for route in main.app.routes
                if getattr(route, "path", None) == path and method in route.methods)


def test_solfege_settings_refuse_active_song_and_apply_in_other_song(monkeypatch):
    async def owner(_request):
        return "made-up-owner"

    async def owned(*_args):
        return {}

    async def update(*_args, **_kwargs):
        return {"settings": {"system": "fixed_do", "mode": "major"}}

    monkeypatch.setattr(main, "_get_user_id_or_401", owner)
    monkeypatch.setattr(main, "_get_session_or_404", owned)
    monkeypatch.setattr(main, "active_job_for_song",
                        lambda _uid, song: {"status": "running"} if song == "active-song" else None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(session_store="firestore"), sessions=object(),
        orchestrator=SimpleNamespace(update_solfege_settings=update))))
    payload = main.SolfegeSettingsRequest(settings={"system": "fixed_do", "mode": "major"})
    endpoint = _route("/sessions/{session_id}/solfege-settings", "PATCH")
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint("active-song", request, payload))
    assert (caught.value.status_code, caught.value.detail) == (
        409, {"code": "sing_all_already_running"})
    assert asyncio.run(endpoint("other-song", request, payload))["settings"]["system"] == "fixed_do"


def test_song_delete_refuses_active_song_and_deletes_other_song(monkeypatch):
    deleted = []

    async def owner(_request):
        return "made-up-owner"

    async def row(*_args):
        return {"id": "made-up-song"}

    async def delete(song, _uid):
        deleted.append(song)

    monkeypatch.setattr(main, "_get_user_id_or_401", owner)
    monkeypatch.setattr(main, "active_job_for_song",
                        lambda _uid, song: {"status": "running"} if song == "active-song" else None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(session_store="firestore"),
        sessions=SimpleNamespace(get_song_row=row, delete_song=delete),
        job_store=SimpleNamespace(list_active_jobs=lambda **_kwargs: []))))
    endpoint = _route("/songs/{session_id}", "DELETE")
    refused = asyncio.run(endpoint("active-song", request))
    assert refused.status_code == 409 and b'"work_running"' in refused.body
    assert asyncio.run(endpoint("other-song", request)) == {"deleted": True}
    assert deleted == ["other-song"]


def test_chat_message_and_export_mix_reach_handlers_during_sing_all(monkeypatch):
    async def context(_request):
        return "made-up-owner", ""

    async def allowed(*_args):
        return {}

    async def no_credits(*_args):
        return None

    async def chat(*_args, **_kwargs):
        return {"message": "Made-up reply"}

    async def reached_billing(**_kwargs):
        raise _ReachedTool

    monkeypatch.setattr(main, "_get_user_context_or_401", context)
    monkeypatch.setattr(main, "_get_session_or_404", allowed)
    monkeypatch.setattr(main, "_require_active_credits", no_credits)
    monkeypatch.setattr(main, "_build_export_mix_billing_context", reached_billing)
    monkeypatch.setattr(main, "active_job_for_song", lambda *_args: {"status": "running"})
    settings = SimpleNamespace(llm_max_message_chars=1000, export_mix_enabled=True)
    sessions = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            settings=settings, sessions=sessions, job_store=object(),
            orchestrator=SimpleNamespace(handle_chat=chat))),
        headers={},
    )
    chat_endpoint = _route("/sessions/{session_id}/chat", "POST")
    result = asyncio.run(chat_endpoint(
        "active-song", request, main.ChatRequest(message="Sing All is running")))
    assert result["message"] == "Made-up reply"

    export_endpoint = _route("/sessions/{session_id}/export-mix", "POST")
    track = lambda name: SimpleNamespace(
        job_id=name, part_id=name, solo=False, muted=False, volume=1.0)
    payload = SimpleNamespace(tracks=[track("P1"), track("P2")], billing_reference_job_id=None)
    with pytest.raises(_ReachedTool):
        asyncio.run(export_endpoint("active-song", request, payload))
