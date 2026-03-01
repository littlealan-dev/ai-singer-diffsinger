from __future__ import annotations

"""Helpers for building and parsing LLM prompts/responses."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from src.api.voice_part_lint_rules import render_lint_rules_for_prompt


LLM_SCHEMA_VERSION = "v1"
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "config" / "system_prompt.txt"
_SYSTEM_PROMPT_LESSONS_PATH = (
    Path(__file__).resolve().parent / "config" / "system_prompt_lessons.txt"
)


@dataclass(frozen=True)
class ToolCall:
    """LLM-specified tool invocation."""
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class LlmResponse:
    """Structured LLM response with tool calls and final text."""
    tool_calls: List[ToolCall]
    final_message: str
    include_score: bool
    thought_summary: str = ""


def build_system_prompt(
    tools: List[Dict[str, Any]],
    score_available: bool,
    voicebank_ids: Optional[List[str]] = None,
    score_summary: Optional[Dict[str, Any]] = None,
    voice_part_signals: Optional[Dict[str, Any]] = None,
    preprocess_mapping_context: Optional[Dict[str, Any]] = None,
    last_successful_preprocess_plan: Optional[Dict[str, Any]] = None,
    voicebank_details: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the system prompt with tool specs and context metadata."""
    tool_specs = []
    for tool in tools:
        tool_specs.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "input_schema": tool.get("inputSchema"),
            }
        )

    score_hint = "available" if score_available else "missing"
    tool_json = json.dumps(tool_specs, indent=2, sort_keys=True)
    voicebanks_text = "none"
    if voicebank_ids:
        voicebanks_text = ", ".join(voicebank_ids)
    score_summary_text = "none"
    if score_summary:
        score_summary_text = json.dumps(score_summary, indent=2, sort_keys=True)
    voice_part_signals_text = "none"
    if voice_part_signals:
        voice_part_signals_text = json.dumps(voice_part_signals, indent=2, sort_keys=True)
    preprocess_mapping_context_text = "none"
    if preprocess_mapping_context:
        preprocess_mapping_context_text = json.dumps(
            preprocess_mapping_context, indent=2, sort_keys=True
        )
    last_successful_preprocess_plan_text = "none"
    if last_successful_preprocess_plan:
        last_successful_preprocess_plan_text = json.dumps(
            last_successful_preprocess_plan, indent=2, sort_keys=True
        )
    voicebank_details_text = "none"
    if voicebank_details:
        voicebank_details_text = json.dumps(voicebank_details, indent=2, sort_keys=True)
    template = _load_system_prompt()
    return (
        template.replace("{score_hint}", score_hint)
        .replace("{tool_json}", tool_json)
        .replace("{voicebanks}", voicebanks_text)
        .replace("{score_summary}", score_summary_text)
        .replace("{voice_part_signals}", voice_part_signals_text)
        .replace("{preprocess_mapping_context}", preprocess_mapping_context_text)
        .replace("{last_successful_preprocess_plan}", last_successful_preprocess_plan_text)
        .replace("{voicebank_details}", voicebank_details_text)
    )


def _load_system_prompt() -> str:
    """Load and combine system prompt templates from disk."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Missing system prompt template: {_SYSTEM_PROMPT_PATH}")
    base_prompt = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    prompt_sections = [base_prompt]
    if _SYSTEM_PROMPT_LESSONS_PATH.exists():
        lessons_prompt = _SYSTEM_PROMPT_LESSONS_PATH.read_text(encoding="utf-8")
        prompt_sections.append(lessons_prompt)
    prompt_sections.append(render_lint_rules_for_prompt())
    return "\n\n---\n\n".join(prompt_sections)


def parse_llm_response(text: str) -> Optional[LlmResponse]:
    """Parse an LLM response JSON snippet into a structured response."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip optional fenced code blocks.
        cleaned = cleaned.strip("`")
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("json"):
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = cleaned[start : end + 1]
    try:
        payload = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    raw_calls = payload.get("tool_calls", [])
    tool_calls: List[ToolCall] = []
    if isinstance(raw_calls, list):
        for entry in raw_calls:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            arguments = entry.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(ToolCall(name=name, arguments=arguments))
    raw_message = payload.get("final_message")
    final_message = str(raw_message).strip() if raw_message is not None else ""
    include_score = bool(payload.get("include_score", False))
    raw_thought_summary = payload.get("thought_summary")
    thought_summary = (
        str(raw_thought_summary).strip() if raw_thought_summary is not None else ""
    )
    return LlmResponse(
        tool_calls=tool_calls,
        final_message=final_message,
        include_score=include_score,
        thought_summary=thought_summary,
    )
