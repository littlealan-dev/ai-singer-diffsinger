"""The chat may open Sing All only with a supported, resolved language."""

import asyncio
import json
import logging

import pytest

from src.backend import orchestrator as orchestrator_module
from src.backend.llm_prompt import ToolCall
from src.backend.sing_all import open_sing_all_result, supported_singing_languages
from src.mcp.tools import list_tools


_VOICES = [
    {"id": "voice_en", "languages": []},
    {"id": "voice_ja", "languages": ["ja"]},
]


def test_open_sing_all_tool_schema() -> None:
    tool = list_tools({"open_sing_all"})[0]
    assert tool["name"] == "open_sing_all"
    assert tool["inputSchema"]["required"] == ["language"]
    assert "solfegeOnly" in tool["inputSchema"]["properties"]


def test_supported_singing_languages_uses_legacy_english_default() -> None:
    assert supported_singing_languages(_VOICES) == ["en", "ja"]


@pytest.mark.parametrize(
    ("arguments", "active", "reason"),
    [
        ({"language": "en"}, "running", "sing_all_already_running"),
        ({}, None, "language_required"),
        ({"language": "not a code"}, None, "language_required"),
        ({"language": "fr"}, None, "no_voicebank_for_language"),
        ({"language": "ja", "solfegeOnly": True}, None, "solfege_only_needs_en"),
    ],
)
def test_open_sing_all_action_required(
    arguments: dict, active: str | None, reason: str,
) -> None:
    result = open_sing_all_result(arguments, voicebanks=_VOICES, active_job_id=active)
    assert result["status"] == "action_required"
    assert result["reason"] == reason
    assert "singAll" not in result
    if reason == "no_voicebank_for_language":
        assert result["language"] == "fr"
        assert result["supported"] == ["en", "ja"]


@pytest.mark.parametrize("language,solfege_only", [("EN", False), ("ja", False), ("en", True)])
def test_supported_language_opens_card(language: str, solfege_only: bool) -> None:
    assert open_sing_all_result(
        {"language": language, "solfegeOnly": solfege_only}, voicebanks=_VOICES
    ) == {
        "status": "ready",
        "singAll": {"language": language.lower(), "solfegeOnly": solfege_only},
    }


def _execute_open(monkeypatch, arguments: dict, *, active: str | None = None):
    class _User:
        def collection(self, name):
            assert name == "users"
            return self
        def document(self, name):
            assert name == "made-up-owner"
            return self
        def get(self):
            return self
        def to_dict(self):
            return {"activeSingAllJobId": active} if active else {}

    monkeypatch.setattr(orchestrator_module, "get_firestore_client", lambda: _User())
    orchestrator = object.__new__(orchestrator_module.Orchestrator)
    orchestrator._logger = logging.getLogger(__name__)
    async def no_stop():
        return None
    async def voicebanks():
        return _VOICES
    orchestrator._check_chat_turn_stopped = no_stop
    orchestrator._get_voicebank_details = voicebanks
    return asyncio.run(orchestrator._execute_tool_calls(
        "made-up-song", {"parts": [{"part_id": "P1"}]},
        [ToolCall(name="open_sing_all", arguments=arguments)],
        user_id="made-up-owner", score_summary={"parts": [{"part_id": "P1"}]},
        user_email="", explicit_verse_number=None,
    ))


@pytest.mark.parametrize("arguments,active,reason", [
    ({"language": "en"}, "running", "sing_all_already_running"),
    ({}, None, "language_required"),
    ({"language": "not a code"}, None, "language_required"),
    ({"language": "fr"}, None, "no_voicebank_for_language"),
    ({"language": "ja", "solfegeOnly": True}, None, "solfege_only_needs_en"),
])
def test_open_handler_returns_reason_to_chat_followup(
    monkeypatch, arguments, active, reason,
) -> None:
    result = _execute_open(monkeypatch, arguments, active=active)
    assert result.audio_response == {"type": "chat_text", "message": ""}
    assert result.action_required_payload["reason"] == reason
    assert json.loads(result.followup_prompt) == result.action_required_payload
    assert result.followup_message_only is False
    assert "singAll" not in result.action_required_payload
    if reason == "no_voicebank_for_language":
        assert result.action_required_payload["supported"] == ["en", "ja"]


@pytest.mark.parametrize("arguments,expected", [
    ({"language": "EN"}, {"language": "en", "solfegeOnly": False}),
    ({"language": "en", "solfegeOnly": True},
     {"language": "en", "solfegeOnly": True}),
])
def test_open_handler_puts_supported_language_on_reply(monkeypatch, arguments, expected) -> None:
    result = _execute_open(monkeypatch, arguments)
    assert result.audio_response["singAll"] == expected
    assert result.followup_prompt is None
    assert result.action_required_payload is None
