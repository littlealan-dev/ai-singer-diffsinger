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

_PREPROCESS_RULE_ENTRY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Structured preflight or postflight issue entry emitted by preprocess validation.",
    "properties": {
        "rule": {"type": "string", "description": "Machine-readable rule code."},
        "rule_name": {"type": "string", "description": "Human-readable rule title."},
        "rule_definition": {"type": "string", "description": "Canonical rule definition."},
        "fail_condition": {"type": "string", "description": "Condition that caused this rule to trigger."},
        "suggestion": {"type": "string", "description": "Suggested repair strategy for the planner."},
        "rule_severity": {
            "type": "string",
            "description": "Priority bucket for selection logic (for example P0, P1, or P2).",
        },
        "rule_domain": {
            "type": "string",
            "description": "Issue domain such as STRUCTURAL or LYRIC.",
        },
        "message": {"type": "string", "description": "Concrete rendered message for this issue."},
        "failing_attributes": {
            "type": "object",
            "description": "Structured dynamic values that explain why the rule failed.",
            "additionalProperties": True,
        },
        "impacted_measures": {
            "type": "array",
            "description": "Flattened impacted measure numbers for ranking and UI hints.",
            "items": {"type": "integer"},
        },
        "impacted_ranges": {
            "type": "array",
            "description": "Inclusive impacted measure ranges for section-level repair targeting.",
            "items": _MEASURE_RANGE_SCHEMA,
        },
    },
    "required": ["rule"],
    "additionalProperties": True,
}

_PREPROCESS_WARNING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Non-blocking preprocess warning entry.",
    "properties": {
        "code": {"type": "string", "description": "Warning code."},
        "message": {"type": "string", "description": "Rendered warning text."},
        "rule_metadata": _PREPROCESS_RULE_ENTRY_SCHEMA,
    },
    "required": ["code", "message"],
    "additionalProperties": True,
}

_PREPROCESS_VALIDATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Aggregate lyric/structure validation metrics for the current preprocess candidate.",
    "properties": {
        "lyric_coverage_ratio": {
            "type": "number",
            "description": "Overall lyric-token coverage across target sung notes.",
        },
        "word_lyric_coverage_ratio": {
            "type": "number",
            "description": "Coverage ratio counting only lexical word lyrics, excluding '+' extensions.",
        },
        "extension_lyric_ratio": {
            "type": "number",
            "description": "Fraction of target sung notes using extension '+' lyrics.",
        },
        "word_lyric_note_count": {
            "type": "integer",
            "description": "Count of target notes that received lexical word lyrics.",
        },
        "extension_lyric_note_count": {
            "type": "integer",
            "description": "Count of target notes that received extension '+' lyrics.",
        },
        "source_word_lyric_note_count": {
            "type": "integer",
            "description": "Count of lexical word lyrics available from the selected source lane(s).",
        },
        "source_alignment_ratio": {
            "type": "number",
            "description": "Alignment ratio between source lyric candidates and mapped target notes.",
        },
        "missing_lyric_sung_note_count": {
            "type": "integer",
            "description": "Count of sung target notes still missing lyrics after propagation.",
        },
        "lyric_exempt_note_count": {
            "type": "integer",
            "description": "Count of target notes intentionally exempted from lyric coverage checks.",
        },
        "unresolved_measures": {
            "type": "array",
            "description": "Measure numbers still containing unresolved lyric issues.",
            "items": {"type": "integer"},
        },
        "structural": {
            "type": "object",
            "description": "Structural singability diagnostics for the candidate lane.",
            "properties": {
                "hard_fail": {
                    "type": "boolean",
                    "description": "True if the candidate is structurally invalid for monophonic synthesis.",
                },
                "max_simultaneous_notes": {
                    "type": "integer",
                    "description": "Maximum simultaneous non-rest note count in the target lane.",
                },
                "simultaneous_conflict_count": {
                    "type": "integer",
                    "description": "Count of simultaneous-onset conflicts in the target lane.",
                },
                "overlap_conflict_count": {
                    "type": "integer",
                    "description": "Count of overlapping-note conflicts in the target lane.",
                },
                "structural_unresolved_measures": {
                    "type": "array",
                    "description": "Measure numbers with structural conflicts.",
                    "items": {"type": "integer"},
                },
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}

_PREPROCESS_SECTION_RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Per-section execution summary for a preprocess target.",
    "properties": {
        "section_mode": {"type": "string", "description": "Resolved section mode such as derive or rest."},
        "decision_type": {"type": "string", "description": "Planner decision type used for this section."},
        "method": {"type": "string", "description": "Selection method used for this section."},
        "start_measure": {"type": "integer", "description": "Inclusive section start measure."},
        "end_measure": {"type": "integer", "description": "Inclusive section end measure."},
        "copied_note_count": {"type": "integer", "description": "Number of notes copied/materialized for this section."},
        "copied_lyric_count": {"type": "integer", "description": "Number of lyric tokens copied for this section."},
        "copied_word_lyric_count": {"type": "integer", "description": "Number of lexical word lyrics copied for this section."},
        "copied_extension_lyric_count": {"type": "integer", "description": "Number of '+' extension lyrics copied for this section."},
        "missing_lyric_sung_note_count": {"type": "integer", "description": "Remaining missing-lyric sung notes in this section."},
        "source_lyric_candidates_count": {"type": "integer", "description": "Number of source lyric candidates seen in this section."},
        "mapped_source_lyrics_count": {"type": "integer", "description": "Number of source lyrics successfully mapped to target notes."},
        "dropped_source_lyrics_count": {"type": "integer", "description": "Number of source lyrics that could not be mapped."},
        "dropped_source_lyrics": {
            "type": "array",
            "description": "Dropped source lyric details for debugging.",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "additionalProperties": True,
}

_PREPROCESS_REVIEW_MATERIALIZATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Deferred materialization payload used by the orchestrator to finalize the selected review candidate.",
    "properties": {
        "score": {
            "type": "object",
            "description": "In-memory score payload for the candidate before final artifact materialization.",
        },
        "part_index": {
            "type": "integer",
            "description": "Source part index from which the derived target was built.",
        },
        "target_voice_part_id": {
            "type": "string",
            "description": "Target voice-part id for the candidate being materialized.",
        },
        "source_voice_part_id": {
            "type": ["string", "null"],
            "description": "Primary source voice-part id used for the candidate when available.",
        },
        "source_part_index": {
            "type": ["integer", "null"],
            "description": "Primary source part index used for the candidate when available.",
        },
        "transformed_part": {
            "type": "object",
            "description": "Derived MusicXML part payload that will be appended/materialized if selected.",
            "additionalProperties": True,
        },
        "propagated": {
            "type": "boolean",
            "description": "True if lyric/note propagation succeeded enough to build a review candidate.",
        },
        "status": {
            "type": "string",
            "description": "Deferred candidate status before final materialization, typically ready.",
        },
        "warnings": {
            "type": "array",
            "description": "Warning list that should be preserved when materialized.",
            "items": _PREPROCESS_WARNING_SCHEMA,
        },
        "validation": _PREPROCESS_VALIDATION_SCHEMA,
        "source_musicxml_path": {
            "type": ["string", "null"],
            "description": "Current source MusicXML path backing this candidate before final materialization.",
        },
        "target_source_voice_id": {
            "type": ["string", "null"],
            "description": "Original MusicXML voice id associated with the target lane when available.",
        },
        "metadata": {
            "type": "object",
            "description": "Auxiliary planner/execution metadata to preserve at finalization time.",
            "additionalProperties": True,
        },
    },
    "required": ["score", "part_index", "target_voice_part_id", "transformed_part", "status"],
    "additionalProperties": True,
}

_PREPROCESS_TARGET_RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Per-target preprocess result for one derived target in a multi-target attempt.",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "ready_with_warnings", "action_required", "error"],
            "description": "Target-local preprocess status.",
        },
        "action": {
            "type": ["string", "null"],
            "description": "Target-local action code when status=action_required.",
        },
        "code": {
            "type": ["string", "null"],
            "description": "Machine-readable target-local code when available.",
        },
        "message": {
            "type": ["string", "null"],
            "description": "Target-local summary message.",
        },
        "part_index": {
            "type": ["integer", "null"],
            "description": "Source part index for this target.",
        },
        "target_voice_part_id": {
            "type": ["string", "null"],
            "description": "Target voice-part id for this target result.",
        },
        "transform_id": {
            "type": ["string", "null"],
            "description": "Transform id for this target when materialized or reconstructed.",
        },
        "appended_part_ref": {
            "type": ["object", "null"],
            "description": "Derived MusicXML part reference for this target when exported.",
            "additionalProperties": True,
        },
        "hidden_default_lane": {
            "type": "boolean",
            "description": "True if this target is a hidden helper/default lane, not a visible exported derived part.",
        },
        "visible": {
            "type": "boolean",
            "description": "True if this target contributes to the visible user-reviewable derived output.",
        },
        "review_required": {
            "type": "boolean",
            "description": "True if this target still requires user review.",
        },
        "structurally_valid": {
            "type": "boolean",
            "description": "True if this target is free of structural P0 issues.",
        },
        "quality_class": {
            "type": "integer",
            "enum": [1, 2, 3],
            "description": "Target-local quality class: 1=has P1, 2=only P2, 3=no remaining issues.",
        },
        "structural_p1_measures": {
            "type": "integer",
            "description": "Count of impacted structural P1 measures for this target.",
        },
        "lyric_p1_measures": {
            "type": "integer",
            "description": "Count of impacted lyric P1 measures for this target.",
        },
        "p2_measures": {
            "type": "integer",
            "description": "Count of impacted P2 measures for this target.",
        },
        "issues": {
            "type": "array",
            "description": "Normalized issue list for this target only.",
            "items": _PREPROCESS_RULE_ENTRY_SCHEMA,
        },
        "validation": _PREPROCESS_VALIDATION_SCHEMA,
        "warnings": {
            "type": "array",
            "description": "Non-blocking warnings for this target.",
            "items": _PREPROCESS_WARNING_SCHEMA,
        },
        "section_results": {
            "type": "array",
            "description": "Per-section execution summaries for this target.",
            "items": _PREPROCESS_SECTION_RESULT_SCHEMA,
        },
        "failing_ranges": {
            "type": "array",
            "description": "Inclusive unresolved ranges for this target.",
            "items": _MEASURE_RANGE_SCHEMA,
        },
        "failed_validation_rules": {
            "type": "array",
            "description": "Structured target-local validation failures.",
            "items": _PREPROCESS_RULE_ENTRY_SCHEMA,
        },
        "modified_musicxml_path": {
            "type": ["string", "null"],
            "description": "Derived MusicXML artifact path after this target was materialized, when available.",
        },
    },
    "required": ["status", "visible", "structurally_valid", "quality_class", "issues"],
    "additionalProperties": True,
}

_PREPROCESS_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Result payload for preprocess_voice_parts, including ready, warning, review-required, and error states.",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "ready_with_warnings", "action_required", "error"],
            "description": (
                "Preprocess status. "
                "ready=transformed score is valid for synthesis; "
                "ready_with_warnings=usable but has non-blocking warnings; "
                "action_required=planner/user must review or change selection; "
                "error=preprocess execution failed."
            ),
        },
        "score": {
            "type": "object",
            "description": "Transformed score payload when the candidate has been finalized/materialized.",
        },
        "part_index": {
            "type": "integer",
            "description": "Resolved target part index for the primary derived target.",
        },
        "target_voice_part": {
            "type": "string",
            "description": "Target voice-part id for the primary candidate when relevant.",
        },
        "modified_musicxml_path": {
            "type": ["string", "null"],
            "description": "Derived MusicXML artifact path when a candidate was materialized.",
        },
        "warnings": {
            "type": "array",
            "description": "Non-blocking warnings attached to a usable candidate.",
            "items": _PREPROCESS_WARNING_SCHEMA,
        },
        "validation": _PREPROCESS_VALIDATION_SCHEMA,
        "section_results": {
            "type": "array",
            "description": "Per-section execution summaries for the primary target.",
            "items": _PREPROCESS_SECTION_RESULT_SCHEMA,
        },
        "failing_ranges": {
            "type": "array",
            "description": "Inclusive measure ranges that remain unresolved after this attempt.",
            "items": _MEASURE_RANGE_SCHEMA,
        },
        "failed_validation_rules": {
            "type": "array",
            "description": "Structured postflight validation failures for this attempt.",
            "items": _PREPROCESS_RULE_ENTRY_SCHEMA,
        },
        "lint_findings": {
            "type": "array",
            "description": "Structured preflight lint findings when the plan is invalid before execution.",
            "items": _PREPROCESS_RULE_ENTRY_SCHEMA,
        },
        "action": {
            "type": "string",
            "description": "Action code when status=action_required.",
        },
        "code": {
            "type": "string",
            "description": "Machine-readable action/error code aligned with status/action.",
        },
        "message": {
            "type": "string",
            "description": "Human-readable summary of the preprocess outcome.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": "Optional machine-readable reason describing why action is required.",
        },
        "diagnostics": {
            "type": "object",
            "description": "Additional structured diagnostics for planner/debug use.",
            "additionalProperties": True,
        },
        "review_materialization": _PREPROCESS_REVIEW_MATERIALIZATION_SCHEMA,
        "targets": {
            "type": "array",
            "description": (
                "Per-target preprocess results for the attempt. "
                "Quality, issues, and visibility must be interpreted per target; "
                "hidden helper lanes may be present but should not drive user-facing candidate quality."
            ),
            "items": _PREPROCESS_TARGET_RESULT_SCHEMA,
        },
    },
    "required": ["status"],
    "additionalProperties": True,
}

_SAVE_AUDIO_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Serialized audio artifact produced by save_audio.",
    "properties": {
        "audio_base64": {
            "type": "string",
            "description": "Base64-encoded bytes of the rendered audio file.",
        },
        "duration_seconds": {
            "type": "number",
            "description": "Audio duration in seconds.",
        },
        "sample_rate": {
            "type": "integer",
            "description": "Final audio sample rate in Hz.",
        },
    },
    "required": ["audio_base64", "duration_seconds", "sample_rate"],
    "additionalProperties": False,
}

_SYNTH_FAILED_RULE_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "description": "Exact synth preflight validation rule that failed.",
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
}

_SYNTH_ACTION_REQUIRED_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Action-required synth response emitted when the score must be preprocessed or timing constraints fail.",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["action_required"],
            "description": "Non-success synth result requiring planner or user action.",
        },
        "action": {
            "type": "string",
            "enum": ["preprocessing_required", "infeasible_anchor_budget"],
            "description": (
                "Action key for deterministic handling. "
                "preprocessing_required means preprocess_voice_parts must run first. "
                "infeasible_anchor_budget means timing constraints cannot be satisfied."
            ),
        },
        "code": {
            "type": "string",
            "enum": ["preprocessing_required", "infeasible_anchor_budget"],
            "description": "Machine-readable code aligned with action.",
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
            "description": "Specific preflight reason explaining why synthesis cannot proceed yet.",
        },
        "message": {
            "type": "string",
            "description": "Human-readable explanation or next-step guidance.",
        },
        "failed_validation_rules": {
            "type": "array",
            "description": "Exact synth preflight validation rules that failed.",
            "items": _SYNTH_FAILED_RULE_SCHEMA,
        },
        "diagnostics": {
            "type": "object",
            "description": "Structured debug context for failed synth preflight checks.",
            "additionalProperties": True,
        },
    },
    "required": ["status", "action", "code", "message"],
    "additionalProperties": True,
}

_SYNTH_SUCCESS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Successful synth output containing rendered waveform data.",
    "properties": {
        "waveform": {
            "type": "array",
            "description": "Synthesized PCM waveform samples as float values.",
            "items": {"type": "number"},
        },
        "sample_rate": {
            "type": "integer",
            "description": "Audio sample rate in Hz.",
        },
        "duration_seconds": {
            "type": "number",
            "description": "Rendered audio duration in seconds.",
        },
    },
    "required": ["waveform", "sample_rate", "duration_seconds"],
    "additionalProperties": False,
}

_SYNTH_OUTPUT_SCHEMA: Dict[str, Any] = {
    "description": "Synth output, either a successful waveform payload or an action_required preflight result.",
    "oneOf": [_SYNTH_SUCCESS_SCHEMA, _SYNTH_ACTION_REQUIRED_SCHEMA],
}

_VOICEBANK_ENTRY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Single voicebank discovered by the backend.",
    "properties": {
        "id": {"type": "string", "description": "Stable voicebank id used in synth requests."},
        "name": {"type": "string", "description": "Human-readable voicebank name."},
        "path": {"type": "string", "description": "Filesystem path to the voicebank directory."},
    },
    "required": ["id", "name", "path"],
    "additionalProperties": False,
}

_VOICEBANK_INFO_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Metadata and capabilities for one voicebank.",
    "properties": {
        "name": {"type": "string", "description": "Human-readable voicebank name."},
        "languages": {
            "type": "array",
            "description": "Supported language codes or display names.",
            "items": {"type": "string"},
        },
        "has_duration_model": {
            "type": "boolean",
            "description": "Whether the voicebank includes a duration model.",
        },
        "has_pitch_model": {
            "type": "boolean",
            "description": "Whether the voicebank includes a pitch model.",
        },
        "has_variance_model": {
            "type": "boolean",
            "description": "Whether the voicebank includes a variance model.",
        },
        "speakers": {
            "type": "array",
            "description": "Available speaker identifiers for multi-speaker voicebanks.",
            "items": {
                "description": "Speaker entry, usually a display name or backend-specific structured object.",
                "oneOf": [
                    {"type": "string", "description": "Speaker display name or identifier."},
                    {"type": "object", "description": "Structured speaker metadata.", "additionalProperties": True},
                ],
            },
        },
        "voice_colors": {
            "type": "array",
            "description": "Available voice color/style options.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Voice color/style display name."},
                    "suffix": {"type": "string", "description": "Suffix token used by the backend to select this color."},
                },
                "required": ["name", "suffix"],
                "additionalProperties": False,
            },
        },
        "default_voice_color": {
            "type": ["string", "null"],
            "description": "Default voice color/style when no explicit color is chosen.",
        },
        "sample_rate": {"type": "integer", "description": "Native output sample rate in Hz."},
        "hop_size": {"type": "integer", "description": "Model hop size used by the backend."},
        "use_lang_id": {"type": "boolean", "description": "Whether language-id conditioning is used."},
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
}

_BACKING_TRACK_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Generated backing-track artifact and prompt metadata.",
    "properties": {
        "status": {
            "type": "string",
            "description": "completed when the backing track was generated successfully.",
        },
        "output_path": {
            "type": "string",
            "description": "Project-relative output audio path.",
        },
        "duration_seconds": {
            "type": "number",
            "description": "Rendered audio duration in seconds.",
        },
        "output_format": {
            "type": "string",
            "description": "Effective ElevenLabs output format used for generation.",
        },
        "wav_output_path": {
            "type": ["string", "null"],
            "description": "Project-relative WAV artifact path retained for reuse and mixing.",
        },
        "backing_track_prompt": {
            "type": ["string", "null"],
            "description": "Final prompt sent to ElevenLabs music.compose when a new backing track was generated.",
        },
        "metadata": {
            "type": "object",
            "description": "Deterministic extracted score metadata used for prompt writing.",
            "additionalProperties": True,
        },
    },
    "required": [
        "status",
        "output_path",
        "duration_seconds",
        "output_format",
        "metadata",
    ],
    "additionalProperties": False,
}

_COMBINED_BACKING_TRACK_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Generated combined melody+backing artifact and any reusable backing-track metadata.",
    "properties": {
        "status": {
            "type": "string",
            "description": "completed when the combined backing track was generated successfully.",
        },
        "output_path": {
            "type": "string",
            "description": "Project-relative combined output audio path.",
        },
        "duration_seconds": {
            "type": "number",
            "description": "Combined audio duration in seconds.",
        },
        "output_format": {
            "type": "string",
            "description": "Effective output format delivered to the client.",
        },
        "backing_track_prompt": {
            "type": ["string", "null"],
            "description": "Final prompt sent to ElevenLabs if a new pure backing track had to be generated.",
        },
        "metadata": {
            "type": "object",
            "description": "Deterministic extracted score metadata used for prompt writing.",
            "additionalProperties": True,
        },
        "backing_track_output_path": {
            "type": "string",
            "description": "Project-relative pure backing-track delivery artifact path reused or generated for mixing.",
        },
        "backing_track_wav_output_path": {
            "type": "string",
            "description": "Project-relative pure backing-track WAV path used for mixing.",
        },
        "backing_track_duration_seconds": {
            "type": "number",
            "description": "Duration of the pure backing-track artifact in seconds.",
        },
        "reused_existing_backing_track": {
            "type": "boolean",
            "description": "True when the combined tool reused an existing pure backing-track WAV instead of regenerating it.",
        },
        "render_variant": {
            "type": "string",
            "description": "Artifact variant identifier.",
        },
    },
    "required": [
        "status",
        "output_path",
        "duration_seconds",
        "output_format",
        "metadata",
        "backing_track_output_path",
        "backing_track_wav_output_path",
        "backing_track_duration_seconds",
        "reused_existing_backing_track",
        "render_variant",
    ],
    "additionalProperties": False,
}

TOOLS: List[Tool] = [
    Tool(
        name="parse_score",
        description="Parse MusicXML into a score JSON dict.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the MusicXML file to parse.",
                },
                "part_id": {
                    "type": ["string", "null"],
                    "description": "Optional MusicXML part id to focus parsing on one part.",
                },
                "part_index": {
                    "type": ["integer", "null"],
                    "description": "Optional 0-based part index to focus parsing on one part.",
                },
                "verse_number": {
                    "type": ["integer", "string", "null"],
                    "description": (
                        "Optional verse selector for lyric extraction. "
                        "For multi-verse rendering workflows, provide an explicit verse "
                        "via reparse before preprocess/synthesize."
                    ),
                },
                "expand_repeats": {
                    "type": "boolean",
                    "description": "Whether repeat structures should be expanded during parsing.",
                },
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
                "part_id": {
                    "type": ["string", "null"],
                    "description": "Optional MusicXML part id to select on reparse.",
                },
                "part_index": {
                    "type": ["integer", "null"],
                    "description": "Optional 0-based part index to select on reparse.",
                },
                "verse_number": {
                    "type": ["integer", "string", "null"],
                    "description": (
                        "Optional verse selector for lyric extraction during reparse. "
                        "Use this to explicitly lock the active verse when score_summary.available_verses "
                        "contains multiple entries."
                    ),
                },
                "expand_repeats": {
                    "type": "boolean",
                    "description": "Whether repeat structures should be expanded during reparse.",
                },
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
            **_PREPROCESS_OUTPUT_SCHEMA,
        },
    ),
    Tool(
        name="save_audio",
        description="Save audio to a file and return base64 bytes.",
        input_schema={
            "type": "object",
            "properties": {
                "waveform": {
                    "type": "array",
                    "description": "PCM waveform samples to serialize.",
                    "items": {"type": "number"},
                },
                "output_path": {
                    "type": "string",
                    "description": "Destination file path, including filename and extension.",
                },
                "sample_rate": {
                    "type": "integer",
                    "description": "Waveform sample rate in Hz.",
                },
                "format": {
                    "type": "string",
                    "description": "Requested output audio format such as wav or mp3.",
                },
                "mp3_bitrate": {
                    "type": "string",
                    "description": "Optional MP3 bitrate string such as 192k.",
                },
                "keep_wav": {
                    "type": "boolean",
                    "description": "Whether to keep an intermediate WAV when writing compressed output formats.",
                },
            },
            "required": ["waveform", "output_path"],
            "additionalProperties": False,
        },
        output_schema=_SAVE_AUDIO_OUTPUT_SCHEMA,
    ),
    Tool(
        name="synthesize",
        description="Run the full pipeline (internal steps 2-6) in one call.",
        input_schema={
            "type": "object",
            "properties": {
                "score": {
                    "type": "object",
                    "description": "Parsed score payload or derived score payload to synthesize from.",
                },
                "voicebank": {
                    "type": "string",
                    "description": "Voicebank id to render with.",
                },
                "part_id": {
                    "type": ["string", "null"],
                    "description": "Exact MusicXML/derived part id to synthesize. Use exactly one of part_id or part_index.",
                },
                "part_index": {
                    "type": ["integer", "null"],
                    "description": "0-based score.parts index to synthesize. Use exactly one of part_id or part_index.",
                },
                "voice_id": {
                    "type": ["string", "null"],
                    "description": "Optional original MusicXML voice id override when targeting a raw part.",
                },
                "voice_part_id": {
                    "type": ["string", "null"],
                    "description": "Optional normalized voice-part id hint for preprocess-aware flows.",
                },
                "allow_lyric_propagation": {
                    "type": "boolean",
                    "description": "Whether synth preflight may attempt lyric propagation on simple cases.",
                },
                "source_voice_part_id": {
                    "type": ["string", "null"],
                    "description": "Optional source voice-part id hint for lyric propagation or derived-target reuse.",
                },
                "source_part_index": {
                    "type": ["integer", "null"],
                    "description": "Optional source part index hint for lyric propagation or derived-target reuse.",
                },
                "verse_number": {
                    "type": ["integer", "string", "null"],
                    "description": (
                        "Optional explicit verse selector used by orchestration guards. "
                        "Required for multi-verse scores before preprocess/synthesize can proceed."
                    ),
                },
                "voice_color": {
                    "type": ["string", "null"],
                    "description": "Optional voice color/style name supported by the selected voicebank.",
                },
                "articulation": {
                    "type": "number",
                    "minimum": -1.0,
                    "maximum": 1.0,
                    "description": "Articulation control value for the synthesis backend.",
                },
                "airiness": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Airiness control value for the synthesis backend.",
                },
                "intensity": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.5,
                    "description": "Intensity control value for the synthesis backend.",
                },
                "clarity": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Clarity control value for the synthesis backend.",
                },
            },
            "required": ["score"],
            "oneOf": [
                {"required": ["part_id"]},
                {"required": ["part_index"]},
            ],
            "additionalProperties": False,
        },
        output_schema=_SYNTH_OUTPUT_SCHEMA,
    ),
    Tool(
        name="generate_backing_track",
        description=(
            "Generate an original instrumental backing track in ElevenLabs after a successful "
            "singing render exists for the current score. In chat flow, backend injects score file context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "style_request": {
                    "type": "string",
                    "description": "Natural-language style or genre request such as lo-fi, jazz trio, or bossa nova.",
                },
                "additional_requirements": {
                    "type": ["string", "null"],
                    "description": "Optional extra arrangement or mood requirements from the user.",
                },
                "output_format": {
                    "type": ["string", "null"],
                    "description": "Optional ElevenLabs output format override such as mp3_44100_128.",
                },
                "seed": {
                    "type": ["integer", "null"],
                    "description": "Optional ElevenLabs music generation seed.",
                },
            },
            "required": ["style_request"],
            "additionalProperties": False,
        },
        output_schema=_BACKING_TRACK_OUTPUT_SCHEMA,
    ),
    Tool(
        name="generate_combined_backing_track",
        description=(
            "Generate one mixed audio file containing both the melody and the instrumental backing track. "
            "Requires a successful singing render for the current score. In chat flow, backend injects "
            "the score file context and any reusable backing-track artifacts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "style_request": {
                    "type": "string",
                    "description": "Natural-language style or genre request such as simple pop rock, bossa nova, or jazz trio.",
                },
                "additional_requirements": {
                    "type": ["string", "null"],
                    "description": "Optional extra arrangement or mood requirements from the user.",
                },
                "output_format": {
                    "type": ["string", "null"],
                    "description": "Optional final output format override such as mp3_44100_128.",
                },
                "seed": {
                    "type": ["integer", "null"],
                    "description": "Optional ElevenLabs music generation seed.",
                },
            },
            "required": ["style_request"],
            "additionalProperties": False,
        },
        output_schema=_COMBINED_BACKING_TRACK_OUTPUT_SCHEMA,
    ),
    Tool(
        name="list_voicebanks",
        description="List available voicebanks.",
        input_schema={
            "type": "object",
            "properties": {
                "search_path": {
                    "type": "string",
                    "description": "Optional override path to scan for voicebanks instead of the backend default.",
                },
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "array",
            "description": "Available voicebank entries discovered by the backend.",
            "items": _VOICEBANK_ENTRY_SCHEMA,
        },
    ),
    Tool(
        name="get_voicebank_info",
        description="Get metadata and capabilities of a voicebank.",
        input_schema={
            "type": "object",
            "properties": {
                "voicebank": {"type": "string", "description": "Voicebank id to inspect."},
            },
            "required": ["voicebank"],
            "additionalProperties": False,
        },
        output_schema=_VOICEBANK_INFO_SCHEMA,
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
