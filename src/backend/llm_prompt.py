from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import json


LLM_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class LlmResponse:
    tool_calls: List[ToolCall]
    final_message: str
    include_score: bool


def build_system_prompt(tools: List[Dict[str, Any]], score_available: bool) -> str:
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
    return (
        "You are the SVS orchestration LLM. Decide which MCP tools to call, "
        "then provide a final user message.\n"
        "Capabilities:\n"
        "- Call ONLY the MCP tools listed below.\n"
        "- Ask to upload MusicXML if the score is missing.\n"
        "Non-capabilities:\n"
        "- No filesystem, network, or external tools access.\n"
        "- Do not claim you rendered audio unless you called synthesize.\n"
        "- Do not invent tool names or tool outputs.\n"
        "\n"
        f"Score status: {score_hint}.\n"
        "\n"
        "Tool list (name, description, input schema):\n"
        f"{tool_json}\n"
        "\n"
        "Response format (JSON only, no markdown):\n"
        "{\n"
        '  "tool_calls": [\n'
        '    {"name": "tool_name", "arguments": {"arg": "value"}}\n'
        "  ],\n"
        '  "final_message": "text response to the user",\n'
        '  "include_score": false\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- tool_calls may be empty if no tool use is needed.\n"
        "- include_score is true only if the user requests the score JSON.\n"
        "- For modify_score, provide code; the backend injects the current score.\n"
        "- For synthesize, omit score and voicebank to use defaults.\n"
    )


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
