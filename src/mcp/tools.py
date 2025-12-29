from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from src.mcp.handlers import HANDLERS


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
        name="phonemize",
        description="Convert lyrics to phoneme sequences.",
        input_schema={
            "type": "object",
            "properties": {
                "lyrics": {"type": "array", "items": {"type": "string"}},
                "voicebank": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["lyrics", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "phonemes": {"type": "array", "items": {"type": "string"}},
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "language_ids": {"type": "array", "items": {"type": "integer"}},
                "word_boundaries": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["phonemes", "phoneme_ids", "language_ids", "word_boundaries"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="align_phonemes_to_notes",
        description="Prepare phonemes and timing for inference.",
        input_schema={
            "type": "object",
            "properties": {
                "score": {"type": "object"},
                "voicebank": {"type": "string"},
                "part_index": {"type": "integer"},
                "voice_id": {"type": ["string", "null"]},
                "include_phonemes": {"type": "boolean"},
            },
            "required": ["score", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "phonemes": {"type": "array", "items": {"type": "string"}},
                "language_ids": {"type": "array", "items": {"type": "integer"}},
                "word_boundaries": {"type": "array", "items": {"type": "integer"}},
                "word_durations": {"type": "array", "items": {"type": "number"}},
                "word_pitches": {"type": "array", "items": {"type": "number"}},
                "note_durations": {"type": "array", "items": {"type": "integer"}},
                "note_pitches": {"type": "array", "items": {"type": "number"}},
                "note_rests": {"type": "array", "items": {"type": "boolean"}},
            },
            "required": [
                "phoneme_ids",
                "language_ids",
                "word_boundaries",
                "word_durations",
                "word_pitches",
                "note_durations",
                "note_pitches",
                "note_rests",
            ],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="predict_durations",
        description="Predict timing for each phoneme.",
        input_schema={
            "type": "object",
            "properties": {
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "word_boundaries": {"type": "array", "items": {"type": "integer"}},
                "word_durations": {"type": "array", "items": {"type": "number"}},
                "word_pitches": {"type": "array", "items": {"type": "number"}},
                "voicebank": {"type": "string"},
                "language_ids": {"type": ["array", "null"], "items": {"type": "integer"}},
            },
            "required": ["phoneme_ids", "word_boundaries", "word_durations", "word_pitches", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "durations": {"type": "array", "items": {"type": "integer"}},
                "total_frames": {"type": "integer"},
                "encoder_out": {},
                "x_masks": {},
            },
            "required": ["durations", "total_frames", "encoder_out", "x_masks"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="predict_pitch",
        description="Generate natural pitch curves.",
        input_schema={
            "type": "object",
            "properties": {
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "durations": {"type": "array", "items": {"type": "integer"}},
                "note_pitches": {"type": "array", "items": {"type": "number"}},
                "note_durations": {"type": "array", "items": {"type": "integer"}},
                "note_rests": {"type": "array", "items": {"type": "boolean"}},
                "voicebank": {"type": "string"},
                "language_ids": {"type": ["array", "null"], "items": {"type": "integer"}},
                "encoder_out": {},
            },
            "required": ["phoneme_ids", "durations", "note_pitches", "note_durations", "note_rests", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "f0": {"type": "array", "items": {"type": "number"}},
                "pitch_midi": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["f0", "pitch_midi"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="predict_variance",
        description="Generate expressiveness parameters.",
        input_schema={
            "type": "object",
            "properties": {
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "durations": {"type": "array", "items": {"type": "integer"}},
                "f0": {"type": "array", "items": {"type": "number"}},
                "voicebank": {"type": "string"},
                "language_ids": {"type": ["array", "null"], "items": {"type": "integer"}},
                "encoder_out": {},
            },
            "required": ["phoneme_ids", "durations", "f0", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "breathiness": {"type": "array", "items": {"type": "number"}},
                "tension": {"type": "array", "items": {"type": "number"}},
                "voicing": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["breathiness", "tension", "voicing"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="synthesize_audio",
        description="Generate audio by running mel synthesis and vocoding.",
        input_schema={
            "type": "object",
            "properties": {
                "phoneme_ids": {"type": "array", "items": {"type": "integer"}},
                "durations": {"type": "array", "items": {"type": "integer"}},
                "f0": {"type": "array", "items": {"type": "number"}},
                "breathiness": {"type": ["array", "null"], "items": {"type": "number"}},
                "tension": {"type": ["array", "null"], "items": {"type": "number"}},
                "voicing": {"type": ["array", "null"], "items": {"type": "number"}},
                "voicebank": {"type": "string"},
                "language_ids": {"type": ["array", "null"], "items": {"type": "integer"}},
                "vocoder_path": {"type": ["string", "null"]},
            },
            "required": ["phoneme_ids", "durations", "f0", "voicebank"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "waveform": {"type": "array", "items": {"type": "number"}},
                "sample_rate": {"type": "integer"},
                "hop_size": {"type": "integer"},
            },
            "required": ["waveform", "sample_rate", "hop_size"],
            "additionalProperties": False,
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
        description="Run the full pipeline (steps 2-6) in one call.",
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


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "outputSchema": tool.output_schema,
        }
        for tool in TOOLS
    ]


def call_tool(name: str, arguments: Dict[str, Any], device: str) -> Any:
    if name not in HANDLERS:
        raise ValueError(f"Unknown tool: {name}")
    handler = HANDLERS[name]
    return handler(arguments, device)
