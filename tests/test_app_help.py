"""The `app_help` tool: how-to text for the app's on-screen controls (docs/specs/app-help-tool-spec.md)."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from src.backend import app_help, orchestrator as orchestrator_module, synthesis_pricing
from src.backend.llm_prompt import ToolCall, build_prompt_bundle
from src.backend.orchestrator import DEFAULT_LLM_TOOL_ALLOWLIST, TOOL_RESULT_PREFIX
from src.mcp.tools import list_tools
from tests.test_backend_api import _create_session, _upload_score, client  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parents[1]
TOPICS = [
    "upload_score", "tracks_and_mixing", "download_mix", "score_view", "sing_a_part", "sing_all",
    "sing_all_download", "share", "song_info", "my_songs", "credits_and_plans", "credit_rates",
]


def _ui_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted((ROOT / "ui" / "src").rglob("*.ts*")))


def test_every_topic_has_a_section_and_the_tool_offers_exactly_those():
    assert app_help.APP_HELP_TOPICS == TOPICS
    assert all(0 < len(app_help.APP_HELP[topic]) <= app_help.APP_HELP_SECTION_LIMIT for topic in TOPICS)
    tool = list_tools({"app_help"})[0]
    assert tool["inputSchema"]["properties"]["topic"]["enum"] == TOPICS
    assert tool["inputSchema"]["required"] == ["topic"]


def test_the_help_names_only_controls_that_exist_in_the_ui():
    """Every quoted name in the help file is an on-screen label."""
    ui = _ui_source()
    labels = set(re.findall(r'"([^"\n]+)"', (ROOT / "src/backend/config/app_help.md").read_text(encoding="utf-8")))
    assert {"Download zip", "Customise", "Turn off sharing", "Song info", "Upgrade", "With Repeats"} <= labels
    assert sorted(label for label in labels if label not in ui) == []
    # Facts the help states, checked where the UI sets them.
    assert 'accept=".musicxml,.xml,.mxl"' in ui
    assert "BROWSER_MIX_BOUNCE_MAX_DURATION_SECONDS = 5 * 60" in ui
    assert 'BROWSER_MIX_RECORDING_MIME_TYPE = "audio/wav"' in ui


def test_credit_rates_come_from_the_pricing_code(monkeypatch):
    rates = app_help.APP_HELP["credit_rates"]
    assert "1 per 30 seconds" in rates and "1 per 2 minutes" in rates and "1 per file" in rates
    assert "{" not in rates
    monkeypatch.setattr(synthesis_pricing, "VOCAL_CREDIT_DURATION_SECONDS", 45)
    monkeypatch.setattr(synthesis_pricing, "INSTRUMENTAL_CREDIT_DURATION_SECONDS", 90)
    reloaded = app_help.load_app_help()["credit_rates"]
    assert "1 per 45 seconds" in reloaded and "1 per 1.5 minutes" in reloaded


@pytest.mark.parametrize("text,problem", [
    ("## made_up\nCosts {made_up_rate}.\n", "Unknown help placeholder"),
    ("A preamble.\n## made_up\nText.\n", "before the first topic"),
    ("## made_up\nOne.\n## made_up\nTwo.\n", "repeated"),
    ("## Made Up\nText.\n", "Bad"),
    ("## made_up\n\n## other\nText.\n", "empty"),
    ("## made_up\n" + "x" * 1501 + "\n", "longer than"),
])
def test_a_malformed_help_file_fails_at_load(tmp_path, text, problem):
    path = tmp_path / "help.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=problem):
        app_help.load_app_help(path)


def _execute(arguments: dict, *, score_summary=None):
    orchestrator = object.__new__(orchestrator_module.Orchestrator)
    orchestrator._logger = logging.getLogger(__name__)

    async def no_stop():
        return None

    orchestrator._check_chat_turn_stopped = no_stop
    return asyncio.run(orchestrator._execute_tool_calls(
        "made-up-song", {}, [ToolCall(name="app_help", arguments=arguments)],
        user_id="made-up-owner", score_summary=score_summary, user_email="", explicit_verse_number=None,
    ))


def test_the_tool_answers_with_the_topic_as_a_message_only_followup():
    # No score, and no Sing All check: it reads nothing and changes nothing.
    result = _execute({"topic": "sing_all_download"})
    assert result.followup_message_only is True
    assert json.loads(result.followup_prompt) == {
        "topic": "sing_all_download", "help": app_help.APP_HELP["sing_all_download"],
    }
    assert result.audio_response == {"type": "chat_text", "message": ""}
    assert result.action_required_payload is None


def test_an_unknown_topic_is_refused_to_the_model_with_the_topics():
    result = _execute({"topic": "made_up"})
    assert result.followup_message_only is True
    assert json.loads(result.followup_prompt) == {"status": "unknown_topic", "topic": "made_up", "topics": TOPICS}


def test_the_prompt_sends_how_to_questions_to_the_tool_and_answers_a_bare_help():
    bundle = build_prompt_bundle(list_tools(DEFAULT_LLM_TOOL_ALLOWLIST), score_available=True)
    static = bundle.static_prompt_text
    assert '"name": "app_help"' in static and "app_help" not in bundle.dynamic_prompt_text
    assert "call `app_help` with the topic that fits" in static
    assert 'only asks for help with nothing specific ("Help"' in static
    assert "offer to do it. Do it only after the user asks you to." in static
    assert "If the score is missing, say that singing starts with uploading a MusicXML score." in static
    # The details moved to the help file.
    for moved in ('"Download mix (real-time)" in the score player', "Tracks & Stems panel", "Upload MusicXML…"):
        assert moved not in static


def test_a_how_to_question_in_the_chat_gets_the_help_and_a_message_only_reply(client):
    test_client, app = client
    session_id = _create_session(test_client)
    _upload_score(test_client, session_id)
    followups: list[dict] = []

    class HelpClient:
        def generate(self, system_prompt, history, **_):
            last = history[-1].get("content", "") if history else ""
            if isinstance(last, str) and last.startswith(TOOL_RESULT_PREFIX):
                followups.append(json.loads(last.split("Message-only payload:\n", 1)[1]))
                return json.dumps({"tool_calls": [], "final_message": "Use Download zip on the card.",
                                   "include_score": False})
            return json.dumps({"tool_calls": [{"name": "app_help", "arguments": {"topic": "sing_all_download"}}],
                               "final_message": "", "include_score": False})

    app.state.llm_client = app.state.orchestrator._llm_client = HelpClient()
    response = test_client.post(f"/sessions/{session_id}/chat", json={"message": "where is my zip?"})
    assert response.status_code == 200
    assert response.json()["message"] == "Use Download zip on the card."
    assert followups == [{"topic": "sing_all_download", "help": app_help.APP_HELP["sing_all_download"]}]


class _ScriptedClient:
    """Answers the first call with `first`, and each follow-up with prose; records what it was sent."""

    def __init__(self, first: dict):
        self.first = first
        self.followups: list[dict] = []
        self.prompts: list[str] = []

    def generate(self, system_prompt, history, **_):
        self.prompts.append(system_prompt)
        last = history[-1].get("content", "") if history else ""
        if isinstance(last, str) and last.startswith(TOOL_RESULT_PREFIX):
            self.followups.append(json.loads(last.split("Message-only payload:\n", 1)[1]))
            return json.dumps({"tool_calls": [], "final_message": "Made-up follow-up.", "include_score": False})
        return json.dumps({"include_score": False, **self.first})


def _chat_before_upload(client, first: dict, message: str):
    test_client, app = client
    session_id = _create_session(test_client)
    scripted = _ScriptedClient(first)
    app.state.llm_client = app.state.orchestrator._llm_client = scripted
    response = test_client.post(f"/sessions/{session_id}/chat", json={"message": message})
    assert response.status_code == 200
    return response.json(), scripted


def test_help_before_any_upload_is_answered_by_the_model(client):
    body, scripted = _chat_before_upload(
        client, {"tool_calls": [], "final_message": "Upload a score first; then I can sing it."}, "Help")
    assert body == {"type": "chat_text", "message": "Upload a score first; then I can sing it."}
    assert "Score status: missing." in scripted.prompts[0]
    assert scripted.followups == []


def test_a_how_to_question_before_any_upload_gets_the_help(client):
    body, scripted = _chat_before_upload(
        client, {"tool_calls": [{"name": "app_help", "arguments": {"topic": "sing_a_part"}}],
                 "final_message": ""}, "how do I change the voice?")
    assert body["message"] == "Made-up follow-up."
    assert scripted.followups == [{"topic": "sing_a_part", "help": app_help.APP_HELP["sing_a_part"]}]
    assert "Score status: missing." in scripted.prompts[1]


def test_a_tool_that_needs_a_score_is_refused_to_the_model_before_any_upload(client):
    body, scripted = _chat_before_upload(
        client, {"tool_calls": [{"name": "synthesize", "arguments": {"part_id": "P1"}}],
                 "final_message": "Singing now."}, "sing the soprano part")
    # The model words the reply; the refused call's own text is never shown.
    assert body["message"] == "Made-up follow-up."
    assert scripted.followups == [{"status": "score_required", "refused_tools": ["synthesize"]}]
