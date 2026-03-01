from __future__ import annotations

"""Tool definitions and dispatch for the MCP server."""

from dataclasses import dataclass
from typing import Any, Dict, List
import logging

from src.mcp.handlers import HANDLERS
from src.mcp.logging_utils import summarize_payload


@dataclass(frozen=True)
class Tool:
    """Tool metadata describing schemas and documentation."""
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]


_VOICE_SV_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Staff/voice identifier inside a measure.",
    "properties": {
        "staff": {"type": "string", "description": "MusicXML staff number."},
        "voice": {"type": "string", "description": "MusicXML voice number."},
    },
    "required": ["staff", "voice"],
    "additionalProperties": False,
}

_MEASURE_RANGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Inclusive measure range.",
    "properties": {
        "start": {"type": "integer", "description": "Start measure number."},
        "end": {"type": "integer", "description": "End measure number."},
    },
    "required": ["start", "end"],
    "additionalProperties": False,
}

_VOICE_PART_REGION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Resolution status for a measure span in one target voice part.",
    "properties": {
        "status": {"type": "string", "description": "Region status (e.g., RESOLVED, NO_MUSIC)."},
        "start_measure": {"type": "integer", "description": "Start measure number."},
        "end_measure": {"type": "integer", "description": "End measure number."},
    },
    "required": ["status", "start_measure", "end_measure"],
    "additionalProperties": False,
}

_SOURCE_CANDIDATE_HINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Suggested source lane for lyric propagation in a local section.",
    "properties": {
        "target_voice_part_id": {"type": "string", "description": "Target derived voice-part id."},
        "source_part_index": {"type": "integer", "description": "Source part index in parsed score."},
        "source_voice_part_id": {"type": "string", "description": "Source voice-part id."},
        "measure_start": {"type": "integer", "description": "Section start measure (inclusive)."},
        "measure_end": {"type": "integer", "description": "Section end measure (inclusive)."},
        "reason": {"type": "string", "description": "Why this source is suggested."},
    },
    "required": [
        "target_voice_part_id",
        "source_part_index",
        "source_voice_part_id",
        "measure_start",
        "measure_end",
        "reason",
    ],
    "additionalProperties": True,
}

_MEASURE_COVERAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Lyric/music coverage diagnostics for one measure.",
    "properties": {
        "measure_number": {"type": "integer", "description": "Measure number."},
        "has_music": {"type": "boolean", "description": "Whether target lane has non-rest notes."},
        "has_word_lyric": {"type": "boolean", "description": "Whether measure has at least one lexical lyric token."},
        "lexical_lyric_count": {"type": "integer", "description": "Count of lexical lyric notes in the measure."},
        "extension_count": {"type": "integer", "description": "Count of extension/slur '+' lyric notes."},
    },
    "required": [
        "measure_number",
        "has_music",
        "has_word_lyric",
        "lexical_lyric_count",
        "extension_count",
    ],
    "additionalProperties": True,
}

_VOICE_PART_SIGNAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Voice-part planning signals for one score part.",
    "properties": {
        "part_index": {"type": "integer", "description": "0-based part index in parsed score."},
        "part_id": {"type": ["string", "null"], "description": "MusicXML part id when available."},
        "part_name": {"type": ["string", "null"], "description": "Part name from MusicXML when available."},
        "multi_voice_part": {"type": "boolean", "description": "True if part contains multiple lanes/voices."},
        "missing_lyric_voice_parts": {
            "type": "array",
            "description": "Voice-part ids in this part that have music but missing lexical lyrics.",
            "items": {"type": "string"},
        },
        "lyric_source_candidates": {
            "type": "array",
            "description": "Global source candidates that can donate lyrics.",
            "items": {
                "type": "object",
                "properties": {
                    "part_index": {"type": "integer"},
                    "part_id": {"type": ["string", "null"]},
                    "part_name": {"type": ["string", "null"]},
                    "voice_part_id": {"type": "string"},
                },
                "required": ["part_index", "voice_part_id"],
                "additionalProperties": True,
            },
        },
        "measure_lyric_coverage": {
            "type": "object",
            "description": "Per-target-voice-part measure lyric coverage.",
            "additionalProperties": {
                "type": "array",
                "items": _MEASURE_COVERAGE_SCHEMA,
            },
        },
        "voice_part_measure_spans": {
            "type": "object",
            "description": "Contiguous measure ranges containing notes for each voice-part id.",
            "additionalProperties": {
                "type": "array",
                "items": _MEASURE_RANGE_SCHEMA,
            },
        },
        "voice_part_id_to_source_voice_id": {
            "type": "object",
            "description": "Mapping from normalized voice-part id to original MusicXML voice id.",
            "additionalProperties": {"type": "string"},
        },
        "source_candidate_hints": {
            "type": "array",
            "description": "Section-level source recommendations.",
            "items": _SOURCE_CANDIDATE_HINT_SCHEMA,
        },
        "part_region_indices": {
            "type": "object",
            "description": "Computed structural regions for split/copy decisions.",
            "properties": {
                "part_span": {
                    "type": "object",
                    "properties": {
                        "start_measure": {"type": "integer"},
                        "end_measure": {"type": "integer"},
                    },
                    "required": ["start_measure", "end_measure"],
                    "additionalProperties": False,
                },
                "chord_regions": {"type": "array", "items": _MEASURE_RANGE_SCHEMA},
                "default_voice_regions": {"type": "array", "items": _MEASURE_RANGE_SCHEMA},
                "target_resolution_by_voice_part": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": _VOICE_PART_REGION_SCHEMA,
                    },
                },
            },
            "required": ["part_span", "chord_regions", "default_voice_regions", "target_resolution_by_voice_part"],
            "additionalProperties": False,
        },
        "verse_number": {"type": ["integer", "null"], "description": "Normalized selected verse number."},
    },
    "required": [
        "part_index",
        "part_id",
        "part_name",
        "multi_voice_part",
        "missing_lyric_voice_parts",
        "lyric_source_candidates",
        "measure_lyric_coverage",
        "voice_part_measure_spans",
        "voice_part_id_to_source_voice_id",
        "source_candidate_hints",
        "part_region_indices",
        "verse_number",
    ],
    "additionalProperties": False,
}

_FULL_SCORE_ANALYSIS_PART_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Full score analysis for one part used by planner/preflight checks.",
    "properties": {
        "part_index": {"type": "integer"},
        "part_id": {"type": ["string", "null"]},
        "part_name": {"type": ["string", "null"]},
        "voice_parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "voice_part_id": {"type": "string"},
                    "source_voice_id": {"type": "string"},
                    "has_notes": {"type": "boolean"},
                    "has_word_lyrics": {"type": "boolean"},
                },
                "required": ["voice_part_id", "source_voice_id"],
                "additionalProperties": True,
            },
        },
        "measure_lyric_coverage": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": _MEASURE_COVERAGE_SCHEMA},
        },
        "voice_part_measure_spans": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": _MEASURE_RANGE_SCHEMA},
        },
        "voice_part_id_to_source_voice_id": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "section_source_candidate_hints": {
            "type": "array",
            "items": _SOURCE_CANDIDATE_HINT_SCHEMA,
        },
        "part_region_indices": {"type": "object", "additionalProperties": True},
    },
    "required": [
        "part_index",
        "part_id",
        "part_name",
        "voice_parts",
        "measure_lyric_coverage",
        "voice_part_measure_spans",
        "voice_part_id_to_source_voice_id",
        "section_source_candidate_hints",
        "part_region_indices",
    ],
    "additionalProperties": False,
}

_PREPROCESS_PLAN_VOICE_REF_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Reference to a specific lane in parsed score analysis.",
    "properties": {
        "part_index": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "0-based index of the source/target part in parsed score.parts. "
                "Use score_summary/voice_part_signals to select this value."
            ),
        },
        "voice_part_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Normalized lane id within part_index (e.g., soprano/alto/voice part 1). "
                "Must match available voice_part_signals for that part."
            ),
        },
    },
    "required": ["part_index", "voice_part_id"],
    "additionalProperties": False,
}

_PREPROCESS_PLAN_SECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Contiguous inclusive measure range with explicit derive/rest behavior. "
        "All sections for a target must be contiguous and cover the full target span."
    ),
    "properties": {
        "start_measure": {
            "type": "integer",
            "minimum": 1,
            "description": "Inclusive start measure number for this section.",
        },
        "end_measure": {
            "type": "integer",
            "minimum": 1,
            "description": "Inclusive end measure number for this section.",
        },
        "mode": {
            "type": "string",
            "enum": ["derive", "rest"],
            "description": (
                "derive=materialize notes/lyrics from sources; "
                "rest=force this range to silence (no melody/lyric sources allowed)."
            ),
        },
        "decision_type": {
            "type": "string",
            "enum": [
                "EXTRACT_FROM_VOICE",
                "SPLIT_CHORDS_SELECT_NOTES",
                "COPY_UNISON_SECTION",
                "INSERT_RESTS",
                "DROP_NOTES_IF_NEEDED",
            ],
            "description": (
                "Planner intent tag for auditability. "
                "Choose the best matching extraction/copy/rest behavior for this section."
            ),
        },
        "method": {
            "type": "string",
            "enum": ["trivial", "ranked"],
            "default": "trivial",
            "description": (
                "Chord note selection strategy for SPLIT_CHORDS_SELECT_NOTES. "
                "ranked = deterministic top-down rank selection using rank_index. "
                "trivial = deterministic rank mapping when chord note count matches "
                "detected voice-part count (highest note -> highest voice part, etc.). "
                "If that equality does not hold, preflight fails and planner must "
                "explicitly switch method."
            ),
        },
        "rank_index": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "For method=ranked, 0 means highest note, 1 means second-highest, "
                "2 means third-highest, and so on."
            ),
        },
        "rank_fallback": {
            "type": "string",
            "enum": ["greedy", "skip"],
            "description": (
                "For method=ranked, behavior when the requested rank does not exist: "
                "greedy = use the nearest available lower rank; "
                "skip = emit no note at that onset."
            ),
        },
        "melody_source": {
            **_PREPROCESS_PLAN_VOICE_REF_SCHEMA,
            "description": (
                "Source lane for note events. Required for derive sections when target "
                "section has no usable notes or explicit melody replacement is needed."
            ),
        },
        "lyric_source": {
            **_PREPROCESS_PLAN_VOICE_REF_SCHEMA,
            "description": (
                "Source lane for lyric tokens. For lyric-only propagation, ensure target "
                "already has singable notes in this section."
            ),
        },
        "lyric_strategy": {
            "type": "string",
            "enum": ["strict_onset", "overlap_best_match", "syllable_flow"],
            "description": (
                "Alignment strategy for lyric copy: strict_onset for tight onsets; "
                "overlap_best_match for shifted rhythms; syllable_flow for phrase continuity."
            ),
        },
        "lyric_policy": {
            "type": "string",
            "enum": ["fill_missing_only", "replace_all", "preserve_existing"],
            "description": (
                "Write policy for target lyrics: fill_missing_only (default), "
                "replace_all (overwrite), preserve_existing (do not overwrite)."
            ),
        },
    },
    "required": ["start_measure", "end_measure", "mode"],
    "additionalProperties": False,
}

_PREPROCESS_PLAN_REQUEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Top-level preprocess plan. Use one or more targets; each target defines "
        "explicit timeline sections for derive/rest decisions."
    ),
    "properties": {
        "targets": {
            "type": "array",
            "minItems": 1,
            "description": "List of target lanes to derive. Executed sequentially.",
            "items": {
                "type": "object",
                "description": "Plan entry for one target lane.",
                "properties": {
                    "target": {
                        **_PREPROCESS_PLAN_VOICE_REF_SCHEMA,
                        "description": "Lane to materialize as derived output.",
                    },
                    "sections": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "Contiguous measure sections covering the full target span."
                        ),
                        "items": _PREPROCESS_PLAN_SECTION_SCHEMA,
                    },
                    "verse_number": {
                        "type": ["integer", "string", "null"],
                        "description": (
                            "Optional verse selector for lyric extraction (e.g., 1, 2)."
                        ),
                    },
                    "copy_all_verses": {
                        "type": "boolean",
                        "description": (
                            "If true, preserve/copy all verse lines instead of a single verse."
                        ),
                    },
                    "split_shared_note_policy": {
                        "type": "string",
                        "enum": ["duplicate_to_all", "assign_primary_only"],
                        "description": (
                            "Policy for shared/chord notes when splitting lanes: "
                            "duplicate_to_all duplicates shared notes; "
                            "assign_primary_only keeps shared notes only in primary lane."
                        ),
                    },
                    "confidence": {
                        "type": ["number", "string", "null"],
                        "description": "Optional planner confidence marker (metadata only).",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "Optional planner notes for debugging/audit.",
                    },
                },
                "required": ["target", "sections"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["targets"],
    "additionalProperties": False,
}

_PARSE_SCORE_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Parsed score payload with summary and voice-part analysis signals.",
    "properties": {
        "title": {"type": ["string", "null"], "description": "Score title when available."},
        "tempos": {
            "type": "array",
            "description": "Tempo map entries from MusicXML.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "parts": {
            "type": "array",
            "description": "Parsed score parts with notes/rests.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "structure": {
            "type": "object",
            "description": "Repeat/endings/jump placeholders.",
            "properties": {
                "repeats": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "endings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "jumps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
            "required": ["repeats", "endings", "jumps"],
            "additionalProperties": False,
        },
        "score_summary": {
            "type": "object",
            "description": "Compact static score summary for UI and orchestration.",
            "additionalProperties": True,
        },
        "source_musicxml_path": {
            "type": "string",
            "description": "Absolute source MusicXML path resolved by backend.",
        },
        "voice_part_signals": {
            "type": "object",
            "description": "Planner-oriented multi-voice analysis, preflight hints, and section diagnostics.",
            "properties": {
                "analysis_version": {"type": "string", "description": "Signal schema version."},
                "requested_verse_number": {
                    "type": ["integer", "null"],
                    "description": "Normalized requested verse number.",
                },
                "has_multi_voice_parts": {
                    "type": "boolean",
                    "description": "True when at least one part contains multiple voice lanes.",
                },
                "has_missing_lyric_voice_parts": {
                    "type": "boolean",
                    "description": "True when at least one lane has notes with missing lexical lyrics.",
                },
                "parts": {
                    "type": "array",
                    "description": "Per-part planning signal objects.",
                    "items": _VOICE_PART_SIGNAL_SCHEMA,
                },
                "full_score_analysis": {
                    "type": "object",
                    "description": "Full static score analysis used by planning/preflight checks.",
                    "properties": {
                        "parts": {"type": "array", "items": _FULL_SCORE_ANALYSIS_PART_SCHEMA},
                        "status_taxonomy": {
                            "type": "array",
                            "description": "Allowed status values used by preprocessing workflow.",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["parts", "status_taxonomy"],
                    "additionalProperties": False,
                },
                "measure_staff_voice_map": {
                    "type": "array",
                    "description": "MusicXML measure-level staff/voice presence and lyric attachment.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "part_id": {"type": ["string", "null"]},
                            "part_name": {"type": ["string", "null"]},
                            "measures": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "measure_number": {"type": "string"},
                                        "all_svs": {"type": "array", "items": _VOICE_SV_SCHEMA},
                                        "lyric_svs": {"type": "array", "items": _VOICE_SV_SCHEMA},
                                    },
                                    "required": ["measure_number", "all_svs", "lyric_svs"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["part_id", "part_name", "measures"],
                        "additionalProperties": False,
                    },
                },
                "measure_annotations": {
                    "type": "array",
                    "description": "Measure-level direction words used as phrase/section hints.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "part_id": {"type": ["string", "null"]},
                            "part_name": {"type": ["string", "null"]},
                            "measures": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "measure_number": {"type": "string"},
                                        "directions": {"type": "array", "items": {"type": "string"}},
                                        "direction_entries": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "text": {"type": "string"},
                                                    "staff": {"type": ["string", "null"]},
                                                    "voice": {"type": ["string", "null"]},
                                                    "placement": {"type": ["string", "null"]},
                                                    "offset_divisions": {"type": ["string", "null"]},
                                                },
                                                "required": ["text", "staff", "voice", "placement", "offset_divisions"],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": ["measure_number", "directions", "direction_entries"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["part_id", "part_name", "measures"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "analysis_version",
                "requested_verse_number",
                "has_multi_voice_parts",
                "has_missing_lyric_voice_parts",
                "parts",
                "full_score_analysis",
                "measure_staff_voice_map",
                "measure_annotations",
            ],
            "additionalProperties": False,
        },
        "requested_part_id": {
            "type": ["string", "null"],
            "description": "Echoed optional parse request argument.",
        },
        "requested_part_index": {
            "type": ["integer", "null"],
            "description": "Echoed optional parse request argument.",
        },
        "selected_verse_number": {
            "type": ["string", "null"],
            "description": (
                "Effective verse selection used by parser. "
                "If verse_number input is omitted, this is the deterministic default "
                "(first available verse)."
            ),
        },
    },
    "required": [
        "title",
        "tempos",
        "parts",
        "structure",
        "score_summary",
        "source_musicxml_path",
        "voice_part_signals",
        "selected_verse_number",
    ],
    "additionalProperties": True,
}


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
                "verse_number": {"type": ["integer", "string", "null"]},
                "expand_repeats": {"type": "boolean"},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        output_schema=_PARSE_SCORE_OUTPUT_SCHEMA,
    ),
    Tool(
        name="reparse",
        description=(
            "Re-parse the currently uploaded MusicXML using updated selection context "
            "(part and/or verse). In chat flow, backend injects file_path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "part_id": {"type": ["string", "null"]},
                "part_index": {"type": ["integer", "null"]},
                "verse_number": {"type": ["integer", "string", "null"]},
                "expand_repeats": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        output_schema=_PARSE_SCORE_OUTPUT_SCHEMA,
    ),
    Tool(
        name="preprocess_voice_parts",
        description=(
            "Run deterministic voice-part preprocessing using a required plan payload "
            "and return a transformed score or action_required."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "score": {"type": "object", "description": "Parsed score payload from parse_score."},
                "request": {
                    "type": "object",
                    "description": (
                        "Preprocess request payload. "
                        "request.plan is mandatory; exposed contract accepts sections-only plans."
                    ),
                    "properties": {
                        "plan": {
                            **_PREPROCESS_PLAN_REQUEST_SCHEMA,
                            "description": "Normalized sections-only preprocess plan.",
                        }
                    },
                    "required": ["plan"],
                    "additionalProperties": False,
                },
            },
            "required": ["score", "request"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["ready", "ready_with_warnings", "action_required", "error"],
                    "description": (
                        "Preprocess status. "
                        "ready=transformed score is valid for synthesis; "
                        "ready_with_warnings=usable but with non-blocking issues; "
                        "action_required=caller must provide additional input or change selection; "
                        "error=preprocess execution failed."
                    ),
                },
                "score": {"type": "object", "description": "Transformed score payload when status is ready."},
                "part_index": {"type": "integer", "description": "Resolved target part index for synthesis."},
                "warnings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "validation": {"type": "object", "additionalProperties": True},
                "action": {"type": "string", "description": "Action code when status=action_required."},
                "code": {"type": "string", "description": "Machine-readable action/error code."},
                "message": {"type": "string", "description": "User-facing guidance message."},
            },
            "required": ["status"],
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
                "mp3_bitrate": {"type": "string"},
                "keep_wav": {"type": "boolean"},
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
                "part_id": {"type": ["string", "null"]},
                "part_index": {"type": ["integer", "null"]},
                "voice_id": {"type": ["string", "null"]},
                "voice_part_id": {"type": ["string", "null"]},
                "allow_lyric_propagation": {"type": "boolean"},
                "source_voice_part_id": {"type": ["string", "null"]},
                "source_part_index": {"type": ["integer", "null"]},
                "voice_color": {"type": ["string", "null"]},
                "articulation": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                "airiness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "intensity": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                "clarity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["score"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["action_required"],
                    "description": (
                        "When present, currently action_required. "
                        "Successful renders return waveform/sample_rate/duration_seconds without status."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["preprocessing_required", "infeasible_anchor_budget"],
                    "description": (
                        "Action key when status=action_required. "
                        "preprocessing_required=run preprocess_voice_parts before synth; "
                        "infeasible_anchor_budget=timing constraints cannot be satisfied for at least one lyric group."
                    ),
                },
                "code": {
                    "type": "string",
                    "enum": ["preprocessing_required", "infeasible_anchor_budget"],
                    "description": "Machine-readable code aligned with action for deterministic client handling.",
                },
                "reason": {
                    "type": "string",
                    "enum": [
                        "complex_score_multi_voice_and_missing_lyrics_without_derived_target",
                        "complex_score_multi_voice_without_derived_target",
                        "complex_score_missing_lyrics_without_derived_target",
                        "preprocessed_score_without_derived_target_selection",
                        "complexity_signal_unavailable_without_derived_target",
                        "target_part_not_found_for_preflight",
                        "verse_change_requires_repreprocess",
                    ],
                    "description": (
                        "Specific preflight reason for preprocessing_required. "
                        "multi_voice_and_missing_lyrics=both complexity signals present; "
                        "multi_voice=multi-voice part requires derived target; "
                        "missing_lyrics=missing lyric lanes require preprocessing; "
                        "preprocessed_score_without_derived_target_selection=preprocess already ran, "
                        "but synthesize targeted a raw/original complex part instead of a derived target; "
                        "complexity_signal_unavailable=score lacks required analysis signals; "
                        "target_part_not_found=requested part index is invalid; "
                        "verse_change_requires_repreprocess=user changed verse after preprocessing, "
                        "so workflow must restart from parse/preprocess for the new verse."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "Human-readable explanation or follow-up guidance.",
                },
                "failed_validation_rules": {
                    "type": "array",
                    "description": "List of exact validation checks that failed before synthesis execution.",
                    "items": {
                        "type": "string",
                        "enum": [
                            "complexity_signal.multi_voice_part",
                            "complexity_signal.missing_lyric_voice_parts",
                            "derived_detection.index_delta_not_met",
                            "derived_detection.transform_metadata_not_found",
                            "derived_detection.derived_target_not_selected_after_preprocess",
                            "complexity_signal.parts_missing",
                            "complexity_signal.target_part_signal_missing",
                            "input_validation.part_index_out_of_range",
                            "verse_lock.requested_verse_differs_from_selected_verse",
                            "workflow_restart.reparse_and_repreprocess_required",
                        ],
                    },
                },
                "diagnostics": {
                    "type": "object",
                    "description": "Structured debug context for failed preflight checks.",
                    "additionalProperties": True,
                },
                "waveform": {
                    "type": "array",
                    "description": "Synthesized PCM waveform samples (float values). Present on success.",
                    "items": {"type": "number"},
                },
                "sample_rate": {
                    "type": "integer",
                    "description": "Audio sample rate in Hz. Present on success.",
                },
                "duration_seconds": {
                    "type": "number",
                    "description": "Rendered audio duration in seconds. Present on success.",
                },
            },
            "additionalProperties": True,
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
                "voice_colors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "suffix": {"type": "string"},
                        },
                        "required": ["name", "suffix"],
                        "additionalProperties": False,
                    },
                },
                "default_voice_color": {"type": ["string", "null"]},
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
                "voice_colors",
                "default_voice_color",
                "sample_rate",
                "hop_size",
                "use_lang_id",
            ],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="estimate_credits",
        description="Estimate the credit cost for a score.",
        input_schema={
            "type": "object",
            "properties": {
                "score": {"type": "object"},
                "uid": {"type": "string"}, # Optional, provided by orchestrator
                "email": {"type": "string"},
                "duration_seconds": {"type": "number"},
            },
            "required": ["score"],
            "additionalProperties": True,
        },
        output_schema={
            "type": "object",
            "properties": {
                "estimated_seconds": {"type": "number"},
                "estimated_credits": {"type": "integer"},
                "current_balance": {"type": "integer"},
                "balance_after": {"type": "integer"},
                "sufficient": {"type": "boolean"},
            },
            "required": ["estimated_seconds", "estimated_credits"],
            "additionalProperties": True,
        },
    ),
]


def list_tools(allowlist: set[str] | None = None) -> List[Dict[str, Any]]:
    """Return tool metadata, optionally filtered by allowlist."""
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
    """Invoke a tool handler and log its input/output."""
    if name not in HANDLERS:
        raise ValueError(f"Unknown tool: {name}")
    handler = HANDLERS[name]
    logger = logging.getLogger(__name__)
    logger.debug("Dispatch tool=%s args=%s", name, summarize_payload(arguments))
    result = handler(arguments, device)
    logger.debug("Return tool=%s result=%s", name, summarize_payload(result))
    return result
