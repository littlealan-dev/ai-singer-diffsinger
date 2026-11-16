"""Sing All's task text must keep its referenced rules in sync."""

from pathlib import Path

from src.backend.sing_all_prompts import quote_prompt, style_prompt


_CONFIG = Path(__file__).resolve().parents[1] / "src" / "backend" / "config"


def test_quote_prompt_uses_current_rule_references_without_score_or_tools() -> None:
    task = (_CONFIG / "system_prompt_sing_all_quote.txt").read_text(encoding="utf-8")
    chat = (_CONFIG / "system_prompt.txt").read_text(encoding="utf-8")
    planner = (_CONFIG / "system_prompt_planner_v2.txt").read_text(encoding="utf-8")
    for heading in (
        "Which parts a request covers",
        "Instrument playback presets for MIDI playback",
        "Voicebank selection",
    ):
        assert heading in task and heading in chat
    assert "lane_layout" in task and "lane_layout" in planner
    prompt = quote_prompt(synthesis_max_duration_seconds=300)
    assert prompt.startswith(task)
    assert prompt.index("The chat assistant's instructions") < prompt.index("The part-splitting planner's instructions")
    assert planner in prompt
    assert "{tool_json}" not in prompt
    assert "{score_summary}" not in prompt
    assert "`300` seconds" in prompt


def test_style_prompt_uses_current_chat_style_rules() -> None:
    task = (_CONFIG / "system_prompt_sing_all_style.txt").read_text(encoding="utf-8")
    chat = (_CONFIG / "system_prompt.txt").read_text(encoding="utf-8")
    for name in ("voice_color", "airiness", "clarity", "gender"):
        assert name in task and name in chat
    prompt = style_prompt(synthesis_max_duration_seconds=300)
    assert prompt.startswith(task)
    assert "The chat assistant's instructions" in prompt
    assert "The part-splitting planner's instructions" not in prompt
