"""The chat may open Sing All only with a supported, resolved language."""

import pytest

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
