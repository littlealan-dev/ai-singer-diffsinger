from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List
import logging

from src.mcp.handlers import HANDLERS
from src.mcp.logging_utils import summarize_payload


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]


TOOLS: List[Tool] = [
    Tool(
        name="parse_score",
        description="Parse MusicXML into a score JSON dict.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "part_id": {"type": ["string", "null"]},
                "part_index": {"type": ["integer", "null"]},
                "expand_repeats": {"type": "boolean"},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"]},
                "tempos": {"type": "array"},
                "parts": {"type": "array"},
                "structure": {"type": "object"},
            },
            "required": ["title", "tempos", "parts", "structure"],
            "additionalProperties": True,
        },
    ),
    Tool(
        name="modify_score",
        description="Execute sandboxed Python to modify a score.",
        input_schema={
            "type": "object",
            "properties": {
                "score": {"type": "object"},
                "code": {"type": "string"},
            },
            "required": ["score", "code"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "additionalProperties": True,
        },
    ),
    Tool(
        name="save_audio",
        description="Save audio to a file and return base64 bytes.",
        input_schema={
            "type": "object",
            "properties": {
                "waveform": {"type": "array", "items": {"type": "number"}},
                "output_path": {"type": "string"},
                "sample_rate": {"type": "integer"},
                "format": {"type": "string"},
            },
            "required": ["waveform", "output_path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "audio_base64": {"type": "string"},
                "duration_seconds": {"type": "number"},
                "sample_rate": {"type": "integer"},
            },
            "required": ["audio_base64", "duration_seconds", "sample_rate"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="synthesize",
        description="Run the full pipeline (internal steps 2-6) in one call.",
        input_schema={
            "type": "object",
            "properties": {
                "score": {"type": "object"},
                "voicebank": {"type": "string"},
                "part_index": {"type": "integer"},
                "voice_id": {"type": ["string", "null"]},
                "articulation": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                "airiness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "intensity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "clarity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["score", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "waveform": {"type": "array", "items": {"type": "number"}},
                "sample_rate": {"type": "integer"},
                "duration_seconds": {"type": "number"},
            },
            "required": ["waveform", "sample_rate", "duration_seconds"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_voicebanks",
        description="List available voicebanks.",
        input_schema={
            "type": "object",
            "properties": {
                "search_path": {"type": "string"},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["id", "name", "path"],
                "additionalProperties": False,
            },
        },
    ),
    Tool(
        name="get_voicebank_info",
        description="Get metadata and capabilities of a voicebank.",
        input_schema={
            "type": "object",
            "properties": {
                "voicebank": {"type": "string"},
            },
            "required": ["voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "languages": {"type": "array", "items": {"type": "string"}},
                "has_duration_model": {"type": "boolean"},
                "has_pitch_model": {"type": "boolean"},
                "has_variance_model": {"type": "boolean"},
                "speakers": {"type": "array"},
                "sample_rate": {"type": "integer"},
                "hop_size": {"type": "integer"},
                "use_lang_id": {"type": "boolean"},
            },
            "required": [
                "name",
                "languages",
                "has_duration_model",
                "has_pitch_model",
                "has_variance_model",
                "speakers",
                "sample_rate",
                "hop_size",
                "use_lang_id",
            ],
            "additionalProperties": False,
        },
    ),
]


def list_tools(allowlist: set[str] | None = None) -> List[Dict[str, Any]]:
    tools = TOOLS
    if allowlist is not None:
        tools = [tool for tool in TOOLS if tool.name in allowlist]
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "outputSchema": tool.output_schema,
        }
        for tool in tools
    ]


def call_tool(name: str, arguments: Dict[str, Any], device: str) -> Any:
    if name not in HANDLERS:
        raise ValueError(f"Unknown tool: {name}")
    handler = HANDLERS[name]
    logger = logging.getLogger(__name__)
    logger.debug("Dispatch tool=%s args=%s", name, summarize_payload(arguments))
    result = handler(arguments, device)
    logger.debug("Return tool=%s result=%s", name, summarize_payload(result))
    return result
