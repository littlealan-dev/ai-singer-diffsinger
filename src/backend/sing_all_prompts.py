"""Assemble Sing All's task prompts with the current chat and planner rules."""

from pathlib import Path

from src.backend.llm_prompt import _load_system_prompt


_CONFIG = Path(__file__).with_name("config")
_CHAT_CONTEXT_MARKERS = {
    "score_hint",
    "score_summary",
    "parsed_score_json",
    "voice_part_signals",
    "preprocess_mapping_context",
    "last_preprocess_plan",
    "voicebanks",
    "voicebank_details",
    "tool_json",
}


def _chat_reference(*, synthesis_max_duration_seconds: float) -> str:
    """Use the same fixed chat text, without a score or a tool list."""
    prompt = _load_system_prompt()
    for marker in _CHAT_CONTEXT_MARKERS:
        prompt = prompt.replace("{" + marker + "}", "<not supplied to Sing All>")
    return prompt.replace(
        "{synthesis_max_duration_seconds}", str(synthesis_max_duration_seconds)
    )


def quote_prompt(*, synthesis_max_duration_seconds: float) -> str:
    """The quote task, then the chat and planner reference instructions."""
    return "\n\n".join(
        (
            (_CONFIG / "system_prompt_sing_all_quote.txt").read_text(encoding="utf-8"),
            "The chat assistant's instructions",
            _chat_reference(synthesis_max_duration_seconds=synthesis_max_duration_seconds),
            "The part-splitting planner's instructions",
            (_CONFIG / "system_prompt_planner_v2.txt").read_text(encoding="utf-8"),
        )
    )


def style_prompt(*, synthesis_max_duration_seconds: float) -> str:
    """The style task and the current chat reference instructions."""
    return "\n\n".join(
        (
            (_CONFIG / "system_prompt_sing_all_style.txt").read_text(encoding="utf-8"),
            "The chat assistant's instructions",
            _chat_reference(synthesis_max_duration_seconds=synthesis_max_duration_seconds),
        )
    )
