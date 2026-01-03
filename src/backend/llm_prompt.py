from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import json


LLM_SCHEMA_VERSION = "v1"
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "config" / "system_prompt.txt"


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class LlmResponse:
    tool_calls: List[ToolCall]
    final_message: str
    include_score: bool


def build_system_prompt(
    tools: List[Dict[str, Any]],
    score_available: bool,
    voicebank_ids: Optional[List[str]] = None,
    score_summary: Optional[Dict[str, Any]] = None,
) -> str:
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
    template = _load_system_prompt()
    return (
        template.replace("{score_hint}", score_hint)
        .replace("{tool_json}", tool_json)
        .replace("{voicebanks}", voicebanks_text)
        .replace("{score_summary}", score_summary_text)
    )


def _load_system_prompt() -> str:
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Missing system prompt template: {_SYSTEM_PROMPT_PATH}")
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def parse_llm_response(text: str) -> Optional[LlmResponse]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
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
    return LlmResponse(
        tool_calls=tool_calls,
        final_message=final_message,
        include_score=include_score,
    )
