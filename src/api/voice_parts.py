"""Voice part analysis and preprocessing helpers for compact multi-voice scores."""

from __future__ import annotations

import os
import re
import json
import hashlib
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET
from src.api.voice_part_lint_rules import get_lint_rule_spec

VOICE_PART_STATUSES = {
    "ready",
    "ready_with_warnings",
    "action_required",
    "error",
}
VALID_PLAN_ACTION_TYPES = {
    "split_voice_part",
    "propagate_lyrics",
    "duplicate_section_to_all_voice_parts",
}
VALID_SPLIT_SHARED_NOTE_POLICIES = {
    "duplicate_to_all",
    "assign_primary_only",
}
VALID_PROPAGATION_STRATEGIES = {
    "strict_onset",
    "overlap_best_match",
    "syllable_flow",
}
VALID_PROPAGATION_POLICIES = {
    "fill_missing_only",
    "replace_all",
    "preserve_existing",
}
VALID_TIMELINE_SECTION_MODES = {"rest", "derive"}
VALID_TIMELINE_DECISION_TYPES = {
    "EXTRACT_FROM_VOICE",
    "SPLIT_CHORDS_SELECT_NOTES",
    "COPY_UNISON_SECTION",
    "INSERT_RESTS",
    "DROP_NOTES_IF_NEEDED",
}
PUBLIC_TIMELINE_METHODS = {"trivial", "ranked"}
# Deprecated internal-only methods kept for compatibility with repair paths.
INTERNAL_TIMELINE_METHODS = {"A", "B"}
VALID_TIMELINE_METHODS = PUBLIC_TIMELINE_METHODS | INTERNAL_TIMELINE_METHODS
VALID_RANK_FALLBACKS = {"greedy", "skip"}
_ARTIFACT_LOCKS_GUARD = threading.Lock()
_ARTIFACT_LOCKS: Dict[str, threading.Lock] = {}
_TRANSFORM_ARTIFACT_INDEX: Dict[str, Dict[str, Any]] = {}
_DERIVED_STEM_SUFFIX_RE = re.compile(r"\.derived_[0-9a-f]{10}$")


def _lint_finding(
    rule: str,
    *,
    severity: str = "error",
    failing_attributes: Optional[Dict[str, Any]] = None,
    **payload: Any,
) -> Dict[str, Any]:
    spec = get_lint_rule_spec(rule)
    rendered_message = spec.message_template
    if spec.message_template:
        try:
            rendered_message = spec.message_template.format(**(failing_attributes or {}))
        except Exception:
            rendered_message = spec.message_template
    finding: Dict[str, Any] = {
        "rule": spec.code,
        "rule_name": spec.name,
        "rule_definition": spec.definition,
        "fail_condition": spec.fail_condition,
        "suggestion": spec.suggestion,
        "severity": severity,
        "message": rendered_message,
    }
    if failing_attributes:
        finding["failing_attributes"] = failing_attributes
    finding.update(payload)
    return finding


def analyze_score_voice_parts(
    score: Dict[str, Any], *, verse_number: Optional[str | int] = None
) -> Dict[str, Any]:
    """Return multi-voice and missing-lyric signals for parsed score data."""
    parts = score.get("parts") or []
    all_sources = _collect_all_lyric_source_options(score)
    normalized_verse = _normalize_verse_number(verse_number)
    part_signals: List[Dict[str, Any]] = []
    analysis_parts: List[Dict[str, Any]] = []
    for idx, part in enumerate(parts):
        full = _analyze_part_voice_parts(part, idx)
        measure_coverage = _build_measure_lyric_coverage(part, full["voice_parts"])
        measure_spans = _build_voice_part_measure_spans(part, full["voice_parts"])
        voice_id_map = _build_voice_part_voice_id_map(full["voice_parts"])
        source_hints = _build_source_candidate_hints(
            score,
            target_part_index=idx,
            voice_parts=full["voice_parts"],
        )
        part_region_indices = _build_part_region_indices(part, full["voice_parts"])
        part_signals.append(
            {
                "part_index": full["part_index"],
                "part_id": full["part_id"],
                "part_name": full["part_name"],
                "multi_voice_part": full["multi_voice_part"],
                "missing_lyric_voice_parts": full["missing_lyric_voice_parts"],
                "lyric_source_candidates": all_sources,
                "measure_lyric_coverage": measure_coverage,
                "voice_part_measure_spans": measure_spans,
                "voice_part_id_to_source_voice_id": voice_id_map,
                "source_candidate_hints": source_hints,
                "part_region_indices": part_region_indices,
                "verse_number": normalized_verse,
            }
        )
        analysis_parts.append(
            {
                "part_index": full["part_index"],
                "part_id": full["part_id"],
                "part_name": full["part_name"],
                "voice_parts": full["voice_parts"],
                "measure_lyric_coverage": measure_coverage,
                "voice_part_measure_spans": measure_spans,
                "voice_part_id_to_source_voice_id": voice_id_map,
                "section_source_candidate_hints": source_hints,
                "part_region_indices": part_region_indices,
            }
        )

    return {
        "analysis_version": "v2",
        "requested_verse_number": normalized_verse,
        "has_multi_voice_parts": any(p["multi_voice_part"] for p in part_signals),
        "has_missing_lyric_voice_parts": any(
            len(p["missing_lyric_voice_parts"]) > 0 for p in part_signals
        ),
        "parts": part_signals,
        "full_score_analysis": {
            "parts": analysis_parts,
            "status_taxonomy": sorted(VOICE_PART_STATUSES),
        },
    }


def _build_part_region_indices(
    part: Dict[str, Any], voice_parts: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    notes = part.get("notes") or []
    by_voice_part_id = {str(vp["voice_part_id"]): str(vp["source_voice_id"]) for vp in voice_parts}
    part_span = _get_measure_span(notes)
    part_start, part_end = part_span

    # Regions where multiple notes start at the same offset for the same source voice.
    chord_measures: set[int] = set()
    grouped: Dict[Tuple[str, int, float], int] = {}
    for note in notes:
        if note.get("is_rest"):
            continue
        voice = _voice_key(note.get("voice"))
        measure = int(note.get("measure_number") or 0)
        if measure <= 0:
            continue
        offset = round(float(note.get("offset_beats") or 0.0), 6)
        key = (voice, measure, offset)
        grouped[key] = grouped.get(key, 0) + 1
    for (_, measure, _), count in grouped.items():
        if count > 1:
            chord_measures.add(measure)

    default_voice_measures = sorted(
        {
            int(note.get("measure_number") or 0)
            for note in notes
            if (not note.get("is_rest"))
            and _voice_key(note.get("voice")) == "_default"
            and int(note.get("measure_number") or 0) > 0
        }
    )

    per_voice_regions: Dict[str, List[Dict[str, Any]]] = {}
    for voice_part_id, source_voice in by_voice_part_id.items():
        active = sorted(
            {
                int(note.get("measure_number") or 0)
                for note in notes
                if (not note.get("is_rest"))
                and _voice_key(note.get("voice")) == source_voice
                and int(note.get("measure_number") or 0) > 0
            }
        )
        active_set = set(active)
        regions: List[Dict[str, Any]] = []
        if part_start > 0 and part_end >= part_start:
            no_music = [m for m in range(part_start, part_end + 1) if m not in active_set]
            for rng in _collapse_measure_ranges(no_music):
                regions.append(
                    {
                        "status": "NO_MUSIC",
                        "start_measure": rng["start"],
                        "end_measure": rng["end"],
                    }
                )
        needs_split = [m for m in active if m in chord_measures]
        for rng in _collapse_measure_ranges(needs_split):
            regions.append(
                {
                    "status": "NEEDS_SPLIT",
                    "start_measure": rng["start"],
                    "end_measure": rng["end"],
                }
            )
        unassigned = [m for m in active if source_voice == "_default" or m in default_voice_measures]
        for rng in _collapse_measure_ranges(sorted(set(unassigned))):
            regions.append(
                {
                    "status": "UNASSIGNED_SOURCE",
                    "start_measure": rng["start"],
                    "end_measure": rng["end"],
                }
            )
        resolved = [m for m in active if m not in set(needs_split) and m not in set(unassigned)]
        for rng in _collapse_measure_ranges(resolved):
            regions.append(
                {
                    "status": "RESOLVED",
                    "start_measure": rng["start"],
                    "end_measure": rng["end"],
                }
            )
        regions.sort(key=lambda row: (int(row["start_measure"]), int(row["end_measure"]), str(row["status"])))
        per_voice_regions[voice_part_id] = regions

    return {
        "part_span": {"start_measure": part_start, "end_measure": part_end},
        "chord_regions": _collapse_measure_ranges(sorted(chord_measures)),
        "default_voice_regions": _collapse_measure_ranges(default_voice_measures),
        "target_resolution_by_voice_part": per_voice_regions,
    }


def _build_voice_part_measure_spans(
    part: Dict[str, Any], voice_parts: Sequence[Dict[str, Any]]
) -> Dict[str, List[Dict[str, int]]]:
    spans: Dict[str, List[Dict[str, int]]] = {}
    notes = part.get("notes") or []
    for vp in voice_parts:
        voice_part_id = str(vp["voice_part_id"])
        voice_id = vp["source_voice_id"]
        selected = _select_part_notes_for_voice(notes, voice_id)
        measures = sorted(
            {
                int(note.get("measure_number") or 0)
                for note in selected
                if int(note.get("measure_number") or 0) > 0
            }
        )
        spans[voice_part_id] = _collapse_measure_ranges(measures)
    return spans


def _build_voice_part_voice_id_map(
    voice_parts: Sequence[Dict[str, Any]]
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for vp in voice_parts:
        mapping[str(vp["voice_part_id"])] = str(vp["source_voice_id"])
    return mapping


def _collapse_measure_ranges(measures: Sequence[int]) -> List[Dict[str, int]]:
    if not measures:
        return []
    ranges: List[Dict[str, int]] = []
    start = measures[0]
    prev = measures[0]
    for measure in measures[1:]:
        if measure == prev + 1:
            prev = measure
            continue
        ranges.append({"start": start, "end": prev})
        start = measure
        prev = measure
    ranges.append({"start": start, "end": prev})
    return ranges


def preprocess_voice_parts(
    score: Dict[str, Any],
    *,
    request: Optional[Dict[str, Any]] = None,
    plan: Optional[Dict[str, Any]] = None,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    voice_part_id: Optional[str] = None,
    allow_lyric_propagation: bool = False,
    source_voice_part_id: Optional[str] = None,
    source_part_index: Optional[int] = None,
    split_shared_note_policy: str = "duplicate_to_all",
    propagation_strategy: str = "strict_onset",
    propagation_policy: str = "fill_missing_only",
    verse_number: Optional[str | int] = "1",
    copy_all_verses: bool = False,
    section_overrides: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Preprocess score voice parts before synthesis.

    This is the primary entrypoint for voice-part preprocessing. It accepts
    either direct args (backward-compatible path) or a `request` payload with
    a normalized plan contract.
    """
    request_payload = request or {}
    if request is not None and not isinstance(request, dict):
        return _action_required(
            "invalid_plan_payload",
            "request must be an object.",
            code="invalid_plan_payload",
        )
    if request is not None:
        if "voice_id" in request_payload:
            return _action_required(
                "deprecated_voice_id_input",
                (
                    "Deprecated request.voice_id is not accepted. "
                    "Use request.plan.targets[].target.voice_part_id."
                ),
                code="deprecated_voice_id_input",
            )
        if "plan" not in request_payload and plan is None:
            return _action_required(
                "preprocessing_plan_required",
                "preprocess_voice_parts requires request.plan as an object.",
                code="preprocessing_plan_required",
            )

    if request_payload:
        if "part_index" in request_payload:
            part_index = int(request_payload.get("part_index", part_index))
        voice_id = request_payload.get("voice_id", voice_id)
        voice_part_id = request_payload.get("voice_part_id", voice_part_id)
        if "allow_lyric_propagation" in request_payload:
            allow_lyric_propagation = bool(
                request_payload.get("allow_lyric_propagation")
            )
        source_voice_part_id = request_payload.get(
            "source_voice_part_id", source_voice_part_id
        )
        if "source_part_index" in request_payload:
            source_part_raw = request_payload.get("source_part_index")
            source_part_index = (
                int(source_part_raw) if source_part_raw is not None else None
            )
        split_shared_note_policy = str(
            request_payload.get("split_shared_note_policy", split_shared_note_policy)
        )
        propagation_strategy = str(
            request_payload.get("propagation_strategy", propagation_strategy)
        )
        propagation_policy = str(
            request_payload.get("propagation_policy", propagation_policy)
        )
        verse_number = request_payload.get("verse_number", verse_number)
        copy_all_verses = bool(request_payload.get("copy_all_verses", copy_all_verses))
        if "section_overrides" in request_payload:
            section_overrides = request_payload.get("section_overrides") or []
        if "plan" in request_payload:
            plan = request_payload.get("plan")

    if plan is not None:
        parsed = parse_voice_part_plan(plan, score=score)
        if not parsed["ok"]:
            return parsed["error"]
        return _execute_preprocess_plan(score, parsed["plan"])

    normalized_voice_part_id = _normalize_voice_part_id(
        voice_part_id=voice_part_id,
        voice_id=voice_id,
    )
    if split_shared_note_policy not in VALID_SPLIT_SHARED_NOTE_POLICIES:
        return _action_required(
            "invalid_plan_enum",
            f"Invalid split_shared_note_policy: {split_shared_note_policy}",
            code="invalid_plan_enum",
        )
    if propagation_strategy not in VALID_PROPAGATION_STRATEGIES:
        return _action_required(
            "invalid_plan_enum",
            f"Invalid propagation_strategy: {propagation_strategy}",
            code="invalid_plan_enum",
        )
    if propagation_policy not in VALID_PROPAGATION_POLICIES:
        return _action_required(
            "invalid_plan_enum",
            f"Invalid propagation_policy: {propagation_policy}",
            code="invalid_plan_enum",
        )
    result = _prepare_score_for_voice_part_legacy(
        score,
        part_index=part_index,
        voice_part_id=normalized_voice_part_id,
        allow_lyric_propagation=allow_lyric_propagation,
        source_voice_part_id=source_voice_part_id,
        source_part_index=source_part_index,
        split_shared_note_policy=split_shared_note_policy,
        propagation_strategy=propagation_strategy,
        propagation_policy=propagation_policy,
        verse_number=verse_number,
        copy_all_verses=copy_all_verses,
        section_overrides=section_overrides,
    )
    if voice_id and not voice_part_id and result.get("status") in {"ready", "action_required"}:
        metadata = result.setdefault("metadata", {})
        metadata["deprecated_inputs"] = ["voice_id"]
        metadata["deprecated_action"] = "deprecated_voice_id_input"
    return result


def prepare_score_for_voice_part(
    score: Dict[str, Any],
    *,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    voice_part_id: Optional[str] = None,
    allow_lyric_propagation: bool = False,
    source_voice_part_id: Optional[str] = None,
    source_part_index: Optional[int] = None,
    split_shared_note_policy: str = "duplicate_to_all",
    propagation_strategy: str = "strict_onset",
    propagation_policy: str = "fill_missing_only",
    verse_number: Optional[str | int] = "1",
    copy_all_verses: bool = False,
    section_overrides: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compatibility wrapper for legacy callers.

    Returns either:
    - {"status": "ready", "score": transformed_score, "part_index": part_index}
    - {"status": "action_required", ...}
    """
    return preprocess_voice_parts(
        score,
        part_index=part_index,
        voice_id=voice_id,
        voice_part_id=voice_part_id,
        allow_lyric_propagation=allow_lyric_propagation,
        source_voice_part_id=source_voice_part_id,
        source_part_index=source_part_index,
        split_shared_note_policy=split_shared_note_policy,
        propagation_strategy=propagation_strategy,
        propagation_policy=propagation_policy,
        verse_number=verse_number,
        copy_all_verses=copy_all_verses,
        section_overrides=section_overrides,
    )


def parse_voice_part_plan(
    plan: Any, *, score: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Validate and normalize AI planning payload for voice-part preprocessing."""
    if not isinstance(plan, dict):
        return _plan_error("invalid_plan_payload", "Plan payload must be an object.")

    raw_targets = plan.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        return _plan_error(
            "invalid_plan_payload",
            "Plan payload must include a non-empty targets list.",
        )

    normalized_targets: List[Dict[str, Any]] = []
    for target_idx, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            return _plan_error(
                "invalid_plan_payload",
                f"targets[{target_idx}] must be an object.",
            )
        raw_target_ref = raw_target.get("target")
        target_ref, error = _normalize_voice_ref(
            raw_target_ref,
            field_name=f"targets[{target_idx}].target",
            allow_voice_id=True,
        )
        if error is not None:
            return {"ok": False, "error": error}

        raw_sections = raw_target.get("sections")
        if raw_sections is not None:
            sections, sections_error = _normalize_timeline_sections(
                raw_sections,
                target_ref=target_ref,
                score=score,
                field_name=f"targets[{target_idx}].sections",
            )
            if sections_error is not None:
                return {"ok": False, "error": sections_error}
            normalized_targets.append(
                {
                    "target": target_ref,
                    "sections": sections,
                    "verse_number": _normalize_verse_number(
                        raw_target.get("verse_number", "1")
                    ),
                    "copy_all_verses": bool(raw_target.get("copy_all_verses", False)),
                    "split_shared_note_policy": str(
                        raw_target.get("split_shared_note_policy", "duplicate_to_all")
                    ).strip(),
                    "confidence": raw_target.get("confidence"),
                    "notes": raw_target.get("notes"),
                }
            )
            continue

        raw_actions = raw_target.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            return _plan_error(
                "invalid_plan_payload",
                (
                    f"targets[{target_idx}] must include either a non-empty sections list "
                    "or a non-empty actions list."
                ),
            )
        normalized_actions: List[Dict[str, Any]] = []
        for action_idx, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, dict):
                return _plan_error(
                    "invalid_plan_payload",
                    f"targets[{target_idx}].actions[{action_idx}] must be an object.",
                )
            action_type = str(raw_action.get("type") or "").strip()
            if action_type not in VALID_PLAN_ACTION_TYPES:
                return _plan_error(
                    "unknown_plan_action_type",
                    (
                        f"Unsupported action type '{action_type}' at "
                        f"targets[{target_idx}].actions[{action_idx}]."
                    ),
                )
            if action_type == "split_voice_part":
                split_policy = str(
                    raw_action.get("split_shared_note_policy", "duplicate_to_all")
                ).strip()
                if split_policy not in VALID_SPLIT_SHARED_NOTE_POLICIES:
                    return _plan_error(
                        "invalid_plan_enum",
                        f"Invalid split_shared_note_policy: {split_policy}",
                    )
                normalized_actions.append(
                    {
                        "type": "split_voice_part",
                        "split_shared_note_policy": split_policy,
                    }
                )
                continue
            if action_type == "duplicate_section_to_all_voice_parts":
                start_measure = raw_action.get("start_measure")
                end_measure = raw_action.get("end_measure")
                if not isinstance(start_measure, int) or not isinstance(end_measure, int):
                    return _plan_error(
                        "invalid_plan_payload",
                        f"targets[{target_idx}].actions[{action_idx}].start_measure/end_measure must be integers.",
                    )
                if start_measure > end_measure:
                    return _plan_error(
                        "invalid_plan_payload",
                        f"targets[{target_idx}].actions[{action_idx}].start_measure cannot exceed end_measure.",
                    )
                source_ref, source_error = _normalize_voice_ref(
                    raw_action.get("source"),
                    field_name=(
                        f"targets[{target_idx}].actions[{action_idx}].source"
                    ),
                    allow_voice_id=False,
                )
                if source_error is not None:
                    return {"ok": False, "error": source_error}
                normalized_actions.append(
                    {
                        "type": "duplicate_section_to_all_voice_parts",
                        "start_measure": int(start_measure),
                        "end_measure": int(end_measure),
                        "source": source_ref,
                    }
                )
                continue

            strategy = str(raw_action.get("strategy", "strict_onset")).strip()
            policy = str(raw_action.get("policy", "fill_missing_only")).strip()
            if strategy not in VALID_PROPAGATION_STRATEGIES:
                return _plan_error(
                    "invalid_plan_enum",
                    f"Invalid propagation strategy: {strategy}",
                )
            if policy not in VALID_PROPAGATION_POLICIES:
                return _plan_error(
                    "invalid_plan_enum",
                    f"Invalid propagation policy: {policy}",
                )
            raw_sources = raw_action.get("source_priority") or []
            if not isinstance(raw_sources, list):
                return _plan_error(
                    "invalid_plan_payload",
                    (
                        f"targets[{target_idx}].actions[{action_idx}].source_priority "
                        "must be a list."
                    ),
                )
            normalized_sources: List[Dict[str, Any]] = []
            for source_idx, raw_source in enumerate(raw_sources):
                source_ref, source_error = _normalize_voice_ref(
                    raw_source,
                    field_name=(
                        f"targets[{target_idx}].actions[{action_idx}]"
                        f".source_priority[{source_idx}]"
                    ),
                    allow_voice_id=False,
                )
                if source_error is not None:
                    return {"ok": False, "error": source_error}
                normalized_sources.append(source_ref)
            overrides, overrides_error = _normalize_section_overrides(
                raw_action.get("section_overrides") or [],
                target_ref=target_ref,
                score=score,
                field_name=f"targets[{target_idx}].actions[{action_idx}].section_overrides",
            )
            if overrides_error is not None:
                return {"ok": False, "error": overrides_error}
            normalized_actions.append(
                {
                    "type": "propagate_lyrics",
                    "verse_number": _normalize_verse_number(
                        raw_action.get("verse_number", "1")
                    ),
                    "copy_all_verses": bool(raw_action.get("copy_all_verses", False)),
                    "source_priority": normalized_sources,
                    "strategy": strategy,
                    "policy": policy,
                    "section_overrides": overrides,
                }
            )

        normalized_targets.append(
            {
                "target": target_ref,
                "actions": normalized_actions,
                "confidence": raw_target.get("confidence"),
                "notes": raw_target.get("notes"),
            }
        )

    return {"ok": True, "plan": {"targets": normalized_targets}}


def validate_voice_part_status(status: str) -> bool:
    return status in VOICE_PART_STATUSES


def _prepare_score_for_voice_part_legacy(
    score: Dict[str, Any],
    *,
    part_index: int = 0,
    voice_part_id: Optional[str] = None,
    allow_lyric_propagation: bool = False,
    source_voice_part_id: Optional[str] = None,
    source_part_index: Optional[int] = None,
    split_shared_note_policy: str = "duplicate_to_all",
    propagation_strategy: str = "strict_onset",
    propagation_policy: str = "fill_missing_only",
    verse_number: Optional[str | int] = "1",
    copy_all_verses: bool = False,
    section_overrides: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    parts = score.get("parts") or []
    if not parts:
        return _action_required(
            "missing_score_parts", "Score has no parts to synthesize."
        )
    if part_index < 0 or part_index >= len(parts):
        return _action_required(
            "invalid_part_index",
            f"part_index {part_index} is out of range.",
            part_index=part_index,
        )

    source_score = deepcopy(score)
    part = source_score["parts"][part_index]
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis["voice_parts"]
    if not voice_parts:
        return _action_required(
            "missing_voice_parts",
            "No voiced note events were found in the selected part.",
            part_index=part_index,
        )

    target = _resolve_target_voice_part(
        voice_parts,
        voice_id=None,
        voice_part_id=voice_part_id,
    )
    if target is None:
        return _action_required(
            "target_voice_part_not_found",
            "Requested voice part was not found in the selected part.",
            part_index=part_index,
            voice_part_options=[vp["voice_part_id"] for vp in voice_parts],
        )

    target_voice = target["source_voice_id"]
    voice_priority = {
        str(vp["source_voice_id"]): idx for idx, vp in enumerate(voice_parts)
    }
    selected_notes = _select_part_notes_for_voice(
        part.get("notes") or [],
        target_voice,
        split_shared_note_policy=split_shared_note_policy,
        voice_priority=voice_priority,
    )
    selected_notes = _enforce_non_overlapping_sequence(selected_notes)
    transformed_part = dict(part)
    transformed_part["_source_part_index"] = part_index
    transformed_part["_source_part_id"] = part.get("part_id")
    transformed_part["_source_part_name"] = part.get("part_name")
    transformed_part["notes"] = selected_notes
    transformed_part["part_name"] = target["voice_part_id"]

    if _part_has_any_lyric(selected_notes) and not allow_lyric_propagation:
        source_score["parts"][part_index] = transformed_part
        return _finalize_transform_result(
            source_score,
            part_index=part_index,
            target_voice_part_id=target["voice_part_id"],
            source_voice_part_id=None,
            source_part_index=part_index,
            transformed_part=transformed_part,
            propagated=False,
            status="ready",
            validation={
                "lyric_coverage_ratio": 1.0,
                "source_alignment_ratio": 1.0,
                "missing_lyric_sung_note_count": 0,
                "lyric_exempt_note_count": 0,
                "unresolved_measures": [],
            },
            source_musicxml_path=_resolve_source_musicxml_path(score),
            target_source_voice_id=target_voice,
        )

    same_part_source_options = [
        _make_source_option(part_index, vp["voice_part_id"])
        for vp in voice_parts
        if vp["source_voice_id"] != target_voice and vp["lyric_note_count"] > 0
    ]
    global_source_options = _collect_global_lyric_source_options(
        source_score, exclude_part_index=part_index
    )
    seen_sources = set()
    source_options: List[Dict[str, Any]] = []
    for option in same_part_source_options + global_source_options:
        key = (int(option["source_part_index"]), str(option["source_voice_part_id"]))
        if key in seen_sources:
            continue
        seen_sources.add(key)
        source_options.append(option)
    if not source_options:
        return _action_required(
            "missing_lyrics_no_source",
            (
                f"Voice part '{target['voice_part_id']}' has no lyrics and no lyric source "
                "voice part is available in this score."
            ),
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=[],
        )

    if not allow_lyric_propagation:
        return _action_required(
            "confirm_lyric_propagation",
            (
                f"Voice part '{target['voice_part_id']}' has no lyrics. "
                "Choose a source voice part and explicitly confirm lyric propagation."
            ),
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=source_options,
        )

    if source_part_index is None or not source_voice_part_id:
        return _action_required(
            "select_source_voice_part",
            (
                "Select a source lyric identifier with both source_part_index "
                "and source_voice_part_id."
            ),
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=source_options,
        )

    source_meta: Optional[Dict[str, Any]] = None
    source_notes: Optional[List[Dict[str, Any]]] = None
    source_part_used = source_part_index
    option_match = _match_source_option(
        source_options,
        source_part_index=source_part_index,
        source_voice_part_id=source_voice_part_id,
    )
    if option_match is None:
        return _action_required(
            "invalid_source_voice_part",
            "Selected source lyric identifier is invalid.",
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=source_options,
        )

    if source_part_index < 0 or source_part_index >= len(source_score["parts"]):
        return _action_required(
            "invalid_source_part_index",
            f"source_part_index {source_part_index} is out of range.",
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=source_options,
        )
    source_part = source_score["parts"][source_part_index]
    source_analysis = _analyze_part_voice_parts(source_part, source_part_index)
    source_meta = _find_voice_part_by_id(source_analysis["voice_parts"], source_voice_part_id)
    if source_meta is not None and source_meta["lyric_note_count"] > 0:
        source_notes = _select_part_notes_for_voice(
            source_part.get("notes") or [], source_meta["source_voice_id"]
        )

    if source_meta is None or source_notes is None:
        return _action_required(
            "invalid_source_voice_part",
            "Selected source voice part is invalid for lyric propagation.",
            part_index=part_index,
            target_voice_part=target["voice_part_id"],
            source_voice_part_options=source_options,
        )

    propagated_notes, _ = _propagate_lyrics(
        target_notes=selected_notes,
        source_notes=source_notes,
        strategy=propagation_strategy,
        policy=propagation_policy,
        verse_number=_normalize_verse_number(verse_number),
        copy_all_verses=copy_all_verses,
        measure_range=None,
    )
    overrides = section_overrides or []
    for override in overrides:
        override_source = override.get("source") or {}
        override_part_index = override_source.get("part_index")
        override_voice_part_id = override_source.get("voice_part_id")
        start_measure = override.get("start_measure")
        end_measure = override.get("end_measure")
        if not isinstance(override_part_index, int) or not isinstance(
            override_voice_part_id, str
        ):
            continue
        if not isinstance(start_measure, int) or not isinstance(end_measure, int):
            continue
        override_source_notes = _resolve_source_notes(
            source_score,
            source_part_index=override_part_index,
            source_voice_part_id=override_voice_part_id,
        )
        if override_source_notes is None:
            continue
        propagated_notes, _ = _propagate_lyrics(
            target_notes=propagated_notes,
            source_notes=override_source_notes,
            strategy=str(override.get("strategy") or propagation_strategy),
            policy=str(override.get("policy") or propagation_policy),
            verse_number=_normalize_verse_number(verse_number),
            copy_all_verses=copy_all_verses,
            measure_range=(
                start_measure,
                end_measure,
            ),
        )
    propagated_notes = _enforce_non_overlapping_sequence(propagated_notes)

    validation = _validate_transformed_notes(
        transformed_notes=propagated_notes,
        source_notes=source_notes,
    )
    status = "ready"
    warnings: List[Dict[str, Any]] = []
    if validation["missing_lyric_sung_note_count"] > 0:
        if validation["lyric_coverage_ratio"] >= 0.90:
            status = "ready_with_warnings"
            warnings.append(
                {
                    "code": "partial_lyric_coverage",
                    "missing_note_count": validation["missing_lyric_sung_note_count"],
                    "lyric_coverage_ratio": validation["lyric_coverage_ratio"],
                    "unresolved_measures": validation["unresolved_measures"][:10],
                }
            )
        else:
            return _action_required(
                "validation_failed_needs_review",
                "Lyric propagation did not meet minimum coverage.",
                part_index=part_index,
                target_voice_part=target["voice_part_id"],
                validation=validation,
            )
    transformed_part["notes"] = propagated_notes
    source_score["parts"][part_index] = transformed_part
    return _finalize_transform_result(
        source_score,
        part_index=part_index,
        target_voice_part_id=target["voice_part_id"],
        source_voice_part_id=source_meta["voice_part_id"],
        source_part_index=source_part_used,
        transformed_part=transformed_part,
        propagated=True,
        status=status,
        warnings=warnings,
        validation=validation,
        source_musicxml_path=_resolve_source_musicxml_path(score),
        target_source_voice_id=target_voice,
    )


def _analyze_part_voice_parts(part: Dict[str, Any], part_index: int) -> Dict[str, Any]:
    notes = part.get("notes") or []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for note in notes:
        if note.get("is_rest"):
            continue
        key = _voice_key(note.get("voice"))
        buckets.setdefault(key, []).append(note)

    voice_parts: List[Dict[str, Any]] = []
    part_name = str(part.get("part_name") or "").upper()
    ranked = sorted(
        buckets.items(),
        key=lambda item: (-_avg_pitch(item[1]), item[0]),
    )
    assigned_names = _assign_voice_part_names(part_name, [voice for voice, _ in ranked])
    for idx, (voice, voice_notes) in enumerate(ranked):
        lyric_count = sum(1 for note in voice_notes if _has_lyric(note))
        voice_parts.append(
            {
                "source_voice_id": voice,
                "voice_part_id": assigned_names[idx],
                "note_count": len(voice_notes),
                "lyric_note_count": lyric_count,
                "missing_lyrics": lyric_count == 0,
                "avg_pitch_midi": round(_avg_pitch(voice_notes), 3),
            }
        )

    missing = [vp["voice_part_id"] for vp in voice_parts if vp["missing_lyrics"]]
    source_options = [vp["voice_part_id"] for vp in voice_parts if vp["lyric_note_count"] > 0]
    return {
        "part_index": part_index,
        "part_id": part.get("part_id"),
        "part_name": part.get("part_name"),
        "multi_voice_part": len(voice_parts) > 1,
        "voice_part_count": len(voice_parts),
        "missing_lyric_voice_parts": missing,
        "missing_lyric_voice_part_count": len(missing),
        "lyric_source_candidates": source_options,
        "voice_parts": voice_parts,
    }


def _assign_voice_part_names(part_name_upper: str, ordered_voice_ids: Sequence[str]) -> List[str]:
    count = len(ordered_voice_ids)
    if count == 2 and "SOPRANO" in part_name_upper and "ALTO" in part_name_upper:
        return ["soprano", "alto"]
    if count == 2 and "TENOR" in part_name_upper and "BASS" in part_name_upper:
        return ["tenor", "bass"]
    return [f"voice part {idx + 1}" for idx in range(count)]


def _resolve_target_voice_part(
    voice_parts: Sequence[Dict[str, Any]],
    *,
    voice_id: Optional[str],
    voice_part_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if voice_part_id:
        return _find_voice_part_by_id(voice_parts, voice_part_id)
    if voice_id:
        voice_text = str(voice_id).strip().lower()
        direct = next(
            (
                vp
                for vp in voice_parts
                if str(vp["source_voice_id"]).strip().lower() == voice_text
            ),
            None,
        )
        if direct is not None:
            return direct
        named = _find_voice_part_by_id(voice_parts, voice_text)
        if named is not None:
            return named
    return voice_parts[0] if voice_parts else None


def _execute_preprocess_plan(score: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    targets = plan.get("targets") or []
    if not targets:
        return _action_required(
            "invalid_plan_payload",
            "Plan must contain at least one target.",
            code="invalid_plan_payload",
        )

    lint_result = _run_preflight_plan_lint(score, plan)
    if not lint_result.get("ok"):
        return _action_required(
            "plan_lint_failed",
            "Preflight plan lint failed. Adjust plan sections before execution.",
            code="plan_lint_failed",
            lint_findings=lint_result.get("findings", []),
        )

    # Multi-target plans execute sequentially and carry forward transformed score.
    original_score = deepcopy(score)
    working_score = deepcopy(score)
    last_result: Optional[Dict[str, Any]] = None
    original_parts = deepcopy(original_score.get("parts") or [])
    explicit_targets_by_part: Dict[int, set[str]] = {}
    for target_entry in targets:
        target_ref = target_entry.get("target") or {}
        part_index = target_ref.get("part_index")
        voice_part_id = target_ref.get("voice_part_id")
        if isinstance(part_index, int) and isinstance(voice_part_id, str) and voice_part_id.strip():
            explicit_targets_by_part.setdefault(part_index, set()).add(voice_part_id.strip())

    for target_entry in targets:
        target_input_score = deepcopy(working_score)
        current_parts = target_input_score.get("parts") or []
        extra_parts = (
            [deepcopy(part) for part in current_parts[len(original_parts) :]]
            if len(current_parts) > len(original_parts)
            else []
        )
        # Keep canonical source parts stable across targets so later targets can
        # still reference original lyric/melody sources from the same input score.
        target_input_score["parts"] = [deepcopy(part) for part in original_parts] + extra_parts
        result = _execute_single_preprocess_target(target_input_score, target_entry)
        if result.get("status") not in {"ready", "ready_with_warnings"}:
            return result
        target_ref = target_entry["target"]
        working_score = result.get("score", working_score)
        last_result = result

        # Also generate sibling derived parts from the same part (same staff/clef
        # scope in current model) so downstream tools can reason across sibling
        # lines without extra user roundtrips.
        sibling_result = _generate_same_part_voice_part_derivations(
            source_score=original_score,
            target_score=working_score,
            part_index=int(target_ref["part_index"]),
            primary_voice_part_id=str(target_ref["voice_part_id"]),
            initial_source_musicxml_path=result.get("modified_musicxml_path"),
            explicit_target_voice_part_ids=explicit_targets_by_part.get(
                int(target_ref["part_index"]), set()
            ),
        )
        if sibling_result is not None:
            if sibling_result.get("status") not in {"ready", "ready_with_warnings"}:
                return sibling_result
            working_score = sibling_result.get("score", working_score)
            last_result["score"] = working_score
            if sibling_result.get("modified_musicxml_path"):
                last_result["modified_musicxml_path"] = sibling_result.get(
                    "modified_musicxml_path"
                )
            metadata = last_result.setdefault("metadata", {})
            metadata["generated_same_part_voice_parts"] = True

    if last_result is None:
        return _action_required(
            "invalid_plan_payload",
            "Plan must contain at least one target.",
            code="invalid_plan_payload",
        )

    return last_result


def _run_preflight_plan_lint(score: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    targets = plan.get("targets") or []
    findings.extend(_lint_same_part_target_completeness(score, targets))
    by_part_claims: Dict[int, set[int]] = {}
    by_part_has_timeline_targets: Dict[int, bool] = {}

    for target_idx, target_entry in enumerate(targets):
        target = target_entry.get("target") or {}
        part_index = int(target.get("part_index", -1))
        target_voice_part_id = str(target.get("voice_part_id") or "")
        sections = target_entry.get("sections") or []
        
        # Guard: Complex scores (chords or mixed regions) require sections.
        if not sections and 0 <= part_index < len(score.get("parts", [])):
            part = score["parts"][part_index]
            analysis = _analyze_part_voice_parts(part, part_index)
            regions = _build_part_region_indices(part, analysis["voice_parts"])
            
            # Aggregate statuses across all regions in this part to detect complexity.
            all_statuses = set()
            for vp_regions_list in regions.get("target_resolution_by_voice_part", {}).values():
                for r in vp_regions_list:
                    all_statuses.add(r["status"])
            
            has_chords = len(regions.get("chord_regions", [])) > 0
            has_split_need = "NEEDS_SPLIT" in all_statuses
            has_unassigned = "UNASSIGNED_SOURCE" in all_statuses or len(regions.get("default_voice_regions", [])) > 0
            
            # Rule 1: Chords or explicit SPLIT needs require sections.
            if has_chords or has_split_need:
                findings.append(
                    _lint_finding(
                        "plan_requires_sections",
                        target_index=target_idx,
                        part_index=part_index,
                        target_voice_part_id=target_voice_part_id,
                        details={
                            "has_chords": has_chords,
                            "has_split_need": has_split_need,
                            "reason": "complexity_requires_sections",
                        },
                        failing_attributes={
                            "has_chords": has_chords,
                            "has_split_need": has_split_need,
                        },
                    )
                )
            # Rule 2: Mixed region qualities (some resolved, some unassigned) require sections to explicitly handle transitions.
            elif "RESOLVED" in all_statuses and has_unassigned:
                findings.append(
                    _lint_finding(
                        "mixed_region_requires_sections",
                        target_index=target_idx,
                        part_index=part_index,
                        target_voice_part_id=target_voice_part_id,
                        details={
                            "region_statuses": sorted(all_statuses),
                            "has_unassigned": has_unassigned,
                            "reason": "mixed_region_qualities_require_sections",
                        },
                        failing_attributes={
                            "region_statuses": sorted(all_statuses),
                            "has_unassigned": has_unassigned,
                        },
                    )
                )

        if not sections:
            continue
        by_part_has_timeline_targets[part_index] = True

        contiguous_error = _lint_sections_contiguous_no_gaps(sections)
        if contiguous_error is not None:
            findings.append(
                _lint_finding(
                    "section_timeline_contiguous_no_gaps",
                    target_index=target_idx,
                    part_index=part_index,
                    target_voice_part_id=target_voice_part_id,
                    details=contiguous_error,
                    failing_attributes=contiguous_error,
                )
            )

        native_sung_measures = _native_sung_measures_for_target(
            score,
            part_index=part_index,
            target_voice_part_id=target_voice_part_id,
        )
        for section in sections:
            start = int(section["start_measure"])
            end = int(section["end_measure"])
            if section.get("mode") == "derive":
                decision_type = str(section.get("decision_type") or "EXTRACT_FROM_VOICE")
                method = str(section.get("method") or "trivial")
                melody_source = section.get("melody_source") or {}
                lyric_source = section.get("lyric_source") or {}
                melody_source_part = melody_source.get("part_index")
                lyric_source_part = lyric_source.get("part_index")
                if (
                    decision_type == "SPLIT_CHORDS_SELECT_NOTES"
                    and method == "trivial"
                    and isinstance(melody_source_part, int)
                    and isinstance(melody_source.get("voice_part_id"), str)
                ):
                    target_lane_count = _count_trivial_split_target_lanes_for_section(
                        targets,
                        part_index=part_index,
                        start_measure=start,
                        end_measure=end,
                        source_part_index=melody_source_part,
                        source_voice_part_id=str(melody_source["voice_part_id"]),
                    )
                    mismatch = _trivial_method_chord_size_mismatch_details(
                        score,
                        source_part_index=melody_source_part,
                        source_voice_part_id=str(melody_source["voice_part_id"]),
                        start_measure=start,
                        end_measure=end,
                        target_lane_count=target_lane_count,
                    )
                    if mismatch["mismatch_count"] > 0:
                        findings.append(
                            _lint_finding(
                                "trivial_method_requires_equal_chord_voice_part_count",
                                target_index=target_idx,
                                part_index=part_index,
                                target_voice_part_id=target_voice_part_id,
                                section={"start_measure": start, "end_measure": end},
                                details=mismatch,
                                failing_attributes={
                                    "method": method,
                                    "decision_type": decision_type,
                                    "source_part_index": melody_source_part,
                                    "source_voice_part_id": str(melody_source["voice_part_id"]),
                                    "target_lane_count": target_lane_count,
                                    "expected_simultaneous_note_count": mismatch.get("expected_simultaneous_note_count"),
                                },
                            )
                        )
                if (
                    isinstance(melody_source_part, int)
                    and melody_source_part != part_index
                    and _part_has_sung_material_in_range(
                        score, part_index=part_index, start_measure=start, end_measure=end
                    )
                ):
                    findings.append(
                        _lint_finding(
                            "cross_staff_melody_source_when_local_available",
                            target_index=target_idx,
                            part_index=part_index,
                            target_voice_part_id=target_voice_part_id,
                            section={"start_measure": start, "end_measure": end},
                            source_part_index=melody_source_part,
                            failing_attributes={
                                "source_part_index": melody_source_part,
                                "target_part_index": part_index,
                            },
                        )
                    )
                if (
                    isinstance(lyric_source_part, int)
                    and lyric_source_part != part_index
                    and _part_has_word_lyric_in_range(
                        score, part_index=part_index, start_measure=start, end_measure=end
                    )
                ):
                    findings.append(
                        _lint_finding(
                            "cross_staff_lyric_source_when_local_available",
                            target_index=target_idx,
                            part_index=part_index,
                            target_voice_part_id=target_voice_part_id,
                            section={"start_measure": start, "end_measure": end},
                            source_part_index=lyric_source_part,
                            failing_attributes={
                                "source_part_index": lyric_source_part,
                                "target_part_index": part_index,
                            },
                        )
                    )
                if (
                    isinstance(lyric_source_part, int)
                    and lyric_source_part == part_index
                    and isinstance(lyric_source.get("voice_part_id"), str)
                ):
                    chosen_voice_part_id = str(lyric_source["voice_part_id"]).strip()
                    chosen_stats = _lyric_stats_for_voice_part_range(
                        score=score,
                        part_index=part_index,
                        voice_part_id=chosen_voice_part_id,
                        start_measure=start,
                        end_measure=end,
                    )
                    if (
                        chosen_stats["word_lyric_note_count"] == 0
                        and chosen_stats["extension_lyric_note_count"] > 0
                    ):
                        alternative = _best_same_part_word_lyric_source_for_range(
                            score=score,
                            part_index=part_index,
                            start_measure=start,
                            end_measure=end,
                            exclude_voice_part_id=chosen_voice_part_id,
                        )
                        if alternative is not None and alternative["stats"]["word_lyric_note_count"] > 0:
                            findings.append(
                                _lint_finding(
                                    "extension_only_lyric_source_with_word_alternative",
                                    target_index=target_idx,
                                    part_index=part_index,
                                    target_voice_part_id=target_voice_part_id,
                                    section={"start_measure": start, "end_measure": end},
                                    selected_lyric_source={
                                        "part_index": part_index,
                                        "voice_part_id": chosen_voice_part_id,
                                        "stats": chosen_stats,
                                    },
                                    suggested_lyric_source={
                                        "part_index": part_index,
                                        "voice_part_id": alternative["voice_part_id"],
                                        "stats": alternative["stats"],
                                    },
                                    failing_attributes={
                                        "selected_voice_part_id": chosen_voice_part_id,
                                        "selected_word_lyric_note_count": chosen_stats.get("word_lyric_note_count"),
                                        "selected_extension_lyric_note_count": chosen_stats.get("extension_lyric_note_count"),
                                        "suggested_voice_part_id": alternative["voice_part_id"],
                                        "suggested_word_lyric_note_count": alternative["stats"].get("word_lyric_note_count"),
                                    },
                                )
                            )
                    if chosen_stats["lyric_note_count"] == 0:
                        alternative = _best_same_part_word_lyric_source_for_range(
                            score=score,
                            part_index=part_index,
                            start_measure=start,
                            end_measure=end,
                            exclude_voice_part_id=chosen_voice_part_id,
                        )
                        if alternative is not None and alternative["stats"]["word_lyric_note_count"] > 0:
                            findings.append(
                                _lint_finding(
                                    "empty_lyric_source_with_word_alternative",
                                    target_index=target_idx,
                                    part_index=part_index,
                                    target_voice_part_id=target_voice_part_id,
                                    section={"start_measure": start, "end_measure": end},
                                    selected_lyric_source={
                                        "part_index": part_index,
                                        "voice_part_id": chosen_voice_part_id,
                                        "stats": chosen_stats,
                                    },
                                    suggested_lyric_source={
                                        "part_index": part_index,
                                        "voice_part_id": alternative["voice_part_id"],
                                        "stats": alternative["stats"],
                                    },
                                    failing_attributes={
                                        "selected_voice_part_id": chosen_voice_part_id,
                                        "selected_lyric_note_count": chosen_stats.get("lyric_note_count"),
                                        "suggested_voice_part_id": alternative["voice_part_id"],
                                        "suggested_word_lyric_note_count": alternative["stats"].get("word_lyric_note_count"),
                                    },
                                )
                            )
                    alternative = _best_same_part_word_lyric_source_for_range(
                        score=score,
                        part_index=part_index,
                        start_measure=start,
                        end_measure=end,
                        exclude_voice_part_id=chosen_voice_part_id,
                    )
                    if (
                        alternative is not None
                        and _is_weak_word_lyric_source_with_better_alternative(
                            selected_stats=chosen_stats,
                            alternative_stats=alternative["stats"],
                        )
                    ):
                        findings.append(
                            _lint_finding(
                                "weak_lyric_source_with_better_alternative",
                                target_index=target_idx,
                                part_index=part_index,
                                target_voice_part_id=target_voice_part_id,
                                section={"start_measure": start, "end_measure": end},
                                selected_lyric_source={
                                    "part_index": part_index,
                                    "voice_part_id": chosen_voice_part_id,
                                    "stats": chosen_stats,
                                },
                                suggested_lyric_source={
                                    "part_index": part_index,
                                    "voice_part_id": alternative["voice_part_id"],
                                    "stats": alternative["stats"],
                                },
                                failing_attributes={
                                    "selected_voice_part_id": chosen_voice_part_id,
                                    "selected_word_lyric_note_count": chosen_stats.get("word_lyric_note_count"),
                                    "selected_word_lyric_coverage_ratio": chosen_stats.get("word_lyric_coverage_ratio"),
                                    "suggested_voice_part_id": alternative["voice_part_id"],
                                    "suggested_word_lyric_note_count": alternative["stats"].get("word_lyric_note_count"),
                                    "suggested_word_lyric_coverage_ratio": alternative["stats"].get("word_lyric_coverage_ratio"),
                                },
                            )
                        )
                has_lyric_source = isinstance(lyric_source_part, int)
                has_melody_source = isinstance(melody_source_part, int)
                if has_lyric_source and not has_melody_source:
                    target_measures = set(range(start, end + 1))
                    if not (target_measures & native_sung_measures):
                        findings.append(
                        _lint_finding(
                            "lyric_source_without_target_notes",
                            target_index=target_idx,
                            part_index=part_index,
                            target_voice_part_id=target_voice_part_id,
                            section={"start_measure": start, "end_measure": end},
                            details={
                                "reason": "lyric_source_without_target_notes",
                                "has_lyric_source": has_lyric_source,
                                "has_melody_source": has_melody_source,
                                "native_sung_measure_overlap": False,
                            },
                            failing_attributes={
                                "has_lyric_source": has_lyric_source,
                                "has_melody_source": has_melody_source,
                                "native_sung_measure_overlap": False,
                            },
                            )
                        )
            if section.get("mode") != "derive":
                rest_hit = [m for m in range(start, end + 1) if m in native_sung_measures]
                if rest_hit:
                    findings.append(
                        _lint_finding(
                            "no_rest_when_target_has_native_notes",
                            target_index=target_idx,
                            part_index=part_index,
                            target_voice_part_id=target_voice_part_id,
                            section={"start_measure": start, "end_measure": end},
                            overlap_ranges=_collapse_measure_ranges(rest_hit),
                            failing_attributes={
                                "mode": section.get("mode"),
                                "overlap_measure_count": len(rest_hit),
                            },
                        )
                    )
            else:
                claim_set = by_part_claims.setdefault(part_index, set())
                for measure in range(start, end + 1):
                    claim_set.add(measure)

    # Group-level claim coverage for each part with timeline targets.
    for part_index in sorted(by_part_has_timeline_targets.keys()):
        source_measures = _part_sung_measures(score, part_index=part_index)
        claimed = by_part_claims.get(part_index, set())
        missing = sorted(m for m in source_measures if m not in claimed)
        if missing:
            findings.append(
                _lint_finding(
                    "same_clef_claim_coverage",
                    part_index=part_index,
                    missing_ranges=_collapse_measure_ranges(missing),
                    failing_attributes={
                        "missing_measure_count": len(missing),
                    },
                )
            )

    return {"ok": len(findings) == 0, "findings": findings}


def _lint_same_part_target_completeness(
    score: Dict[str, Any], targets: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    parts = score.get("parts") or []
    timeline_targets_by_part: Dict[int, set[str]] = {}
    for target_entry in targets:
        sections = target_entry.get("sections") or []
        if not sections:
            continue
        target = target_entry.get("target") or {}
        part_index = int(target.get("part_index", -1))
        voice_part_id = str(target.get("voice_part_id") or "")
        if part_index < 0 or not voice_part_id:
            continue
        timeline_targets_by_part.setdefault(part_index, set()).add(voice_part_id)

    for part_index, actual_targets in sorted(timeline_targets_by_part.items()):
        if part_index < 0 or part_index >= len(parts):
            continue
        analysis = _analyze_part_voice_parts(parts[part_index], part_index)
        expected_targets = {
            str(vp.get("voice_part_id") or "")
            for vp in (analysis.get("voice_parts") or [])
            if str(vp.get("source_voice_id") or "") != "_default"
        }
        expected_targets = {vp for vp in expected_targets if vp}
        # Guard is meaningful only when there are sibling split lines.
        if len(expected_targets) <= 1:
            continue
        missing = sorted(expected_targets - actual_targets)
        if missing:
            findings.append(
                _lint_finding(
                    "same_part_target_completeness",
                    part_index=part_index,
                    expected_voice_part_ids=sorted(expected_targets),
                    actual_voice_part_ids=sorted(actual_targets),
                    missing_voice_part_ids=missing,
                    failing_attributes={
                        "expected_voice_part_ids": sorted(expected_targets),
                        "actual_voice_part_ids": sorted(actual_targets),
                    },
                )
            )
    return findings


def _lint_sections_contiguous_no_gaps(
    sections: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not sections:
        return None
    prev_end: Optional[int] = None
    for idx, section in enumerate(sections):
        start = int(section["start_measure"])
        end = int(section["end_measure"])
        if start > end:
            return {
                "issue": "invalid_range",
                "section_index": idx,
                "start_measure": start,
                "end_measure": end,
            }
        if prev_end is not None and start != prev_end + 1:
            return {
                "issue": "gap_or_overlap",
                "section_index": idx,
                "expected_start_measure": prev_end + 1,
                "actual_start_measure": start,
                "previous_end_measure": prev_end,
            }
        prev_end = end
    return None


def _trivial_method_chord_size_mismatch_details(
    score: Dict[str, Any],
    *,
    source_part_index: int,
    source_voice_part_id: str,
    start_measure: int,
    end_measure: int,
    target_lane_count: int,
) -> Dict[str, Any]:
    parts = score.get("parts") or []
    if source_part_index < 0 or source_part_index >= len(parts):
        return {
            "reason": "source_part_out_of_range",
            "source_part_index": source_part_index,
            "source_voice_part_id": source_voice_part_id,
            "expected_simultaneous_note_count": 0,
            "target_lane_count": target_lane_count,
            "mismatch_count": 0,
            "mismatch_examples": [],
        }
    source_part = parts[source_part_index]
    source_notes = _resolve_source_notes_allow_lyricless(
        score,
        source_part_index=source_part_index,
        source_voice_part_id=source_voice_part_id,
    )
    if source_notes is None:
        return {
            "reason": "source_voice_part_not_found",
            "source_part_index": source_part_index,
            "source_voice_part_id": source_voice_part_id,
            "expected_simultaneous_note_count": 0,
            "target_lane_count": target_lane_count,
            "mismatch_count": 0,
            "mismatch_examples": [],
        }

    grouped: Dict[Tuple[int, float], int] = {}
    for note in source_notes:
        if note.get("is_rest") or not _note_in_measure_range(note, (start_measure, end_measure)):
            continue
        measure = int(note.get("measure_number") or 0)
        offset = round(float(note.get("offset_beats") or 0.0), 6)
        key = (measure, offset)
        grouped[key] = grouped.get(key, 0) + 1

    expected_simultaneous_note_count = max(grouped.values(), default=0)
    mismatch_examples: List[Dict[str, Any]] = []
    if expected_simultaneous_note_count > 1 and target_lane_count != expected_simultaneous_note_count:
        for (measure, offset), chord_note_count in sorted(grouped.items()):
            if chord_note_count == expected_simultaneous_note_count:
                mismatch_examples.append(
                    {
                        "measure_number": measure,
                        "offset_beats": offset,
                        "chord_note_count": chord_note_count,
                        "expected_simultaneous_note_count": expected_simultaneous_note_count,
                        "target_lane_count": target_lane_count,
                    }
                )

    mismatch_measures = sorted({int(item["measure_number"]) for item in mismatch_examples})
    return {
        "reason": "trivial_method_section_simultaneous_note_mismatch",
        "source_part_index": source_part_index,
        "source_voice_part_id": source_voice_part_id,
        "expected_simultaneous_note_count": expected_simultaneous_note_count,
        "target_lane_count": target_lane_count,
        "message": (
            "Trivial chord splitting requires the target lane count to match the "
            "maximum simultaneous note count in the source section."
        ),
        "mismatch_count": 1 if mismatch_examples else 0,
        "mismatch_ranges": _collapse_measure_ranges(mismatch_measures),
        "mismatch_examples": mismatch_examples[:8],
    }


def _count_trivial_split_target_lanes_for_section(
    targets: Sequence[Dict[str, Any]],
    *,
    part_index: int,
    start_measure: int,
    end_measure: int,
    source_part_index: int,
    source_voice_part_id: str,
) -> int:
    lane_ids: set[str] = set()
    for target_entry in targets:
        target = target_entry.get("target") or {}
        if int(target.get("part_index", -1)) != part_index:
            continue
        target_voice_part_id = str(target.get("voice_part_id") or "").strip()
        if not target_voice_part_id:
            continue
        for section in target_entry.get("sections") or []:
            if section.get("mode") != "derive":
                continue
            if str(section.get("decision_type") or "EXTRACT_FROM_VOICE") != "SPLIT_CHORDS_SELECT_NOTES":
                continue
            if int(section.get("start_measure", -1)) != start_measure:
                continue
            if int(section.get("end_measure", -1)) != end_measure:
                continue
            melody_source = section.get("melody_source") or {}
            if int(melody_source.get("part_index", -1)) != source_part_index:
                continue
            if str(melody_source.get("voice_part_id") or "").strip() != source_voice_part_id:
                continue
            lane_ids.add(target_voice_part_id)
            break
    return len(lane_ids)


def _native_sung_measures_for_target(
    score: Dict[str, Any], *, part_index: int, target_voice_part_id: str
) -> set[int]:
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return set()
    part = parts[part_index]
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis.get("voice_parts") or []
    target = _find_voice_part_by_id(voice_parts, target_voice_part_id)
    if target is None:
        return set()
    source_voice = str(target.get("source_voice_id") or "")
    selected = _select_part_notes_for_voice(part.get("notes") or [], source_voice)
    return {
        int(note.get("measure_number") or 0)
        for note in selected
        if not note.get("is_rest") and int(note.get("measure_number") or 0) > 0
    }


def _part_sung_measures(score: Dict[str, Any], *, part_index: int) -> set[int]:
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return set()
    notes = parts[part_index].get("notes") or []
    return {
        int(note.get("measure_number") or 0)
        for note in notes
        if not note.get("is_rest") and int(note.get("measure_number") or 0) > 0
    }


def _part_has_sung_material_in_range(
    score: Dict[str, Any], *, part_index: int, start_measure: int, end_measure: int
) -> bool:
    measures = _part_sung_measures(score, part_index=part_index)
    return any(start_measure <= m <= end_measure for m in measures)


def _part_has_word_lyric_in_range(
    score: Dict[str, Any], *, part_index: int, start_measure: int, end_measure: int
) -> bool:
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return False
    notes = parts[part_index].get("notes") or []
    for note in notes:
        if note.get("is_rest"):
            continue
        measure = int(note.get("measure_number") or 0)
        if measure < start_measure or measure > end_measure:
            continue
        lyric = str(note.get("lyric") or "").strip()
        if lyric and not lyric.startswith("+"):
            return True
    return False


def _execute_single_preprocess_target(
    score: Dict[str, Any], target_entry: Dict[str, Any]
) -> Dict[str, Any]:
    target_ref = target_entry["target"]
    sections = target_entry.get("sections") or []
    if sections:
        return _execute_timeline_sections(score, target_entry)
    actions = target_entry.get("actions") or []
    split_action = next((action for action in actions if action["type"] == "split_voice_part"), None)
    duplicate_actions = [
        action for action in actions if action["type"] == "duplicate_section_to_all_voice_parts"
    ]
    propagate_action = next(
        (action for action in actions if action["type"] == "propagate_lyrics"),
        None,
    )
    if split_action is None and propagate_action is None and not duplicate_actions:
        return _action_required(
            "invalid_plan_payload",
            "Plan target requires split_voice_part and/or propagate_lyrics action.",
            code="invalid_plan_payload",
        )

    if duplicate_actions:
        for duplicate_action in duplicate_actions:
            duplicate_result = _duplicate_section_to_all_voice_parts(
                score,
                target_part_index=target_ref["part_index"],
                source_part_index=duplicate_action["source"]["part_index"],
                source_voice_part_id=duplicate_action["source"]["voice_part_id"],
                start_measure=duplicate_action["start_measure"],
                end_measure=duplicate_action["end_measure"],
            )
            if duplicate_result is not None:
                return duplicate_result

    source_priority = []
    if propagate_action is not None:
        source_priority = propagate_action.get("source_priority") or []
    source_ref = source_priority[0] if source_priority else None
    split_policy = (split_action or {}).get("split_shared_note_policy", "duplicate_to_all")
    propagation_strategy = (
        (propagate_action or {}).get("strategy", "strict_onset")
    )
    propagation_policy = (
        (propagate_action or {}).get("policy", "fill_missing_only")
    )
    verse_number = (propagate_action or {}).get("verse_number", "1")
    copy_all_verses = bool((propagate_action or {}).get("copy_all_verses", False))
    section_overrides = (propagate_action or {}).get("section_overrides") or []

    result = _prepare_score_for_voice_part_legacy(
        score,
        part_index=target_ref["part_index"],
        voice_part_id=target_ref["voice_part_id"],
        allow_lyric_propagation=propagate_action is not None,
        source_voice_part_id=(
            source_ref["voice_part_id"] if isinstance(source_ref, dict) else None
        ),
        source_part_index=(
            source_ref["part_index"] if isinstance(source_ref, dict) else None
        ),
        split_shared_note_policy=split_policy,
        propagation_strategy=propagation_strategy,
        propagation_policy=propagation_policy,
        verse_number=verse_number,
        copy_all_verses=copy_all_verses,
        section_overrides=section_overrides,
    )
    if (
        _feature_flag_enabled("VOICE_PART_REPAIR_LOOP_ENABLED")
        and result.get("status") == "action_required"
        and result.get("action") == "validation_failed_needs_review"
        and propagate_action is not None
    ):
        repair_attempts: List[Dict[str, Any]] = []
        candidate_strategies = [
            strategy_name
            for strategy_name in ["overlap_best_match", "syllable_flow", "strict_onset"]
            if strategy_name != propagation_strategy
        ]
        max_retries = _env_int("VOICE_PART_REPAIR_MAX_RETRIES", 2)
        for attempt_index, strategy_name in enumerate(
            candidate_strategies[:max_retries], start=1
        ):
            retry_result = _prepare_score_for_voice_part_legacy(
                score,
                part_index=target_ref["part_index"],
                voice_part_id=target_ref["voice_part_id"],
                allow_lyric_propagation=True,
                source_voice_part_id=(
                    source_ref["voice_part_id"] if isinstance(source_ref, dict) else None
                ),
                source_part_index=(
                    source_ref["part_index"] if isinstance(source_ref, dict) else None
                ),
                split_shared_note_policy=split_policy,
                propagation_strategy=strategy_name,
                propagation_policy=propagation_policy,
                verse_number=verse_number,
                copy_all_verses=copy_all_verses,
                section_overrides=section_overrides,
            )
            repair_attempts.append(
                {
                    "attempt": attempt_index,
                    "strategy": strategy_name,
                    "status": retry_result.get("status"),
                    "action": retry_result.get("action"),
                    "validation": _summarize_validation_issues(
                        retry_result.get("validation") or {}
                    ),
                }
            )
            if retry_result.get("status") in {"ready", "ready_with_warnings"}:
                metadata = retry_result.setdefault("metadata", {})
                metadata["repair_loop"] = {
                    "attempted": True,
                    "attempt_count": attempt_index,
                    "resolved_with_strategy": strategy_name,
                    "attempts": repair_attempts,
                }
                return retry_result
        result["repair_loop"] = {
            "attempted": True,
            "attempt_count": len(repair_attempts),
            "attempts": repair_attempts,
            "escalated": True,
            "summary": _summarize_validation_issues(result.get("validation") or {}),
        }

    if result.get("status") in {"ready", "ready_with_warnings", "action_required"}:
        metadata = result.setdefault("metadata", {})
        metadata["plan_applied"] = True
        metadata["section_overrides_received"] = len(section_overrides)
        metadata["split_shared_note_policy"] = split_policy
    return result


def _generate_same_part_voice_part_derivations(
    *,
    source_score: Dict[str, Any],
    target_score: Dict[str, Any],
    part_index: int,
    primary_voice_part_id: str,
    initial_source_musicxml_path: Optional[str] = None,
    explicit_target_voice_part_ids: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    source_parts = source_score.get("parts") or []
    target_parts = target_score.get("parts") or []
    if part_index < 0 or part_index >= len(source_parts) or part_index >= len(target_parts):
        return None
    part = source_parts[part_index]
    target_part = target_parts[part_index]
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis.get("voice_parts") or []
    if len(voice_parts) <= 1:
        return None

    # Chain materialization by reusing latest output path as next source.
    current_score = deepcopy(target_score)
    source_musicxml_path = (
        str(initial_source_musicxml_path)
        if isinstance(initial_source_musicxml_path, str) and initial_source_musicxml_path
        else _resolve_source_musicxml_path(current_score)
    )
    last_result: Optional[Dict[str, Any]] = None
    for vp in voice_parts:
        vp_id = str(vp.get("voice_part_id") or "")
        source_voice_id = str(vp.get("source_voice_id") or "")
        # For grouped generation, focus on split lines (exclude _default unison lane)
        # so requesting one line per staff yields the expected SATB-style siblings.
        if source_voice_id == "_default":
            continue
        if not vp_id or vp_id == primary_voice_part_id:
            continue
        if explicit_target_voice_part_ids and vp_id in explicit_target_voice_part_ids:
            continue
        voice_priority = {
            str(item.get("source_voice_id")): idx for idx, item in enumerate(voice_parts)
        }
        selected_notes = _select_part_notes_for_voice(
            part.get("notes") or [],
            source_voice_id,
            split_shared_note_policy="duplicate_to_all",
            voice_priority=voice_priority,
        )
        vp_text = vp_id.lower()
        prefer_high = (
            "voice part 1" in vp_text or "soprano" in vp_text or "tenor" in vp_text
        )
        selected_notes = _enforce_non_overlapping_sequence(
            selected_notes, prefer_high=prefer_high
        )
        transformed_part = dict(target_part)
        transformed_part["_source_part_index"] = part_index
        transformed_part["_source_part_id"] = target_part.get("part_id")
        transformed_part["_source_part_name"] = target_part.get("part_name")
        transformed_part["notes"] = selected_notes
        transformed_part["part_name"] = vp_id

        current_score["source_musicxml_path"] = source_musicxml_path
        result = _finalize_transform_result(
            current_score,
            part_index=part_index,
            target_voice_part_id=vp_id,
            source_voice_part_id=None,
            source_part_index=part_index,
            transformed_part=transformed_part,
            propagated=False,
            status="ready",
            validation={
                "lyric_coverage_ratio": 1.0 if _part_has_any_lyric(selected_notes) else 0.0,
                "source_alignment_ratio": 1.0,
                "missing_lyric_sung_note_count": 0,
                "lyric_exempt_note_count": 0,
                "unresolved_measures": [],
            },
            source_musicxml_path=source_musicxml_path,
            target_source_voice_id=source_voice_id,
        )
        if result.get("status") not in {"ready", "ready_with_warnings"}:
            return result
        current_score = result.get("score", current_score)
        source_musicxml_path = result.get("modified_musicxml_path") or source_musicxml_path
        last_result = result

    if last_result is None:
        return None
    return last_result


def _execute_timeline_sections(
    score: Dict[str, Any], target_entry: Dict[str, Any], *, allow_repair: bool = True
) -> Dict[str, Any]:
    target_ref = target_entry["target"]
    part_index = int(target_ref["part_index"])
    target_voice_part_id = str(target_ref["voice_part_id"])
    verse_number = _normalize_verse_number(target_entry.get("verse_number", "1"))
    copy_all_verses = bool(target_entry.get("copy_all_verses", False))
    split_shared_note_policy = str(
        target_entry.get("split_shared_note_policy", "duplicate_to_all")
    ).strip()

    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return _action_required(
            "invalid_part_index",
            f"part_index {part_index} is out of range.",
            part_index=part_index,
        )

    source_score = deepcopy(score)
    part = source_score["parts"][part_index]
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis["voice_parts"]
    if not voice_parts:
        return _action_required(
            "missing_voice_parts",
            "No voiced note events were found in the selected part.",
            part_index=part_index,
        )

    target_meta = _resolve_target_voice_part(
        voice_parts,
        voice_id=None,
        voice_part_id=target_voice_part_id,
    )
    if target_meta is None:
        return _action_required(
            "target_voice_part_not_found",
            "Requested voice part was not found in the selected part.",
            part_index=part_index,
            voice_part_options=[vp["voice_part_id"] for vp in voice_parts],
        )
    target_voice = str(target_meta["source_voice_id"])
    voice_priority = {
        str(vp["source_voice_id"]): idx for idx, vp in enumerate(voice_parts)
    }
    working_notes = _select_part_notes_for_voice(
        part.get("notes") or [],
        target_voice,
        split_shared_note_policy=split_shared_note_policy,
        voice_priority=voice_priority,
    )
    source_notes_for_validation: List[Dict[str, Any]] = []
    section_results: List[Dict[str, Any]] = []
    section_quality_issues: List[Dict[str, Any]] = []
    propagated = False
    first_source_ref: Optional[Dict[str, Any]] = None

    for section in target_entry.get("sections") or []:
        start_measure = int(section["start_measure"])
        end_measure = int(section["end_measure"])
        mode = str(section["mode"])
        decision_type = str(section.get("decision_type") or "EXTRACT_FROM_VOICE")
        method = str(section.get("method") or "trivial")
        before_lyric_count = _count_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        before_word_lyric_count = _count_word_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        before_extension_lyric_count = _count_extension_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        copied_note_count = 0
        propagation_diag: Optional[Dict[str, Any]] = None

        if mode == "rest":
            working_notes = [
                dict(note)
                for note in working_notes
                if not _note_in_measure_range(note, (start_measure, end_measure))
            ]
            working_notes.extend(
                _build_section_rest_notes(
                    source_score=source_score,
                    part_index=part_index,
                    target_voice=target_voice,
                    start_measure=start_measure,
                    end_measure=end_measure,
                )
            )
        else:
            melody_source = section.get("melody_source")
            if isinstance(melody_source, dict):
                if decision_type == "SPLIT_CHORDS_SELECT_NOTES":
                    copied_note_count = _split_chords_select_notes_into_target(
                        working_notes=working_notes,
                        source_score=source_score,
                        target_voice=target_voice,
                        target_voice_rank=int(voice_priority.get(target_voice, 0)),
                        target_voice_part_count=len(voice_parts),
                        source_part_index=int(melody_source["part_index"]),
                        source_voice_part_id=str(melody_source["voice_part_id"]),
                        start_measure=start_measure,
                        end_measure=end_measure,
                        method=method,
                        split_selector=str(section.get("split_selector") or "upper"),
                        rank_index=int(section.get("rank_index") or 0),
                        rank_fallback=str(section.get("rank_fallback") or "greedy"),
                    )
                else:
                    copied_note_count = _copy_section_melody_into_target(
                        working_notes=working_notes,
                        source_score=source_score,
                        target_voice=target_voice,
                        source_part_index=int(melody_source["part_index"]),
                        source_voice_part_id=str(melody_source["voice_part_id"]),
                        start_measure=start_measure,
                        end_measure=end_measure,
                    )
            lyric_source = section.get("lyric_source")
            if isinstance(lyric_source, dict):
                if (
                    not isinstance(melody_source, dict)
                    and not _has_sung_note_in_measure_range(
                        working_notes,
                        start_measure=start_measure,
                        end_measure=end_measure,
                    )
                ):
                    return _action_required(
                        "lyric_source_without_target_notes",
                        (
                            "Section uses lyric_source without melody_source, but target lane "
                            "has no notes in this range."
                        ),
                        part_index=part_index,
                        target_voice_part=target_voice_part_id,
                        section_start_measure=start_measure,
                        section_end_measure=end_measure,
                    )
                override_source_notes = _resolve_source_notes(
                    source_score,
                    source_part_index=int(lyric_source["part_index"]),
                    source_voice_part_id=str(lyric_source["voice_part_id"]),
                )
                if override_source_notes is None:
                    return _action_required(
                        "invalid_section_source",
                        (
                            "Selected section lyric source is invalid for lyric propagation."
                        ),
                        part_index=part_index,
                        target_voice_part=target_voice_part_id,
                        section_start_measure=start_measure,
                        section_end_measure=end_measure,
                    )
                if first_source_ref is None:
                    first_source_ref = {
                        "part_index": int(lyric_source["part_index"]),
                        "voice_part_id": str(lyric_source["voice_part_id"]),
                    }
                source_notes_for_validation.extend(override_source_notes)
                propagated = True
                working_notes, propagation_diag = _propagate_lyrics(
                    target_notes=working_notes,
                    source_notes=override_source_notes,
                    strategy=str(section.get("lyric_strategy") or "strict_onset"),
                    policy=str(section.get("lyric_policy") or "fill_missing_only"),
                    verse_number=verse_number,
                    copy_all_verses=copy_all_verses,
                    measure_range=(start_measure, end_measure),
                )

        after_lyric_count = _count_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        after_word_lyric_count = _count_word_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        after_extension_lyric_count = _count_extension_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        section_missing = _count_missing_lyric_sung_notes_in_range(
            working_notes, start_measure, end_measure
        )
        copied_word_lyric_count = max(0, after_word_lyric_count - before_word_lyric_count)
        copied_extension_lyric_count = max(
            0, after_extension_lyric_count - before_extension_lyric_count
        )
        section_results.append(
            {
                "section_mode": mode,
                "decision_type": decision_type,
                "method": method,
                "start_measure": start_measure,
                "end_measure": end_measure,
                "copied_note_count": copied_note_count,
                "copied_lyric_count": max(0, after_lyric_count - before_lyric_count),
                "copied_word_lyric_count": copied_word_lyric_count,
                "copied_extension_lyric_count": copied_extension_lyric_count,
                "missing_lyric_sung_note_count": section_missing,
                "source_lyric_candidates_count": (
                    int(propagation_diag.get("source_lyric_candidates_count", 0))
                    if isinstance(propagation_diag, dict)
                    else 0
                ),
                "mapped_source_lyrics_count": (
                    int(propagation_diag.get("mapped_source_lyrics_count", 0))
                    if isinstance(propagation_diag, dict)
                    else 0
                ),
                "dropped_source_lyrics_count": (
                    int(propagation_diag.get("dropped_source_lyrics_count", 0))
                    if isinstance(propagation_diag, dict)
                    else 0
                ),
                "dropped_source_lyrics": (
                    list(propagation_diag.get("dropped_source_lyrics") or [])
                    if isinstance(propagation_diag, dict)
                    else []
                ),
            }
        )
        lyric_source = section.get("lyric_source")
        if (
            mode == "derive"
            and copied_note_count > 0
            and isinstance(lyric_source, dict)
            and isinstance(lyric_source.get("part_index"), int)
            and isinstance(lyric_source.get("voice_part_id"), str)
        ):
            source_stats = _lyric_stats_for_voice_part_range(
                score=source_score,
                part_index=int(lyric_source["part_index"]),
                voice_part_id=str(lyric_source["voice_part_id"]),
                start_measure=start_measure,
                end_measure=end_measure,
            )
            target_stats = _lyric_stats_for_notes_range(
                working_notes,
                start_measure=start_measure,
                end_measure=end_measure,
            )
            if (
                source_stats["word_lyric_note_count"] > 0
                and target_stats["word_lyric_note_count"] == 0
                and target_stats["extension_lyric_note_count"] > 0
            ):
                section_quality_issues.append(
                    {
                        "code": "extension_only_output_with_word_source_available",
                        "start_measure": start_measure,
                        "end_measure": end_measure,
                        "decision_type": decision_type,
                        "method": method,
                        "lyric_source": {
                            "part_index": int(lyric_source["part_index"]),
                            "voice_part_id": str(lyric_source["voice_part_id"]),
                        },
                        "source_stats": source_stats,
                        "target_stats": target_stats,
                    }
                )

    if section_quality_issues:
        return _action_required(
            "section_lyric_quality_failed",
            "Section-level lyric quality check failed. Selected lyric sources produced extension-only output where words are available.",
            part_index=part_index,
            target_voice_part=target_voice_part_id,
            section_quality_issues=section_quality_issues,
            section_results=section_results,
        )

    working_notes.sort(
        key=lambda row: (
            int(row.get("measure_number") or 0),
            float(row.get("offset_beats") or 0.0),
            float(row.get("pitch_midi") or 0.0),
        )
    )
    vp_text = str(target_voice_part_id).lower()
    prefer_high = "voice part 1" in vp_text or "soprano" in vp_text or "tenor" in vp_text
    working_notes = _enforce_non_overlapping_sequence(
        working_notes, prefer_high=prefer_high
    )
    transformed_part = dict(part)
    transformed_part["_source_part_index"] = part_index
    transformed_part["_source_part_id"] = part.get("part_id")
    transformed_part["_source_part_name"] = part.get("part_name")
    transformed_part["notes"] = [dict(note) for note in working_notes]
    transformed_part["part_name"] = target_voice_part_id
    source_score["parts"][part_index] = transformed_part

    validation = _validate_transformed_notes(
        transformed_notes=working_notes,
        source_notes=source_notes_for_validation,
    )
    structural = validation.get("structural") or {}
    if structural.get("hard_fail"):
        failing_ranges = _collapse_measure_ranges(
            structural.get("structural_unresolved_measures") or []
        )
        if (
            allow_repair
            and _feature_flag_enabled("VOICE_PART_REPAIR_LOOP_ENABLED")
            and failing_ranges
        ):
            patched_entry = deepcopy(target_entry)
            for section in patched_entry.get("sections") or []:
                s_start = int(section["start_measure"])
                s_end = int(section["end_measure"])
                overlaps = any(
                    not (s_end < region["start"] or s_start > region["end"])
                    for region in failing_ranges
                )
                if not overlaps:
                    continue
                # Deprecated internal-only repair fallback. Public plans cannot request B.
                section["method"] = "B"
                section["lyric_policy"] = "replace_all"
                section["lyric_strategy"] = "syllable_flow"
                if section.get("mode") == "derive":
                    section["decision_type"] = "SPLIT_CHORDS_SELECT_NOTES"
                    if section.get("melody_source") is None:
                        section["melody_source"] = {
                            "part_index": part_index,
                            "voice_part_id": target_voice_part_id,
                        }
                    vp_text = str(target_voice_part_id).lower()
                    section["split_selector"] = (
                        "upper" if "voice part 1" in vp_text or "soprano" in vp_text or "tenor" in vp_text else "lower"
                    )
            retry = _execute_timeline_sections(score, patched_entry, allow_repair=False)
            meta = retry.setdefault("metadata", {})
            meta["repair_loop"] = {
                "attempted": True,
                "attempt_count": 1,
                "reason": "structural_validation_failed",
                "failing_ranges": failing_ranges,
            }
            return retry
        return _action_required(
            "structural_validation_failed",
            "Derived section output is not synthesis-safe (monophony/overlap checks failed).",
            part_index=part_index,
            target_voice_part=target_voice_part_id,
            validation=validation,
            section_results=section_results,
            failing_ranges=failing_ranges,
        )
    status = "ready"
    warnings: List[Dict[str, Any]] = []
    if validation["missing_lyric_sung_note_count"] > 0:
        if validation["lyric_coverage_ratio"] >= 0.90:
            status = "ready_with_warnings"
            warnings.append(
                {
                    "code": "partial_lyric_coverage",
                    "missing_note_count": validation["missing_lyric_sung_note_count"],
                    "lyric_coverage_ratio": validation["lyric_coverage_ratio"],
                    "unresolved_measures": validation["unresolved_measures"][:10],
                }
            )
        else:
            return _action_required(
                "validation_failed_needs_review",
                "Lyric propagation did not meet minimum coverage.",
                part_index=part_index,
                target_voice_part=target_voice_part_id,
                validation=validation,
                section_results=section_results,
                failing_ranges=_collapse_measure_ranges(
                    validation.get("unresolved_measures") or []
                ),
            )
    min_word_ratio = _env_float("VOICE_PART_MIN_WORD_LYRIC_COVERAGE_RATIO", 0.15)
    warn_floor_ratio = _env_float("VOICE_PART_MIN_WORD_LYRIC_WARN_FLOOR_RATIO", 0.75)
    if (
        validation["source_word_lyric_note_count"] > 0
        and validation["word_lyric_coverage_ratio"] < min_word_ratio
    ):
        if validation["word_lyric_coverage_ratio"] >= max(0.0, min_word_ratio * warn_floor_ratio):
            status = "ready_with_warnings"
            warnings.append(
                {
                    "code": "low_word_lyric_coverage",
                    "word_lyric_coverage_ratio": validation["word_lyric_coverage_ratio"],
                    "min_required_word_lyric_coverage_ratio": min_word_ratio,
                    "extension_lyric_ratio": validation["extension_lyric_ratio"],
                }
            )
        else:
            return _action_required(
                "word_lyric_coverage_too_low",
                "Word-lyric coverage is too low. Output is dominated by extension-only lyrics.",
                part_index=part_index,
                target_voice_part=target_voice_part_id,
                validation=validation,
                min_required_word_lyric_coverage_ratio=min_word_ratio,
                section_results=section_results,
            )

    result = _finalize_transform_result(
        source_score,
        part_index=part_index,
        target_voice_part_id=target_voice_part_id,
        source_voice_part_id=(
            str(first_source_ref["voice_part_id"]) if first_source_ref else None
        ),
        source_part_index=(
            int(first_source_ref["part_index"]) if first_source_ref else part_index
        ),
        transformed_part=transformed_part,
        propagated=propagated,
        status=status,
        warnings=warnings,
        validation=validation,
        source_musicxml_path=_resolve_source_musicxml_path(score),
        target_source_voice_id=target_voice,
    )
    if result.get("status") in {"ready", "ready_with_warnings", "action_required"}:
        metadata = result.setdefault("metadata", {})
        metadata["plan_applied"] = True
        metadata["plan_mode"] = "timeline_sections"
        metadata["section_count"] = len(target_entry.get("sections") or [])
        metadata["split_shared_note_policy"] = split_shared_note_policy
        metadata["section_results"] = section_results
    return result


def _split_chords_select_notes_into_target(
    *,
    working_notes: List[Dict[str, Any]],
    source_score: Dict[str, Any],
    target_voice: str,
    target_voice_rank: int,
    target_voice_part_count: int,
    source_part_index: int,
    source_voice_part_id: str,
    start_measure: int,
    end_measure: int,
    method: str,
    split_selector: str,
    rank_index: int,
    rank_fallback: str,
) -> int:
    source_notes = _resolve_source_notes_allow_lyricless(
        source_score,
        source_part_index=source_part_index,
        source_voice_part_id=source_voice_part_id,
    )
    if source_notes is None:
        return 0
    grouped: Dict[Tuple[int, float], List[Dict[str, Any]]] = {}
    copied_rests: List[Dict[str, Any]] = []
    for note in source_notes:
        if not _note_in_measure_range(note, (start_measure, end_measure)):
            continue
        if note.get("is_rest"):
            rest_note = dict(note)
            rest_note["voice"] = target_voice
            rest_note["lyric"] = None
            rest_note["syllabic"] = None
            rest_note["lyric_is_extended"] = False
            copied_rests.append(rest_note)
            continue
        key = (
            int(note.get("measure_number") or 0),
            round(float(note.get("offset_beats") or 0.0), 6),
        )
        grouped.setdefault(key, []).append(note)
    chosen: List[Dict[str, Any]] = []
    previous_pitch: Optional[float] = None
    if method == "B":
        # Deprecated internal-only method retained for compatibility with repair paths.
        chosen = _choose_notes_dp(grouped, split_selector=split_selector)
    elif method == "ranked":
        chosen = _choose_notes_ranked(
            grouped,
            rank_index=rank_index,
            rank_fallback=rank_fallback,
        )
    elif method == "trivial":
        chosen = _choose_notes_trivial_ranked(
            grouped,
            target_voice_rank=target_voice_rank,
            target_voice_part_count=target_voice_part_count,
            split_selector=split_selector,
        )
    else:
        # Deprecated internal-only method retained for compatibility with repair paths.
        for key in sorted(grouped):
            candidates = grouped[key]
            selected = _choose_note_rule_based(
                candidates,
                split_selector=split_selector,
                previous_pitch=previous_pitch,
            )
            if selected is None:
                continue
            chosen.append(selected)
            previous_pitch = float(selected.get("pitch_midi") or 0.0)
    copied: List[Dict[str, Any]] = list(copied_rests)
    for note in chosen:
        new_note = dict(note)
        new_note["voice"] = target_voice
        new_note["lyric"] = None
        new_note["syllabic"] = None
        new_note["lyric_is_extended"] = False
        copied.append(new_note)
    copied = _enforce_non_overlapping_sequence(
        copied,
        prefer_high=str(split_selector).lower() in {"upper", "high", "tenor"},
    )
    kept = [
        dict(note)
        for note in working_notes
        if not _note_in_measure_range(note, (start_measure, end_measure))
    ]
    kept.extend(copied)
    working_notes[:] = kept
    return len([note for note in copied if not note.get("is_rest")])


def _choose_notes_ranked(
    grouped: Dict[Tuple[int, float], List[Dict[str, Any]]],
    *,
    rank_index: int,
    rank_fallback: str,
) -> List[Dict[str, Any]]:
    """Choose a fixed top-down rank at each onset with deterministic fallback."""
    chosen: List[Dict[str, Any]] = []
    rank = max(0, int(rank_index))
    fallback = str(rank_fallback or "greedy").strip().lower()
    if fallback not in VALID_RANK_FALLBACKS:
        fallback = "greedy"
    for key in sorted(grouped):
        ordered = sorted(
            grouped[key],
            key=lambda n: float(n.get("pitch_midi") or 0.0),
            reverse=True,
        )
        if rank < len(ordered):
            chosen.append(dict(ordered[rank]))
            continue
        if fallback == "skip":
            continue
        chosen.append(dict(ordered[-1]))
    return chosen


def _choose_note_rule_based(
    candidates: Sequence[Dict[str, Any]],
    *,
    split_selector: str,
    previous_pitch: Optional[float],
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    reverse = str(split_selector).lower() in {"upper", "high", "tenor"}
    ordered = sorted(
        candidates, key=lambda n: float(n.get("pitch_midi") or 0.0), reverse=reverse
    )
    if previous_pitch is None:
        return dict(ordered[0])
    return dict(
        min(
            ordered,
            key=lambda n: (
                abs(float(n.get("pitch_midi") or 0.0) - previous_pitch),
                -float(n.get("pitch_midi") or 0.0) if reverse else float(n.get("pitch_midi") or 0.0),
            ),
        )
    )


def _choose_notes_dp(
    grouped: Dict[Tuple[int, float], List[Dict[str, Any]]], *, split_selector: str
) -> List[Dict[str, Any]]:
    slots = sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    if not slots:
        return []
    prefer_high = str(split_selector).lower() in {"upper", "high", "tenor"}
    dp: List[Dict[int, Tuple[float, Optional[int]]]] = []
    candidates_per_slot: List[List[Dict[str, Any]]] = []
    for _, candidates in slots:
        ordered = sorted(candidates, key=lambda n: float(n.get("pitch_midi") or 0.0))
        candidates_per_slot.append(ordered)
    for t, cands in enumerate(candidates_per_slot):
        layer: Dict[int, Tuple[float, Optional[int]]] = {}
        for j, cand in enumerate(cands):
            pitch = float(cand.get("pitch_midi") or 0.0)
            extreme_cost = (-0.05 * pitch) if prefer_high else (0.05 * pitch)
            if t == 0:
                layer[j] = (extreme_cost, None)
                continue
            best = (10**9, None)
            for i, (prev_cost, _) in dp[t - 1].items():
                prev_pitch = float(candidates_per_slot[t - 1][i].get("pitch_midi") or 0.0)
                leap = abs(pitch - prev_pitch)
                leap_cost = leap * (1.5 if leap > 7 else 1.0)
                cost = prev_cost + leap_cost + extreme_cost
                if cost < best[0]:
                    best = (cost, i)
            layer[j] = best
        dp.append(layer)
    last_idx = min(dp[-1], key=lambda idx: dp[-1][idx][0])
    out_indices = [last_idx]
    for t in range(len(dp) - 1, 0, -1):
        _, parent = dp[t][out_indices[-1]]
        out_indices.append(0 if parent is None else parent)
    out_indices.reverse()
    return [dict(candidates_per_slot[t][idx]) for t, idx in enumerate(out_indices)]


def _choose_notes_trivial_ranked(
    grouped: Dict[Tuple[int, float], List[Dict[str, Any]]],
    *,
    target_voice_rank: int,
    target_voice_part_count: int,
    split_selector: str,
) -> List[Dict[str, Any]]:
    """Use direct rank mapping when chord size equals voice-part count; otherwise fallback."""
    chosen: List[Dict[str, Any]] = []
    previous_pitch: Optional[float] = None
    rank = max(0, int(target_voice_rank))
    voice_count = max(1, int(target_voice_part_count))
    for key in sorted(grouped):
        candidates = grouped[key]
        if len(candidates) == voice_count:
            ordered = sorted(
                candidates,
                key=lambda n: float(n.get("pitch_midi") or 0.0),
                reverse=True,
            )
            idx = min(rank, len(ordered) - 1)
            selected = dict(ordered[idx])
        else:
            selected = _choose_note_rule_based(
                candidates,
                split_selector=split_selector,
                previous_pitch=previous_pitch,
            )
            if selected is None:
                continue
        chosen.append(selected)
        previous_pitch = float(selected.get("pitch_midi") or 0.0)
    return chosen


def _enforce_non_overlapping_sequence(
    notes: Sequence[Dict[str, Any]], *, prefer_high: bool = True
) -> List[Dict[str, Any]]:
    if not notes:
        return []
    ordered_all = sorted(
        [dict(note) for note in notes],
        key=lambda n: (
            int(n.get("measure_number") or 0),
            float(n.get("offset_beats") or 0.0),
            float(n.get("pitch_midi") or 0.0),
        ),
    )
    # Enforce one note per onset: keep lyric-bearing note first, then higher pitch.
    by_start: Dict[float, List[Dict[str, Any]]] = {}
    for note in ordered_all:
        start = round(float(note.get("offset_beats") or 0.0), 6)
        by_start.setdefault(start, []).append(note)
    ordered: List[Dict[str, Any]] = []
    for start in sorted(by_start):
        candidates = by_start[start]
        if prefer_high:
            chosen = max(
                candidates,
                key=lambda n: (
                    1 if _has_lyric(n) else 0,
                    float(n.get("pitch_midi") or 0.0),
                ),
            )
        else:
            chosen = min(
                candidates,
                key=lambda n: (
                    0 if _has_lyric(n) else 1,
                    float(n.get("pitch_midi") or 0.0),
                ),
            )
        ordered.append(chosen)

    out: List[Dict[str, Any]] = []
    for idx, note in enumerate(ordered):
        start = float(note.get("offset_beats") or 0.0)
        duration = max(0.0, float(note.get("duration_beats") or 0.0))
        end = start + duration
        if idx + 1 < len(ordered):
            next_start = float(ordered[idx + 1].get("offset_beats") or 0.0)
            if next_start > start:
                end = min(end, next_start)
        clipped = max(0.0, end - start)
        if clipped <= 0.0:
            continue
        note["duration_beats"] = clipped
        out.append(note)
    return out


def _copy_section_melody_into_target(
    *,
    working_notes: List[Dict[str, Any]],
    source_score: Dict[str, Any],
    target_voice: str,
    source_part_index: int,
    source_voice_part_id: str,
    start_measure: int,
    end_measure: int,
) -> int:
    source_notes = _resolve_source_notes_allow_lyricless(
        source_score,
        source_part_index=source_part_index,
        source_voice_part_id=source_voice_part_id,
    )
    if source_notes is None:
        return 0
    copied: List[Dict[str, Any]] = []
    for source_note in source_notes:
        if not _note_in_measure_range(source_note, (start_measure, end_measure)):
            continue
        note = dict(source_note)
        note["voice"] = target_voice
        note["lyric"] = None
        note["syllabic"] = None
        note["lyric_is_extended"] = False
        copied.append(note)

    kept = [
        dict(note)
        for note in working_notes
        if not _note_in_measure_range(note, (start_measure, end_measure))
    ]
    kept.extend(copied)
    working_notes[:] = kept
    return len([note for note in copied if not note.get("is_rest")])


def _resolve_source_notes_allow_lyricless(
    score: Dict[str, Any],
    *,
    source_part_index: int,
    source_voice_part_id: str,
) -> Optional[List[Dict[str, Any]]]:
    parts = score.get("parts") or []
    if source_part_index < 0 or source_part_index >= len(parts):
        return None
    source_part = parts[source_part_index]
    source_analysis = _analyze_part_voice_parts(source_part, source_part_index)
    source_meta = _find_voice_part_by_id(
        source_analysis["voice_parts"], source_voice_part_id
    )
    if source_meta is None:
        return None
    return _select_part_notes_for_voice(
        source_part.get("notes") or [],
        source_meta["source_voice_id"],
    )


def _build_section_rest_notes(
    *,
    source_score: Dict[str, Any],
    part_index: int,
    target_voice: str,
    start_measure: int,
    end_measure: int,
) -> List[Dict[str, Any]]:
    parts = source_score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return []
    part_notes = parts[part_index].get("notes") or []
    by_measure: Dict[int, List[Dict[str, Any]]] = {}
    for note in part_notes:
        measure = int(note.get("measure_number") or 0)
        if measure < start_measure or measure > end_measure:
            continue
        by_measure.setdefault(measure, []).append(note)

    rests: List[Dict[str, Any]] = []
    for measure in range(start_measure, end_measure + 1):
        rows = by_measure.get(measure) or []
        if not rows:
            continue
        starts = [float(row.get("offset_beats") or 0.0) for row in rows]
        ends = [
            float(row.get("offset_beats") or 0.0)
            + max(0.0, float(row.get("duration_beats") or 0.0))
            for row in rows
        ]
        start = min(starts)
        end = max(ends)
        duration = max(0.0, end - start)
        if duration <= 0.0:
            continue
        rests.append(
            {
                "offset_beats": start,
                "duration_beats": duration,
                "pitch_midi": None,
                "pitch_hz": None,
                "lyric": None,
                "syllabic": None,
                "lyric_is_extended": False,
                "is_rest": True,
                "tie_type": None,
                "voice": target_voice,
                "staff": None,
                "chord_group_id": None,
                "lyric_line_index": None,
                "measure_number": measure,
            }
        )
    return rests


def _count_lyric_sung_notes_in_range(
    notes: Sequence[Dict[str, Any]], start_measure: int, end_measure: int
) -> int:
    return sum(
        1
        for note in notes
        if (
            not note.get("is_rest")
            and _note_in_measure_range(note, (start_measure, end_measure))
            and _has_lyric(note)
        )
    )


def _lyric_stats_for_notes_range(
    notes: Sequence[Dict[str, Any]],
    *,
    start_measure: int,
    end_measure: int,
) -> Dict[str, float | int]:
    sung = [
        note
        for note in notes
        if (not note.get("is_rest")) and _note_in_measure_range(note, (start_measure, end_measure))
    ]
    word = sum(1 for note in sung if _lyric_kind(note) == "word")
    extension = sum(1 for note in sung if _lyric_kind(note) == "extension")
    empty = sum(1 for note in sung if _lyric_kind(note) == "empty")
    lyric = word + extension
    total = max(1, len(sung))
    return {
        "sung_note_count": len(sung),
        "lyric_note_count": lyric,
        "word_lyric_note_count": word,
        "extension_lyric_note_count": extension,
        "empty_lyric_note_count": empty,
        "word_lyric_coverage_ratio": round(float(word) / float(total), 4),
        "extension_lyric_ratio": round(float(extension) / float(total), 4),
    }


def _lyric_stats_for_voice_part_range(
    *,
    score: Dict[str, Any],
    part_index: int,
    voice_part_id: str,
    start_measure: int,
    end_measure: int,
) -> Dict[str, float | int]:
    source_notes = _resolve_source_notes_allow_lyricless(
        score,
        source_part_index=part_index,
        source_voice_part_id=voice_part_id,
    )
    if source_notes is None:
        return {
            "sung_note_count": 0,
            "lyric_note_count": 0,
            "word_lyric_note_count": 0,
            "extension_lyric_note_count": 0,
            "empty_lyric_note_count": 0,
            "word_lyric_coverage_ratio": 0.0,
            "extension_lyric_ratio": 0.0,
        }
    return _lyric_stats_for_notes_range(
        source_notes,
        start_measure=start_measure,
        end_measure=end_measure,
    )


def _best_same_part_word_lyric_source_for_range(
    *,
    score: Dict[str, Any],
    part_index: int,
    start_measure: int,
    end_measure: int,
    exclude_voice_part_id: str,
) -> Optional[Dict[str, Any]]:
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return None
    analysis = _analyze_part_voice_parts(parts[part_index], part_index)
    candidates = analysis.get("voice_parts") or []
    best: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        voice_part_id = str(candidate.get("voice_part_id") or "").strip()
        source_voice_id = str(candidate.get("source_voice_id") or "").strip()
        if (
            not voice_part_id
            or voice_part_id == exclude_voice_part_id
            or source_voice_id == "_default"
        ):
            continue
        stats = _lyric_stats_for_voice_part_range(
            score=score,
            part_index=part_index,
            voice_part_id=voice_part_id,
            start_measure=start_measure,
            end_measure=end_measure,
        )
        if best is None:
            best = {"voice_part_id": voice_part_id, "stats": stats}
            continue
        best_stats = best["stats"]
        current_rank = (
            int(stats["word_lyric_note_count"]),
            float(stats["word_lyric_coverage_ratio"]),
            -int(stats["extension_lyric_note_count"]),
        )
        best_rank = (
            int(best_stats["word_lyric_note_count"]),
            float(best_stats["word_lyric_coverage_ratio"]),
            -int(best_stats["extension_lyric_note_count"]),
        )
        if current_rank > best_rank:
            best = {"voice_part_id": voice_part_id, "stats": stats}
    return best


def _is_weak_word_lyric_source_with_better_alternative(
    *,
    selected_stats: Dict[str, float | int],
    alternative_stats: Dict[str, float | int],
) -> bool:
    selected_sung = int(selected_stats.get("sung_note_count") or 0)
    selected_lyric = int(selected_stats.get("lyric_note_count") or 0)
    selected_word = int(selected_stats.get("word_lyric_note_count") or 0)
    alternative_word = int(alternative_stats.get("word_lyric_note_count") or 0)
    selected_ratio = float(selected_stats.get("word_lyric_coverage_ratio") or 0.0)
    alternative_ratio = float(alternative_stats.get("word_lyric_coverage_ratio") or 0.0)

    if selected_sung <= 0 or selected_lyric <= 0:
        return False
    if selected_word <= 0 or alternative_word <= selected_word:
        return False

    min_ratio = _env_float("VOICE_PART_WEAK_LYRIC_SOURCE_MAX_WORD_RATIO", 0.35)
    min_ratio_delta = _env_float("VOICE_PART_WEAK_LYRIC_SOURCE_MIN_RATIO_DELTA", 0.25)
    min_word_delta = _env_int("VOICE_PART_WEAK_LYRIC_SOURCE_MIN_WORD_DELTA", 2)

    return (
        selected_ratio < min_ratio
        and alternative_ratio >= (selected_ratio + min_ratio_delta)
        and alternative_word >= (selected_word + min_word_delta)
    )


def _count_word_lyric_sung_notes_in_range(
    notes: Sequence[Dict[str, Any]], start_measure: int, end_measure: int
) -> int:
    return sum(
        1
        for note in notes
        if (
            not note.get("is_rest")
            and _note_in_measure_range(note, (start_measure, end_measure))
            and _lyric_kind(note) == "word"
        )
    )


def _count_extension_lyric_sung_notes_in_range(
    notes: Sequence[Dict[str, Any]], start_measure: int, end_measure: int
) -> int:
    return sum(
        1
        for note in notes
        if (
            not note.get("is_rest")
            and _note_in_measure_range(note, (start_measure, end_measure))
            and _lyric_kind(note) == "extension"
        )
    )


def _count_missing_lyric_sung_notes_in_range(
    notes: Sequence[Dict[str, Any]], start_measure: int, end_measure: int
) -> int:
    return sum(
        1
        for note in notes
        if (
            not note.get("is_rest")
            and _note_in_measure_range(note, (start_measure, end_measure))
            and not _has_lyric(note)
        )
    )


def _has_sung_note_in_measure_range(
    notes: Sequence[Dict[str, Any]],
    *,
    start_measure: int,
    end_measure: int,
) -> bool:
    return any(
        not note.get("is_rest")
        and _note_in_measure_range(note, (start_measure, end_measure))
        for note in notes
    )


def _duplicate_section_to_all_voice_parts(
    score: Dict[str, Any],
    *,
    target_part_index: int,
    source_part_index: int,
    source_voice_part_id: str,
    start_measure: int,
    end_measure: int,
) -> Optional[Dict[str, Any]]:
    parts = score.get("parts") or []
    if target_part_index < 0 or target_part_index >= len(parts):
        return _action_required(
            "invalid_part_index",
            f"part_index {target_part_index} is out of range.",
            part_index=target_part_index,
        )
    if source_part_index < 0 or source_part_index >= len(parts):
        return _action_required(
            "invalid_source_part_index",
            f"source_part_index {source_part_index} is out of range.",
            part_index=target_part_index,
        )
    if source_part_index != target_part_index:
        return _action_required(
            "invalid_plan_payload",
            "duplicate_section_to_all_voice_parts must use a source within the same part.",
            part_index=target_part_index,
        )
    target_part = parts[target_part_index]
    analysis = _analyze_part_voice_parts(target_part, target_part_index)
    source_meta = _find_voice_part_by_id(analysis["voice_parts"], source_voice_part_id)
    if source_meta is None:
        return _action_required(
            "invalid_source_voice_part",
            "Selected source voice part is invalid for duplication.",
            part_index=target_part_index,
        )
    source_voice_id = source_meta["source_voice_id"]
    voice_ids = [vp["source_voice_id"] for vp in analysis["voice_parts"]]
    target_voice_ids = [vid for vid in voice_ids if vid != source_voice_id]
    notes = target_part.get("notes") or []
    selected_source_notes = [
        dict(note)
        for note in notes
        if not note.get("is_rest")
        and _voice_key(note.get("voice")) == source_voice_id
        and start_measure <= int(note.get("measure_number") or 0) <= end_measure
    ]
    if not selected_source_notes:
        return None

    for target_voice_id in target_voice_ids:
        has_existing = any(
            _voice_key(note.get("voice")) == target_voice_id
            and start_measure <= int(note.get("measure_number") or 0) <= end_measure
            for note in notes
        )
        if has_existing:
            continue
        for note in selected_source_notes:
            new_note = dict(note)
            new_note["voice"] = target_voice_id
            notes.append(new_note)
    notes.sort(
        key=lambda row: (
            int(row.get("measure_number") or 0),
            float(row.get("offset_beats") or 0.0),
            float(row.get("pitch_midi") or 0.0),
        )
    )
    return None


def _find_voice_part_by_id(
    voice_parts: Sequence[Dict[str, Any]], voice_part_id: str
) -> Optional[Dict[str, Any]]:
    key = str(voice_part_id).strip().lower()
    for vp in voice_parts:
        if str(vp["voice_part_id"]).strip().lower() == key:
            return vp
    return None


def _select_part_notes_for_voice(
    notes: Sequence[Dict[str, Any]],
    voice_key: str,
    *,
    split_shared_note_policy: str = "duplicate_to_all",
    voice_priority: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for note in notes:
        note_voice = _voice_key(note.get("voice"))
        if note.get("is_rest"):
            if note_voice == voice_key:
                selected.append(dict(note))
            continue
        if note_voice == voice_key:
            selected.append(dict(note))

    if split_shared_note_policy != "assign_primary_only" or not voice_priority:
        return selected

    by_signature: Dict[Tuple[float, float, float], List[str]] = {}
    for note in notes:
        if note.get("is_rest"):
            continue
        sig = _note_signature(note)
        by_signature.setdefault(sig, []).append(_voice_key(note.get("voice")))

    filtered: List[Dict[str, Any]] = []
    for note in selected:
        if note.get("is_rest"):
            filtered.append(note)
            continue
        sig = _note_signature(note)
        voices = by_signature.get(sig, [])
        if len(voices) <= 1:
            filtered.append(note)
            continue
        primary_voice = min(
            set(voices),
            key=lambda value: (int(voice_priority.get(value, 10**9)), value),
        )
        if _voice_key(note.get("voice")) == primary_voice:
            filtered.append(note)
    return filtered


def _propagate_lyrics(
    *,
    target_notes: Sequence[Dict[str, Any]],
    source_notes: Sequence[Dict[str, Any]],
    strategy: str,
    policy: str,
    verse_number: Optional[str],
    copy_all_verses: bool,
    measure_range: Optional[Tuple[int, int]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    effective_strategy = strategy
    if strategy == "syllable_flow" and not _feature_flag_enabled(
        "VOICE_PART_SYLLABLE_FLOW_ENABLED"
    ):
        effective_strategy = "strict_onset"

    source_timeline = _build_source_timeline(
        source_notes,
        verse_number=verse_number,
        copy_all_verses=copy_all_verses,
    )
    if measure_range is not None:
        source_timeline = [
            item
            for item in source_timeline
            if _note_in_measure_range(item["note"], measure_range)
        ]

    source_by_offset: Dict[float, List[Dict[str, Any]]] = {}
    for item in source_timeline:
        source_by_offset.setdefault(item["start"], []).append(item)

    syllable_tokens = source_timeline
    syllable_index = 0
    phrase_boundaries = _detect_phrase_boundaries(target_notes)
    matched_source_indices: set[int] = set()
    target_note_count_in_range = 0
    target_note_attempted_count = 0
    target_note_matched_count = 0

    out: List[Dict[str, Any]] = []
    for note_idx, note in enumerate(target_notes):
        copied = dict(note)
        if copied.get("is_rest"):
            out.append(copied)
            continue
        if not _note_in_measure_range(copied, measure_range):
            out.append(copied)
            continue
        target_note_count_in_range += 1
        if not _should_apply_policy(copied, policy):
            out.append(copied)
            continue
        target_note_attempted_count += 1

        source_item: Optional[Dict[str, Any]] = None
        if effective_strategy == "strict_onset":
            key = round(float(copied.get("offset_beats") or 0.0), 6)
            candidates = source_by_offset.get(key, [])
            source_item = candidates[0] if candidates else None
        elif effective_strategy == "overlap_best_match":
            source_item = _choose_overlap_best_match(copied, source_timeline)
        elif effective_strategy == "syllable_flow":
            if note_idx in phrase_boundaries:
                syllable_index = min(syllable_index, len(syllable_tokens))
            if syllable_index < len(syllable_tokens):
                source_item = syllable_tokens[syllable_index]
                if syllable_index < len(syllable_tokens) - 1:
                    syllable_index += 1
        if source_item is not None:
            _copy_lyric_fields(copied, source_item["note"])
            matched_source_indices.add(int(source_item.get("source_index", -1)))
            target_note_matched_count += 1
        out.append(copied)

    dropped_source_lyrics: List[Dict[str, Any]] = []
    for item in source_timeline:
        source_index = int(item.get("source_index", -1))
        if source_index in matched_source_indices:
            continue
        src_note = item.get("note") or {}
        dropped_source_lyrics.append(
            {
                "source_index": source_index,
                "measure_number": int(src_note.get("measure_number") or 0),
                "offset_beats": float(src_note.get("offset_beats") or 0.0),
                "duration_beats": float(src_note.get("duration_beats") or 0.0),
                "lyric": src_note.get("lyric"),
                "syllabic": src_note.get("syllabic"),
                "lyric_is_extended": bool(src_note.get("lyric_is_extended")),
            }
        )

    diagnostics = {
        "strategy": effective_strategy,
        "source_lyric_candidates_count": len(source_timeline),
        "target_note_count_in_range": target_note_count_in_range,
        "target_note_attempted_count": target_note_attempted_count,
        "target_note_matched_count": target_note_matched_count,
        "mapped_source_lyrics_count": len(matched_source_indices),
        "dropped_source_lyrics_count": len(dropped_source_lyrics),
        "dropped_source_lyrics": dropped_source_lyrics,
    }
    return out, diagnostics


def _resolve_source_notes(
    score: Dict[str, Any],
    *,
    source_part_index: int,
    source_voice_part_id: str,
) -> Optional[List[Dict[str, Any]]]:
    parts = score.get("parts") or []
    if source_part_index < 0 or source_part_index >= len(parts):
        return None
    source_part = parts[source_part_index]
    source_analysis = _analyze_part_voice_parts(source_part, source_part_index)
    source_meta = _find_voice_part_by_id(
        source_analysis["voice_parts"], source_voice_part_id
    )
    if source_meta is None or source_meta["lyric_note_count"] <= 0:
        return None
    return _select_part_notes_for_voice(
        source_part.get("notes") or [],
        source_meta["source_voice_id"],
    )


def _validate_transformed_notes(
    *,
    transformed_notes: Sequence[Dict[str, Any]],
    source_notes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    sung_notes = [note for note in transformed_notes if not note.get("is_rest")]
    exempt_notes = [note for note in sung_notes if bool(note.get("lyric_exempt"))]
    missing_notes = [note for note in sung_notes if not _has_lyric(note) and note not in exempt_notes]
    word_notes = [note for note in sung_notes if _lyric_kind(note) == "word" and note not in exempt_notes]
    extension_notes = [
        note for note in sung_notes if _lyric_kind(note) == "extension" and note not in exempt_notes
    ]
    effective_total = max(1, len(sung_notes) - len(exempt_notes))
    lyric_coverage_ratio = round(
        float(effective_total - len(missing_notes)) / float(effective_total), 4
    )
    word_lyric_coverage_ratio = round(float(len(word_notes)) / float(effective_total), 4)
    extension_lyric_ratio = round(float(len(extension_notes)) / float(effective_total), 4)
    source_offsets = {
        round(float(note.get("offset_beats") or 0.0), 6)
        for note in source_notes
        if not note.get("is_rest") and _has_lyric(note)
    }
    source_word_note_count = sum(
        1 for note in source_notes if not note.get("is_rest") and _lyric_kind(note) == "word"
    )
    aligned_notes = 0
    lyric_notes = 0
    for note in sung_notes:
        if not _has_lyric(note):
            continue
        lyric_notes += 1
        key = round(float(note.get("offset_beats") or 0.0), 6)
        if key in source_offsets:
            aligned_notes += 1
    source_alignment_ratio = round(
        float(aligned_notes) / float(max(1, lyric_notes)), 4
    )
    unresolved_measures = sorted(
        {
            int(note.get("measure_number") or 0)
            for note in missing_notes
            if int(note.get("measure_number") or 0) > 0
        }
    )
    structural = _validate_structural_singability(transformed_notes)
    return {
        "lyric_coverage_ratio": lyric_coverage_ratio,
        "word_lyric_coverage_ratio": word_lyric_coverage_ratio,
        "extension_lyric_ratio": extension_lyric_ratio,
        "word_lyric_note_count": len(word_notes),
        "extension_lyric_note_count": len(extension_notes),
        "source_word_lyric_note_count": source_word_note_count,
        "source_alignment_ratio": source_alignment_ratio,
        "missing_lyric_sung_note_count": len(missing_notes),
        "lyric_exempt_note_count": len(exempt_notes),
        "unresolved_measures": unresolved_measures,
        "structural": structural,
    }


def _validate_structural_singability(
    transformed_notes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    overlap_epsilon = 1e-5
    sung = [note for note in transformed_notes if not note.get("is_rest")]
    if not sung:
        return {
            "hard_fail": False,
            "max_simultaneous_notes": 0,
            "simultaneous_conflict_count": 0,
            "overlap_conflict_count": 0,
            "structural_unresolved_measures": [],
        }
    starts: Dict[int, List[Dict[str, Any]]] = {}
    intervals: List[Tuple[float, float, int]] = []
    for note in sung:
        start = float(note.get("offset_beats") or 0.0)
        duration = float(note.get("duration_beats") or 0.0)
        end = start + max(0.0, duration)
        measure = int(note.get("measure_number") or 0)
        start_bucket = int(round(start / overlap_epsilon))
        starts.setdefault(start_bucket, []).append(note)
        intervals.append((start, end, measure))
    simultaneous_conflicts = [bucket for bucket, notes in starts.items() if len(notes) > 1]
    intervals.sort(key=lambda row: (row[0], row[1]))
    active_ends: List[Tuple[float, int]] = []
    max_active = 0
    overlap_conflicts = 0
    overlap_measures: List[int] = []
    for start, end, measure in intervals:
        active_ends = [
            (active_end, active_measure)
            for active_end, active_measure in active_ends
            if active_end > (start + overlap_epsilon)
        ]
        if active_ends:
            overlap_conflicts += 1
            if measure > 0:
                overlap_measures.append(measure)
        active_ends.append((end, measure))
        if len(active_ends) > max_active:
            max_active = len(active_ends)
    unresolved = sorted(
        {
            int(note.get("measure_number") or 0)
            for bucket in simultaneous_conflicts
            for note in starts.get(bucket, [])
            if int(note.get("measure_number") or 0) > 0
        }
        | {m for m in overlap_measures if m > 0}
    )
    hard_fail = bool(simultaneous_conflicts or overlap_conflicts)
    return {
        "hard_fail": hard_fail,
        "max_simultaneous_notes": max_active,
        "simultaneous_conflict_count": len(simultaneous_conflicts),
        "overlap_conflict_count": overlap_conflicts,
        "structural_unresolved_measures": unresolved,
    }


def _summarize_validation_issues(validation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "lyric_coverage_ratio": validation.get("lyric_coverage_ratio"),
        "missing_lyric_sung_note_count": validation.get("missing_lyric_sung_note_count"),
        "unresolved_measures": (validation.get("unresolved_measures") or [])[:10],
    }


def _build_source_timeline(
    notes: Sequence[Dict[str, Any]],
    *,
    verse_number: Optional[str],
    copy_all_verses: bool,
) -> List[Dict[str, Any]]:
    timeline: List[Dict[str, Any]] = []
    for idx, note in enumerate(notes):
        if note.get("is_rest") or not _has_lyric(note):
            continue
        lyric = str(note.get("lyric") or "")
        if not _lyric_matches_requested_verse(
            lyric,
            verse_number=verse_number,
            copy_all_verses=copy_all_verses,
        ):
            continue
        start = round(float(note.get("offset_beats") or 0.0), 6)
        duration = float(note.get("duration_beats") or 0.0)
        end = round(start + duration, 6)
        lyric_confidence = 1.0 if not bool(note.get("lyric_is_extended")) else 0.5
        timeline.append(
            {
                "note": note,
                "source_index": idx,
                "start": start,
                "end": end,
                "duration": max(duration, 0.0001),
                "lyric_confidence": lyric_confidence,
            }
        )
    timeline.sort(key=lambda item: (item["start"], item["source_index"]))
    return timeline


def _choose_overlap_best_match(
    target_note: Dict[str, Any], source_timeline: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    target_start = float(target_note.get("offset_beats") or 0.0)
    target_duration = max(float(target_note.get("duration_beats") or 0.0), 0.0001)
    target_end = target_start + target_duration
    onset_window_beats = max(target_duration, 1.0)

    scored: List[Tuple[float, float, float, int, Dict[str, Any]]] = []
    for item in source_timeline:
        overlap_start = max(target_start, float(item["start"]))
        overlap_end = min(target_end, float(item["end"]))
        overlap_duration = max(0.0, overlap_end - overlap_start)
        if overlap_duration <= 0:
            continue
        overlap_ratio = overlap_duration / target_duration
        onset_delta = abs(target_start - float(item["start"]))
        onset_proximity = max(0.0, 1.0 - (onset_delta / onset_window_beats))
        score = (0.7 * overlap_ratio) + (0.3 * onset_proximity)
        scored.append(
            (
                score,
                float(item["lyric_confidence"]),
                -onset_delta,
                -int(item["source_index"]),
                item,
            )
        )
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][-1]


def _detect_phrase_boundaries(target_notes: Sequence[Dict[str, Any]]) -> set[int]:
    boundaries: set[int] = set()
    last_note_end: Optional[float] = None
    for idx, note in enumerate(target_notes):
        if note.get("is_rest"):
            continue
        start = float(note.get("offset_beats") or 0.0)
        duration = float(note.get("duration_beats") or 0.0)
        if duration >= 4.0:
            boundaries.add(idx + 1)
        if last_note_end is not None and start - last_note_end >= 1.0:
            boundaries.add(idx)
        last_note_end = start + duration
    return boundaries


def _copy_lyric_fields(target_note: Dict[str, Any], source_note: Dict[str, Any]) -> None:
    target_note["lyric"] = source_note.get("lyric")
    target_note["syllabic"] = source_note.get("syllabic")
    target_note["lyric_is_extended"] = bool(source_note.get("lyric_is_extended"))


def _should_apply_policy(note: Dict[str, Any], policy: str) -> bool:
    if policy == "replace_all":
        return True
    if policy in {"fill_missing_only", "preserve_existing"}:
        return not _has_lyric(note)
    return not _has_lyric(note)


def _note_in_measure_range(
    note: Dict[str, Any], measure_range: Optional[Tuple[int, int]]
) -> bool:
    if measure_range is None:
        return True
    start_measure, end_measure = measure_range
    measure = int(note.get("measure_number") or 0)
    return start_measure <= measure <= end_measure


def _note_signature(note: Dict[str, Any]) -> Tuple[float, float, float]:
    return (
        round(float(note.get("offset_beats") or 0.0), 6),
        round(float(note.get("duration_beats") or 0.0), 6),
        round(float(note.get("pitch_midi") or 0.0), 6),
    )


def _feature_flag_enabled(name: str) -> bool:
    raw_value = str(os.environ.get(name, "")).strip().lower()
    return raw_value in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return float(default)
    try:
        parsed = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return float(default)
    if parsed < 0.0:
        return 0.0
    if parsed > 1.0:
        return 1.0
    return parsed


def _env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return int(default)
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return int(default)
    return max(0, parsed)


def _lyric_matches_requested_verse(
    lyric: str,
    *,
    verse_number: Optional[str],
    copy_all_verses: bool,
) -> bool:
    if copy_all_verses:
        return True
    if verse_number is None:
        return True
    parsed_verse = _extract_verse_from_lyric(lyric)
    if parsed_verse is None:
        return True
    return parsed_verse == str(verse_number)


def _persist_transform_metadata(
    score: Dict[str, Any],
    *,
    part_index: int,
    target_voice_part_id: str,
    source_voice_part_id: Optional[str],
    source_part_index: Optional[int] = None,
    transformed_part: Dict[str, Any],
    propagated: bool,
    score_fingerprint: Optional[str] = None,
    transform_hash: Optional[str] = None,
    transform_id: Optional[str] = None,
    appended_part_ref: Optional[Dict[str, Any]] = None,
    modified_musicxml_path: Optional[str] = None,
) -> None:
    cache = score.setdefault("voice_part_transforms", {})
    key_score = score_fingerprint or "-"
    key_hash = transform_hash or "-"
    key = (
        f"part:{part_index}|target:{target_voice_part_id}"
        f"|source_part:{source_part_index if source_part_index is not None else part_index}"
        f"|source:{source_voice_part_id or '-'}"
        f"|score:{key_score}|hash:{key_hash}"
    )
    cache[key] = {
        "part_index": part_index,
        "target_voice_part_id": target_voice_part_id,
        "source_voice_part_id": source_voice_part_id,
        "source_part_index": source_part_index if source_part_index is not None else part_index,
        "propagated_lyrics": propagated,
        "score_fingerprint": score_fingerprint,
        "transform_hash": transform_hash,
        "transform_id": transform_id,
        "appended_part_ref": appended_part_ref,
        "modified_musicxml_path": modified_musicxml_path,
        "part": transformed_part,
    }


def _finalize_transform_result(
    score: Dict[str, Any],
    *,
    part_index: int,
    target_voice_part_id: str,
    source_voice_part_id: Optional[str],
    source_part_index: Optional[int],
    transformed_part: Dict[str, Any],
    propagated: bool,
    status: str,
    warnings: Optional[List[Dict[str, Any]]] = None,
    validation: Optional[Dict[str, Any]] = None,
    source_musicxml_path: Optional[str] = None,
    target_source_voice_id: Optional[str] = None,
) -> Dict[str, Any]:
    score_fingerprint = _compute_score_fingerprint(score)
    transform_payload = {
        "part_index": part_index,
        "target_voice_part_id": target_voice_part_id,
        "source_voice_part_id": source_voice_part_id,
        "source_part_index": source_part_index if source_part_index is not None else part_index,
        "propagated": propagated,
        "notes": transformed_part.get("notes") or [],
    }
    transform_hash = hashlib.sha256(
        json.dumps(
            transform_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    transform_id = f"vp:part{part_index}:{target_voice_part_id}:{transform_hash[:12]}"
    derived_part_id = _derived_part_id(transform_hash)
    derived_part_name = _build_derived_part_name(
        score=score,
        part_index=part_index,
        target_voice_part_id=target_voice_part_id,
    )
    hidden_default_lane = (
        str(target_source_voice_id or "").strip() == "_default"
        or _is_hidden_default_lane(
            score=score,
            part_index=part_index,
            target_voice_part_id=target_voice_part_id,
        )
    )

    artifact_key = f"{score_fingerprint}:{transform_hash}"
    lock_key = f"{source_musicxml_path or 'memory'}:{artifact_key}"
    appended_part_ref: Optional[Dict[str, Any]] = None
    modified_musicxml_path: Optional[str] = None
    reused_transform = False

    with _get_artifact_lock(lock_key):
        existing = _TRANSFORM_ARTIFACT_INDEX.get(artifact_key)
        if existing:
            existing_path = existing.get("modified_musicxml_path")
            if isinstance(existing_path, str) and existing_path and Path(existing_path).exists():
                appended_part_ref = dict(existing.get("appended_part_ref") or {})
                modified_musicxml_path = existing_path
                reused_transform = True

        if not hidden_default_lane and not reused_transform and source_musicxml_path:
            materialized = _materialize_transformed_part(
                source_musicxml_path=source_musicxml_path,
                transformed_part=transformed_part,
                part_id=derived_part_id,
                part_name=derived_part_name,
                transform_hash=transform_hash,
            )
            if materialized.get("status") == "ready":
                appended_part_ref = materialized.get("appended_part_ref")
                modified_musicxml_path = materialized.get("modified_musicxml_path")

        if hidden_default_lane:
            appended_part_ref = {
                "part_id": derived_part_id,
                "part_name": derived_part_name,
                "part_index": part_index,
                "hidden_default_lane": True,
            }
        elif appended_part_ref is None:
            appended_part_ref = {
                "part_id": derived_part_id,
                "part_name": derived_part_name,
            }
        if not hidden_default_lane:
            appended_part_ref = _ensure_derived_part_in_score(
                score,
                transformed_part=transformed_part,
                appended_part_ref=appended_part_ref,
            )
        _TRANSFORM_ARTIFACT_INDEX[artifact_key] = {
            "transform_id": transform_id,
            "transform_hash": transform_hash,
            "score_fingerprint": score_fingerprint,
            "appended_part_ref": appended_part_ref,
            "modified_musicxml_path": modified_musicxml_path,
        }

    _persist_transform_metadata(
        score,
        part_index=part_index,
        target_voice_part_id=target_voice_part_id,
        source_voice_part_id=source_voice_part_id,
        source_part_index=source_part_index,
        transformed_part=transformed_part,
        propagated=propagated,
        score_fingerprint=score_fingerprint,
        transform_hash=transform_hash,
        transform_id=transform_id,
        appended_part_ref=appended_part_ref,
        modified_musicxml_path=modified_musicxml_path,
    )
    if isinstance(modified_musicxml_path, str) and modified_musicxml_path:
        score["source_musicxml_path"] = modified_musicxml_path

    result: Dict[str, Any] = {
        "status": status,
        "score": score,
        "part_index": int(appended_part_ref.get("part_index", part_index)),
        "transform_id": transform_id,
        "score_fingerprint": score_fingerprint,
        "transform_hash": transform_hash,
        "appended_part_ref": appended_part_ref,
        "modified_musicxml_path": modified_musicxml_path,
        "reused_transform": reused_transform,
        "hidden_default_lane": hidden_default_lane,
    }
    if warnings:
        result["warnings"] = warnings
    if validation:
        result["validation"] = validation
    return result


def _get_artifact_lock(lock_key: str) -> threading.Lock:
    with _ARTIFACT_LOCKS_GUARD:
        lock = _ARTIFACT_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _ARTIFACT_LOCKS[lock_key] = lock
        return lock


def _compute_score_fingerprint(score: Dict[str, Any]) -> str:
    canonical = {
        "title": score.get("title"),
        "tempos": score.get("tempos"),
        "parts": score.get("parts"),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _derived_part_id(transform_hash: str) -> str:
    return f"P_DERIVED_{transform_hash[:10].upper()}"


def _build_derived_part_name(
    *, score: Dict[str, Any], part_index: int, target_voice_part_id: str
) -> str:
    parts = score.get("parts") or []
    source_part_name = ""
    source_part_id = ""
    if 0 <= part_index < len(parts):
        source_part_name = str(parts[part_index].get("part_name") or "").strip()
        source_part_id = str(parts[part_index].get("part_id") or "").strip()
    if source_part_name and not re.match(r"^voice\s+part\s+\d+$", source_part_name, re.IGNORECASE):
        return f"{source_part_name} - {target_voice_part_id} (Derived)"
    if source_part_id:
        return f"{source_part_id} - {target_voice_part_id} (Derived)"
    return f"Part {part_index + 1} - {target_voice_part_id} (Derived)"


def _is_hidden_default_lane(
    *, score: Dict[str, Any], part_index: int, target_voice_part_id: str
) -> bool:
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return False
    analysis = _analyze_part_voice_parts(parts[part_index], part_index)
    target_meta = _find_voice_part_by_id(analysis.get("voice_parts") or [], target_voice_part_id)
    if target_meta is None:
        return False
    return str(target_meta.get("source_voice_id") or "") == "_default"


def _resolve_source_musicxml_path(score: Dict[str, Any]) -> Optional[str]:
    raw_path = score.get("source_musicxml_path")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path
    return None


def _ensure_derived_part_in_score(
    score: Dict[str, Any],
    *,
    transformed_part: Dict[str, Any],
    appended_part_ref: Dict[str, Any],
) -> Dict[str, Any]:
    parts = score.setdefault("parts", [])
    part_id = str(appended_part_ref.get("part_id") or "").strip()
    part_name = str(
        appended_part_ref.get("part_name")
        or transformed_part.get("part_name")
        or "Derived Voice Part"
    )
    if not part_id:
        part_id = _derived_part_id(
            hashlib.sha256(
                json.dumps(transformed_part, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
        )
    derived_part = {
        "part_id": part_id,
        "part_name": part_name,
        "notes": [dict(note) for note in transformed_part.get("notes") or []],
    }

    existing_index = next(
        (idx for idx, part in enumerate(parts) if str(part.get("part_id")) == part_id),
        None,
    )
    if existing_index is None:
        parts.append(derived_part)
        existing_index = len(parts) - 1
    else:
        parts[existing_index] = derived_part

    out = dict(appended_part_ref)
    out["part_id"] = part_id
    out["part_name"] = part_name
    out["part_index"] = existing_index
    return out


def _materialize_transformed_part(
    *,
    source_musicxml_path: str,
    transformed_part: Dict[str, Any],
    part_id: str,
    part_name: str,
    transform_hash: str,
) -> Dict[str, Any]:
    source_path = Path(source_musicxml_path)
    if not source_path.exists():
        return {
            "status": "error",
            "action": "musicxml_append_failed",
            "message": f"source MusicXML does not exist: {source_musicxml_path}",
        }
    try:
        tree = ET.parse(source_path)
    except Exception as exc:
        return {
            "status": "error",
            "action": "musicxml_append_failed",
            "message": f"failed to parse source MusicXML: {exc}",
        }
    root = tree.getroot()
    ns = _xml_namespace(root.tag)
    q = lambda tag: f"{{{ns}}}{tag}" if ns else tag

    part_list = root.find(q("part-list"))
    if part_list is None:
        part_list = ET.Element(q("part-list"))
        root.insert(0, part_list)
    if part_list.find(f"{q('score-part')}[@id='{part_id}']") is None:
        score_part = ET.SubElement(part_list, q("score-part"), {"id": part_id})
        ET.SubElement(score_part, q("part-name")).text = part_name

    existing_part = root.find(f"{q('part')}[@id='{part_id}']")
    if existing_part is not None:
        root.remove(existing_part)
    derived_part = ET.SubElement(root, q("part"), {"id": part_id})

    source_parts = root.findall(q("part"))
    reference_part = _resolve_reference_part_for_transformed(
        source_parts=source_parts,
        transformed_part=transformed_part,
        derived_part_id=part_id,
    )
    divisions = _resolve_divisions(reference_part, q) if reference_part is not None else 1
    _append_transformed_measures(
        derived_part,
        transformed_notes=transformed_part.get("notes") or [],
        divisions=divisions,
        reference_part=reference_part,
        q=q,
    )

    output_stem = _normalize_materialized_musicxml_stem(source_path.stem)
    output_path = source_path.with_name(f"{output_stem}.derived_{transform_hash[:10]}.xml")
    try:
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
    except Exception:
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"{source_path.stem}.derived_{transform_hash[:10]}.xml"
        tree.write(output_path, encoding="utf-8", xml_declaration=True)

    return {
        "status": "ready",
        "modified_musicxml_path": str(output_path),
        "appended_part_ref": {
            "part_id": part_id,
            "part_name": part_name,
        },
    }


def _normalize_materialized_musicxml_stem(stem: str) -> str:
    """Strip trailing .derived_<hash> segments to avoid double-derived filenames."""
    normalized = stem
    while True:
        updated = _DERIVED_STEM_SUFFIX_RE.sub("", normalized)
        if updated == normalized:
            return normalized
        normalized = updated


def _resolve_reference_part_for_transformed(
    *,
    source_parts: Sequence[ET.Element],
    transformed_part: Dict[str, Any],
    derived_part_id: str,
) -> Optional[ET.Element]:
    source_part_index = transformed_part.get("_source_part_index")
    if isinstance(source_part_index, int) and 0 <= source_part_index < len(source_parts):
        candidate = source_parts[source_part_index]
        if candidate.get("id") != derived_part_id:
            return candidate
    transformed_part_id = str(transformed_part.get("part_id") or "").strip()
    if transformed_part_id:
        for candidate in source_parts:
            if candidate.get("id") == transformed_part_id:
                return candidate
    for candidate in source_parts:
        if candidate.get("id") != derived_part_id:
            return candidate
    return None


def _xml_namespace(tag: str) -> Optional[str]:
    if tag.startswith("{") and "}" in tag:
        return tag[1 : tag.index("}")]
    return None


def _resolve_divisions(reference_part: ET.Element, q: Any) -> int:
    for measure in reference_part.findall(q("measure")):
        attributes = measure.find(q("attributes"))
        if attributes is None:
            continue
        divisions_node = attributes.find(q("divisions"))
        if divisions_node is None or not divisions_node.text:
            continue
        try:
            value = int(divisions_node.text)
            if value > 0:
                return value
        except Exception:
            continue
    return 1


def _append_transformed_measures(
    part_node: ET.Element,
    *,
    transformed_notes: Sequence[Dict[str, Any]],
    divisions: int,
    reference_part: Optional[ET.Element],
    q: Any,
) -> None:
    by_measure: Dict[int, List[Dict[str, Any]]] = {}
    for note in transformed_notes:
        measure = int(note.get("measure_number") or 0)
        by_measure.setdefault(measure, []).append(note)
    reference_measures = (
        [m for m in reference_part.findall(q("measure"))] if reference_part is not None else []
    )
    if reference_measures:
        ordered_measures = reference_measures
    else:
        ordered_measures = [
            ET.Element(q("measure"), {"number": str(num)})
            for num in sorted(by_measure)
        ]

    current_time = {"beats": 4, "beat_type": 4}
    for idx, ref_measure in enumerate(ordered_measures):
        measure_number = ref_measure.attrib.get("number") or str(idx + 1)
        measure_int = int(measure_number) if measure_number.isdigit() else idx + 1
        measure_node = ET.SubElement(part_node, q("measure"), {"number": str(measure_number)})

        ref_attributes = ref_measure.find(q("attributes"))
        if ref_attributes is not None:
            measure_node.append(ET.fromstring(ET.tostring(ref_attributes)))
        elif idx == 0:
            attributes = ET.SubElement(measure_node, q("attributes"))
            ET.SubElement(attributes, q("divisions")).text = str(divisions)

        _update_time_signature(ref_attributes, q, current_time)
        notes = sorted(
            by_measure.get(measure_int, []),
            key=lambda row: (float(row.get("offset_beats") or 0.0), float(row.get("pitch_midi") or 0.0)),
        )
        if not notes:
            duration_beats = (
                float(current_time["beats"]) * (4.0 / float(current_time["beat_type"]))
            )
            duration_div = max(1, int(round(duration_beats * float(divisions))))
            rest_node = ET.SubElement(measure_node, q("note"))
            ET.SubElement(rest_node, q("rest"))
            ET.SubElement(rest_node, q("duration")).text = str(duration_div)
            ET.SubElement(rest_node, q("voice")).text = "1"
            ET.SubElement(rest_node, q("type")).text = _duration_to_type(duration_beats)
            continue

        for note in notes:
            note_node = ET.SubElement(measure_node, q("note"))
            if note.get("is_rest"):
                ET.SubElement(note_node, q("rest"))
            else:
                pitch_node = ET.SubElement(note_node, q("pitch"))
                step = str(note.get("pitch_step") or "").strip()
                alter_raw = note.get("pitch_alter")
                octave_raw = note.get("pitch_octave")
                if step and octave_raw is not None:
                    alter = int(alter_raw) if alter_raw is not None else 0
                    octave = int(octave_raw)
                else:
                    step, alter, octave = _midi_to_pitch_components(
                        float(note.get("pitch_midi") or 60.0)
                    )
                ET.SubElement(pitch_node, q("step")).text = step
                if alter != 0:
                    ET.SubElement(pitch_node, q("alter")).text = str(alter)
                ET.SubElement(pitch_node, q("octave")).text = str(octave)
            duration_div = max(
                1, int(round(float(note.get("duration_beats") or 1.0) * float(divisions)))
            )
            ET.SubElement(note_node, q("duration")).text = str(duration_div)
            ET.SubElement(note_node, q("voice")).text = str(note.get("voice") or "1")
            ET.SubElement(note_node, q("type")).text = _duration_to_type(
                float(note.get("duration_beats") or 1.0)
            )
            lyric = note.get("lyric")
            if isinstance(lyric, str) and lyric.strip():
                lyric_node = ET.SubElement(note_node, q("lyric"))
                ET.SubElement(lyric_node, q("text")).text = lyric


def _update_time_signature(
    attributes: Optional[ET.Element], q: Any, current_time: Dict[str, int]
) -> None:
    if attributes is None:
        return
    time_node = attributes.find(q("time"))
    if time_node is None:
        return
    beats_node = time_node.find(q("beats"))
    beat_type_node = time_node.find(q("beat-type"))
    if beats_node is not None and beats_node.text and beats_node.text.strip().isdigit():
        current_time["beats"] = int(beats_node.text.strip())
    if (
        beat_type_node is not None
        and beat_type_node.text
        and beat_type_node.text.strip().isdigit()
    ):
        current_time["beat_type"] = int(beat_type_node.text.strip())


def _midi_to_pitch_components(midi: float) -> Tuple[str, int, int]:
    midi_int = int(round(midi))
    octave = (midi_int // 12) - 1
    classes = [
        ("C", 0),
        ("C", 1),
        ("D", 0),
        ("D", 1),
        ("E", 0),
        ("F", 0),
        ("F", 1),
        ("G", 0),
        ("G", 1),
        ("A", 0),
        ("A", 1),
        ("B", 0),
    ]
    step, alter = classes[midi_int % 12]
    return step, alter, octave


def _duration_to_type(duration_beats: float) -> str:
    if duration_beats >= 4.0:
        return "whole"
    if duration_beats >= 2.0:
        return "half"
    if duration_beats >= 1.0:
        return "quarter"
    if duration_beats >= 0.5:
        return "eighth"
    return "16th"


def _collect_global_lyric_source_options(
    score: Dict[str, Any], *, exclude_part_index: int
) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    seen = set()
    parts = score.get("parts") or []
    for idx, part in enumerate(parts):
        if idx == exclude_part_index:
            continue
        analysis = _analyze_part_voice_parts(part, idx)
        for vp in analysis["voice_parts"]:
            if vp["lyric_note_count"] <= 0:
                continue
            key = (idx, str(vp["voice_part_id"]))
            if key in seen:
                continue
            seen.add(key)
            options.append(_make_source_option(idx, str(vp["voice_part_id"])))
    return options


def _collect_all_lyric_source_options(score: Dict[str, Any]) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    seen = set()
    parts = score.get("parts") or []
    for idx, part in enumerate(parts):
        analysis = _analyze_part_voice_parts(part, idx)
        for vp in analysis["voice_parts"]:
            if vp["lyric_note_count"] <= 0:
                continue
            key = (idx, str(vp["voice_part_id"]))
            if key in seen:
                continue
            seen.add(key)
            options.append(_make_source_option(idx, str(vp["voice_part_id"])))
    return options


def _build_measure_lyric_coverage(
    part: Dict[str, Any], voice_parts: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    by_source_voice = {
        str(vp["source_voice_id"]): vp["voice_part_id"] for vp in voice_parts
    }
    per_measure: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for note in part.get("notes") or []:
        if note.get("is_rest"):
            continue
        measure = int(note.get("measure_number") or 0)
        source_voice = _voice_key(note.get("voice"))
        voice_part_id = by_source_voice.get(source_voice)
        if voice_part_id is None:
            continue
        measure_bucket = per_measure.setdefault(measure, {})
        stats = measure_bucket.setdefault(
            voice_part_id,
            {
                "voice_part_id": voice_part_id,
                "sung_note_count": 0,
                "lyric_note_count": 0,
                "word_lyric_note_count": 0,
                "extension_lyric_note_count": 0,
                "empty_lyric_note_count": 0,
                "verses_present": [],
            },
        )
        stats["sung_note_count"] += 1
        lyric_kind = _lyric_kind(note)
        if lyric_kind == "word":
            stats["word_lyric_note_count"] += 1
            stats["lyric_note_count"] += 1
            verse = _extract_verse_from_lyric(note.get("lyric"))
            if verse and verse not in stats["verses_present"]:
                stats["verses_present"].append(verse)
        elif lyric_kind == "extension":
            stats["extension_lyric_note_count"] += 1
            stats["lyric_note_count"] += 1
        else:
            stats["empty_lyric_note_count"] += 1

    rows: List[Dict[str, Any]] = []
    for measure in sorted(per_measure):
        voice_stats = []
        for voice_part_id in sorted(per_measure[measure]):
            stats = per_measure[measure][voice_part_id]
            sung = int(stats["sung_note_count"])
            lyric = int(stats["lyric_note_count"])
            word_lyric = int(stats["word_lyric_note_count"])
            extension_lyric = int(stats["extension_lyric_note_count"])
            empty_lyric = int(stats["empty_lyric_note_count"])
            ratio = float(lyric) / float(sung) if sung > 0 else 0.0
            word_ratio = float(word_lyric) / float(sung) if sung > 0 else 0.0
            extension_ratio = float(extension_lyric) / float(sung) if sung > 0 else 0.0
            voice_stats.append(
                {
                    "voice_part_id": voice_part_id,
                    "sung_note_count": sung,
                    "lyric_note_count": lyric,
                    "word_lyric_note_count": word_lyric,
                    "extension_lyric_note_count": extension_lyric,
                    "empty_lyric_note_count": empty_lyric,
                    "lyric_coverage_ratio": round(ratio, 4),
                    "word_lyric_coverage_ratio": round(word_ratio, 4),
                    "extension_lyric_ratio": round(extension_ratio, 4),
                    "verses_present": sorted(stats["verses_present"]),
                }
            )
        rows.append({"measure_number": measure, "voice_parts": voice_stats})
    return rows


def _build_source_candidate_hints(
    score: Dict[str, Any],
    *,
    target_part_index: int,
    voice_parts: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    parts = score.get("parts") or []
    if target_part_index < 0 or target_part_index >= len(parts):
        return []

    target_part = parts[target_part_index]
    measure_min, measure_max = _get_measure_span(target_part.get("notes") or [])
    hints: List[Dict[str, Any]] = []
    for target_voice in voice_parts:
        target_notes = _select_part_notes_for_voice(
            target_part.get("notes") or [],
            target_voice["source_voice_id"],
        )
        target_offsets = {
            round(float(note.get("offset_beats") or 0.0), 6)
            for note in target_notes
            if not note.get("is_rest")
        }
        ranked_sources: List[Dict[str, Any]] = []
        for candidate in _collect_all_lyric_source_options(score):
            source_part_index = int(candidate["source_part_index"])
            source_voice_part_id = str(candidate["source_voice_part_id"])
            source_part = parts[source_part_index]
            source_analysis = _analyze_part_voice_parts(source_part, source_part_index)
            source_meta = _find_voice_part_by_id(
                source_analysis["voice_parts"], source_voice_part_id
            )
            if source_meta is None or source_meta["lyric_note_count"] <= 0:
                continue
            source_notes = _select_part_notes_for_voice(
                source_part.get("notes") or [],
                source_meta["source_voice_id"],
            )
            lyric_offsets = {
                round(float(note.get("offset_beats") or 0.0), 6)
                for note in source_notes
                if not note.get("is_rest") and _has_lyric(note)
            }
            if not lyric_offsets:
                continue
            if target_offsets:
                overlap_ratio = len(target_offsets & lyric_offsets) / len(target_offsets)
            else:
                overlap_ratio = 0.0
            source_lyric_density = (
                float(source_meta["lyric_note_count"]) / float(source_meta["note_count"])
                if source_meta["note_count"] > 0
                else 0.0
            )
            rank_score = (0.7 * overlap_ratio) + (0.3 * source_lyric_density)
            ranked_sources.append(
                {
                    "part_index": source_part_index,
                    "voice_part_id": source_voice_part_id,
                    "rank_score": round(rank_score, 4),
                    "overlap_ratio": round(overlap_ratio, 4),
                    "lyric_density": round(source_lyric_density, 4),
                }
            )
        ranked_sources.sort(
            key=lambda row: (
                -float(row["rank_score"]),
                int(row["part_index"]),
                str(row["voice_part_id"]),
            )
        )
        hints.append(
            {
                "target_voice_part_id": target_voice["voice_part_id"],
                "start_measure": measure_min,
                "end_measure": measure_max,
                "ranked_sources": ranked_sources[:6],
            }
        )
    return hints


def _get_measure_span(notes: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    measures = sorted(
        int(note.get("measure_number") or 0)
        for note in notes
        if int(note.get("measure_number") or 0) > 0
    )
    if not measures:
        return 0, 0
    return measures[0], measures[-1]


def _make_source_option(source_part_index: int, source_voice_part_id: str) -> Dict[str, Any]:
    return {
        "source_part_index": source_part_index,
        "source_voice_part_id": source_voice_part_id,
    }


def _match_source_option(
    options: Sequence[Dict[str, Any]],
    *,
    source_part_index: int,
    source_voice_part_id: str,
) -> Optional[Dict[str, Any]]:
    for option in options:
        if (
            int(option.get("source_part_index", -1)) == int(source_part_index)
            and str(option.get("source_voice_part_id", "")).strip().lower()
            == str(source_voice_part_id).strip().lower()
        ):
            return option
    return None


def _normalize_section_overrides(
    raw_overrides: Any,
    *,
    target_ref: Dict[str, Any],
    score: Optional[Dict[str, Any]],
    field_name: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if raw_overrides is None:
        return [], None
    if not isinstance(raw_overrides, list):
        return [], _plan_error(
            "invalid_plan_payload",
            f"{field_name} must be a list.",
        )["error"]

    target_part_index = int(target_ref["part_index"])
    part_span = _resolve_part_measure_span(score, target_part_index)
    normalized: List[Dict[str, Any]] = []
    for idx, raw_override in enumerate(raw_overrides):
        if not isinstance(raw_override, dict):
            return [], _plan_error(
                "invalid_section_override",
                f"{field_name}[{idx}] must be an object.",
            )["error"]
        start_measure = raw_override.get("start_measure")
        end_measure = raw_override.get("end_measure")
        if not isinstance(start_measure, int) or not isinstance(end_measure, int):
            return [], _plan_error(
                "invalid_section_override",
                f"{field_name}[{idx}] start_measure/end_measure must be integers.",
            )["error"]
        if start_measure > end_measure:
            return [], _plan_error(
                "invalid_section_override",
                f"{field_name}[{idx}] start_measure cannot exceed end_measure.",
            )["error"]
        if part_span is not None:
            span_start, span_end = part_span
            if start_measure < span_start or end_measure > span_end:
                return [], _plan_error(
                    "invalid_section_override",
                    (
                        f"{field_name}[{idx}] range {start_measure}-{end_measure} is out of "
                        f"bounds for target part ({span_start}-{span_end})."
                    ),
                )["error"]
        source_ref, source_error = _normalize_voice_ref(
            raw_override.get("source"),
            field_name=f"{field_name}[{idx}].source",
            allow_voice_id=False,
        )
        if source_error is not None:
            return [], source_error
        strategy = str(raw_override.get("strategy", "strict_onset")).strip()
        policy = str(raw_override.get("policy", "fill_missing_only")).strip()
        if strategy not in VALID_PROPAGATION_STRATEGIES:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{field_name}[{idx}] strategy is invalid: {strategy}",
            )["error"]
        if policy not in VALID_PROPAGATION_POLICIES:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{field_name}[{idx}] policy is invalid: {policy}",
            )["error"]
        normalized.append(
            {
                "start_measure": int(start_measure),
                "end_measure": int(end_measure),
                "source": source_ref,
                "strategy": strategy,
                "policy": policy,
                "precedence": idx,
            }
        )
    return normalized, None


def _normalize_timeline_sections(
    raw_sections: Any,
    *,
    target_ref: Dict[str, Any],
    score: Optional[Dict[str, Any]],
    field_name: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not isinstance(raw_sections, list) or not raw_sections:
        return [], _plan_error(
            "invalid_plan_payload",
            f"{field_name} must be a non-empty list.",
        )["error"]

    target_part_index = int(target_ref["part_index"])
    part_span = _resolve_part_measure_span(score, target_part_index)
    max_measure = part_span[1] if part_span is not None else None
    normalized: List[Dict[str, Any]] = []
    prev_end = 0
    for idx, raw_section in enumerate(raw_sections):
        section_name = f"{field_name}[{idx}]"
        if not isinstance(raw_section, dict):
            return [], _plan_error(
                "invalid_plan_payload",
                f"{section_name} must be an object.",
            )["error"]
        start_measure = raw_section.get("start_measure")
        end_measure = raw_section.get("end_measure")
        if not isinstance(start_measure, int) or not isinstance(end_measure, int):
            return [], _plan_error(
                "invalid_section_range",
                f"{section_name} start_measure/end_measure must be integers.",
            )["error"]
        if start_measure < 1 or end_measure < 1:
            return [], _plan_error(
                "invalid_section_range",
                f"{section_name} start_measure/end_measure must be >= 1.",
            )["error"]
        if start_measure > end_measure:
            return [], _plan_error(
                "invalid_section_range",
                f"{section_name} start_measure cannot exceed end_measure.",
            )["error"]
        if max_measure is not None and end_measure > max_measure:
            return [], _plan_error(
                "invalid_section_range",
                f"{section_name} end_measure exceeds target part max measure {max_measure}.",
            )["error"]
        if prev_end and start_measure <= prev_end:
            return [], _plan_error(
                "overlapping_sections",
                f"{section_name} overlaps with the previous section.",
            )["error"]
        expected_start = 1 if idx == 0 else prev_end + 1
        if start_measure != expected_start:
            return [], _plan_error(
                "non_contiguous_sections",
                (
                    f"{section_name} must start at measure {expected_start} "
                    "for contiguous coverage."
                ),
            )["error"]
        mode = str(raw_section.get("mode") or "").strip().lower()
        if mode not in VALID_TIMELINE_SECTION_MODES:
            return [], _plan_error(
                "invalid_section_mode",
                f"{section_name} mode must be one of: {sorted(VALID_TIMELINE_SECTION_MODES)}.",
            )["error"]

        decision_type = str(
            raw_section.get("decision_type") or "EXTRACT_FROM_VOICE"
        ).strip()
        if decision_type not in VALID_TIMELINE_DECISION_TYPES:
            return [], _plan_error(
                "invalid_section_mode",
                f"{section_name} decision_type is invalid: {decision_type}",
            )["error"]
        method = str(raw_section.get("method") or "trivial").strip()
        if method not in PUBLIC_TIMELINE_METHODS:
            return [], _plan_error(
                "invalid_section_mode",
                f"{section_name} method is invalid: {method}. Allowed methods: {sorted(PUBLIC_TIMELINE_METHODS)}.",
            )["error"]
        raw_rank_index = raw_section.get("rank_index", 0)
        try:
            rank_index = int(raw_rank_index)
        except (TypeError, ValueError):
            return [], _plan_error(
                "invalid_plan_enum",
                f"{section_name} rank_index must be an integer.",
            )["error"]
        if rank_index < 0:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{section_name} rank_index must be >= 0.",
            )["error"]
        rank_fallback = str(raw_section.get("rank_fallback") or "greedy").strip().lower()
        if rank_fallback not in VALID_RANK_FALLBACKS:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{section_name} rank_fallback is invalid: {rank_fallback}",
            )["error"]

        melody_raw = raw_section.get("melody_source")
        lyric_raw = raw_section.get("lyric_source")
        melody_source = None
        lyric_source = None

        if mode == "rest":
            if melody_raw is not None or lyric_raw is not None:
                return [], _plan_error(
                    "invalid_plan_payload",
                    f"{section_name} rest mode cannot include source fields.",
                )["error"]
        else:
            if melody_raw is None and lyric_raw is None:
                return [], _plan_error(
                    "empty_section_source",
                    f"{section_name} derive mode must include melody_source and/or lyric_source.",
                )["error"]
            if melody_raw is not None:
                melody_source, melody_error = _normalize_voice_ref(
                    melody_raw,
                    field_name=f"{section_name}.melody_source",
                    allow_voice_id=False,
                )
                if melody_error is not None:
                    return [], _plan_error(
                        "malformed_section_source",
                        f"{section_name}.melody_source is invalid.",
                    )["error"]
            if lyric_raw is not None:
                lyric_source, lyric_error = _normalize_voice_ref(
                    lyric_raw,
                    field_name=f"{section_name}.lyric_source",
                    allow_voice_id=False,
                )
                if lyric_error is not None:
                    return [], _plan_error(
                        "malformed_section_source",
                        f"{section_name}.lyric_source is invalid.",
                    )["error"]

        strategy = str(raw_section.get("lyric_strategy", "strict_onset")).strip()
        policy = str(raw_section.get("lyric_policy", "fill_missing_only")).strip()
        if strategy not in VALID_PROPAGATION_STRATEGIES:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{section_name} lyric_strategy is invalid: {strategy}",
            )["error"]
        if policy not in VALID_PROPAGATION_POLICIES:
            return [], _plan_error(
                "invalid_plan_enum",
                f"{section_name} lyric_policy is invalid: {policy}",
            )["error"]
        normalized.append(
            {
                "start_measure": int(start_measure),
                "end_measure": int(end_measure),
                "mode": mode,
                "decision_type": decision_type,
                "method": method,
                "rank_index": rank_index,
                "rank_fallback": rank_fallback,
                "melody_source": melody_source,
                "lyric_source": lyric_source,
                "lyric_strategy": strategy,
                "lyric_policy": policy,
            }
        )
        prev_end = end_measure

    if max_measure is not None and prev_end != max_measure:
        return [], _plan_error(
            "non_contiguous_sections",
            (
                f"{field_name} must end at target part max measure {max_measure}; "
                f"got {prev_end}."
            ),
        )["error"]
    return normalized, None


def _resolve_part_measure_span(
    score: Optional[Dict[str, Any]], part_index: int
) -> Optional[Tuple[int, int]]:
    if not score:
        return None
    parts = score.get("parts") or []
    if part_index < 0 or part_index >= len(parts):
        return None
    return _get_measure_span(parts[part_index].get("notes") or [])


def _normalize_voice_ref(
    raw_ref: Any,
    *,
    field_name: str,
    allow_voice_id: bool,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if not isinstance(raw_ref, dict):
        return {}, _plan_error(
            "invalid_plan_target_ref",
            f"{field_name} must be an object.",
        )["error"]

    raw_part_index = raw_ref.get("part_index")
    if not isinstance(raw_part_index, int):
        return {}, _plan_error(
            "invalid_plan_target_ref",
            f"{field_name}.part_index must be an integer.",
        )["error"]

    voice_part_id = raw_ref.get("voice_part_id")
    if not isinstance(voice_part_id, str) or not voice_part_id.strip():
        if allow_voice_id:
            raw_voice_id = raw_ref.get("voice_id")
            if isinstance(raw_voice_id, str) and raw_voice_id.strip():
                voice_part_id = raw_voice_id
            else:
                return {}, _plan_error(
                    "invalid_plan_target_ref",
                    f"{field_name}.voice_part_id is required.",
                )["error"]
        else:
            return {}, _plan_error(
                "invalid_plan_target_ref",
                f"{field_name}.voice_part_id is required.",
            )["error"]

    return {
        "part_index": int(raw_part_index),
        "voice_part_id": str(voice_part_id).strip(),
    }, None


def _plan_error(action: str, message: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": _action_required(action, message, code=action),
    }


def _action_required(action: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": "action_required",
        "action": action,
        "message": message,
    }
    payload.update(extra)
    return payload


def build_infeasible_anchor_action_required(
    *,
    error: Exception,
    part_index: Optional[int] = None,
    voice_id: Optional[str] = None,
    voice_part_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a user-facing action_required payload for timing infeasibility."""
    details: Dict[str, Any] = {"raw_error": str(error)}
    to_payload = getattr(error, "to_payload", None)
    if callable(to_payload):
        payload = to_payload()
        if isinstance(payload, dict):
            details.update(payload)
    if part_index is not None:
        details["part_index"] = int(part_index)
    if voice_id:
        details["voice_id"] = str(voice_id)
    if voice_part_id:
        details["voice_part_id"] = str(voice_part_id)
    return _action_required(
        "infeasible_anchor_budget",
        (
            "Timing allocation is infeasible for at least one lyric group "
            "(phoneme count exceeds available anchor frames)."
        ),
        code="infeasible_anchor_budget",
        **details,
    )


def build_preprocessing_required_action(
    *,
    part_index: int,
    reason: str,
    failed_validation_rules: Optional[List[str]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a user-facing action_required payload for preprocessing requirements."""
    payload = _action_required(
        "preprocessing_required",
        message
        or (
            "This part requires voice-part preprocessing before synthesis. "
            "Run preprocess_voice_parts first, then synthesize the derived target."
        ),
        code="preprocessing_required",
        part_index=int(part_index),
        reason=reason,
    )
    if failed_validation_rules:
        payload["failed_validation_rules"] = list(failed_validation_rules)
    if diagnostics:
        payload["diagnostics"] = dict(diagnostics)
    return payload


def _collect_derived_target_candidates(score: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect derived target refs from persisted transform metadata."""
    transforms = score.get("voice_part_transforms")
    if not isinstance(transforms, dict) or not transforms:
        return []
    parts = score.get("parts") or []
    candidates: List[Dict[str, Any]] = []
    seen: set[Tuple[Any, ...]] = set()
    for value in transforms.values():
        if not isinstance(value, dict):
            continue
        appended_ref = value.get("appended_part_ref")
        if not isinstance(appended_ref, dict):
            continue
        part_index = appended_ref.get("part_index")
        if not isinstance(part_index, int):
            continue
        part_id = str(appended_ref.get("part_id") or "").strip()
        part_name = str(appended_ref.get("part_name") or "").strip()
        if (not part_id or not part_name) and 0 <= part_index < len(parts):
            part = parts[part_index]
            if not part_id:
                part_id = str(part.get("part_id") or "").strip()
            if not part_name:
                part_name = str(part.get("part_name") or "").strip()
        target_voice_part_id = str(value.get("target_voice_part_id") or "").strip()
        source_part_index = value.get("source_part_index")
        source_voice_part_id = str(value.get("source_voice_part_id") or "").strip()
        key = (
            part_index,
            part_id,
            part_name,
            target_voice_part_id,
            source_part_index if isinstance(source_part_index, int) else None,
            source_voice_part_id,
        )
        if key in seen:
            continue
        seen.add(key)
        candidate: Dict[str, Any] = {
            "part_index": part_index,
            "part_id": part_id or None,
            "part_name": part_name or None,
            "target_voice_part_id": target_voice_part_id or None,
        }
        if isinstance(source_part_index, int):
            candidate["source_part_index"] = source_part_index
        if source_voice_part_id:
            candidate["source_voice_part_id"] = source_voice_part_id
        candidates.append(candidate)
    candidates.sort(
        key=lambda c: (
            int(c.get("part_index", -1)),
            str(c.get("target_voice_part_id") or ""),
            str(c.get("part_id") or ""),
        )
    )
    return candidates


def synthesize_preflight_action_required(
    score: Dict[str, Any],
    *,
    part_index: int,
) -> Optional[Dict[str, Any]]:
    """Return action_required when a complex raw part is synthesized without preprocessing."""
    parts = score.get("parts") or []
    diagnostics: Dict[str, Any] = {
        "input_part_index": int(part_index),
        "parts_count": len(parts),
    }
    if part_index < 0 or part_index >= len(parts):
        diagnostics["part_index_in_range"] = False
        return build_preprocessing_required_action(
            part_index=part_index,
            reason="target_part_not_found_for_preflight",
            failed_validation_rules=["input_validation.part_index_out_of_range"],
            diagnostics=diagnostics,
        )
    diagnostics["part_index_in_range"] = True

    part = parts[part_index]
    diagnostics["part_type"] = type(part).__name__
    part_id = str(part.get("part_id") or "").strip()
    part_name = str(part.get("part_name") or "").strip()
    derived_by_marker = part_id.startswith("P_DERIVED_") or "(Derived)" in part_name
    diagnostics["part_id"] = part_id
    diagnostics["part_name"] = part_name
    diagnostics["derived_by_marker"] = bool(derived_by_marker)
    if derived_by_marker:
        return None

    score_summary = score.get("score_summary") if isinstance(score.get("score_summary"), dict) else {}
    summary_parts = score_summary.get("parts") if isinstance(score_summary, dict) else None
    original_part_count = len(summary_parts) if isinstance(summary_parts, list) and summary_parts else len(parts)
    diagnostics["score_summary_present"] = isinstance(score_summary, dict) and bool(score_summary)
    diagnostics["summary_parts_present"] = isinstance(summary_parts, list)
    diagnostics["summary_parts_count"] = len(summary_parts) if isinstance(summary_parts, list) else 0
    diagnostics["original_part_count"] = int(original_part_count)

    transforms = score.get("voice_part_transforms") if isinstance(score.get("voice_part_transforms"), dict) else {}
    derived_by_transform = False
    diagnostics["voice_part_transforms_present"] = isinstance(transforms, dict)
    diagnostics["voice_part_transforms_count"] = len(transforms) if isinstance(transforms, dict) else 0
    derived_target_candidates = _collect_derived_target_candidates(score)
    diagnostics["derived_target_candidates"] = derived_target_candidates
    suggested_for_input_part = [
        candidate
        for candidate in derived_target_candidates
        if candidate.get("source_part_index") == int(part_index)
    ]
    diagnostics["suggested_derived_targets_for_input_part"] = suggested_for_input_part
    if isinstance(transforms, dict):
        for value in transforms.values():
            if not isinstance(value, dict):
                continue
            appended_ref = value.get("appended_part_ref")
            if not isinstance(appended_ref, dict):
                continue
            appended_part_index = appended_ref.get("part_index")
            appended_part_id = str(appended_ref.get("part_id") or "").strip()
            if (isinstance(appended_part_index, int) and appended_part_index == part_index) or (
                part_id and appended_part_id and appended_part_id == part_id
            ):
                derived_by_transform = True
                break

    derived_by_index_delta = part_index >= original_part_count
    is_derived_target = derived_by_index_delta or derived_by_transform or derived_by_marker
    diagnostics["derived_by_index_delta"] = bool(derived_by_index_delta)
    diagnostics["derived_by_transform_metadata"] = bool(derived_by_transform)
    diagnostics["is_derived_target"] = bool(is_derived_target)
    if is_derived_target:
        return None

    signals = score.get("voice_part_signals") if isinstance(score.get("voice_part_signals"), dict) else {}
    signal_parts = signals.get("parts") if isinstance(signals, dict) else None
    diagnostics["voice_part_signals_present"] = isinstance(signals, dict) and bool(signals)
    diagnostics["signal_parts_present"] = isinstance(signal_parts, list)
    diagnostics["signal_parts_count"] = len(signal_parts) if isinstance(signal_parts, list) else 0
    if not isinstance(signal_parts, list):
        return build_preprocessing_required_action(
            part_index=part_index,
            reason="complexity_signal_unavailable_without_derived_target",
            failed_validation_rules=["complexity_signal.parts_missing"],
            diagnostics=diagnostics,
        )

    signal = None
    for item in signal_parts:
        if isinstance(item, dict) and int(item.get("part_index", -1)) == part_index:
            signal = item
            break
    if not isinstance(signal, dict):
        return build_preprocessing_required_action(
            part_index=part_index,
            reason="complexity_signal_unavailable_without_derived_target",
            failed_validation_rules=["complexity_signal.target_part_signal_missing"],
            diagnostics=diagnostics,
        )
    diagnostics["target_signal_found"] = True

    missing = signal.get("missing_lyric_voice_parts")
    has_multi_voice = bool(signal.get("multi_voice_part"))
    has_missing_lyric_voice_parts = isinstance(missing, list) and len(missing) > 0
    diagnostics["missing_lyric_voice_parts"] = (
        list(missing) if isinstance(missing, list) else missing
    )
    diagnostics["has_multi_voice_part_signal"] = bool(has_multi_voice)
    diagnostics["has_missing_lyric_voice_parts_signal"] = bool(has_missing_lyric_voice_parts)
    preprocess_required = has_multi_voice or has_missing_lyric_voice_parts
    preprocessed_score_detected = bool(
        (isinstance(transforms, dict) and len(transforms) > 0)
        or len(parts) > int(original_part_count)
    )
    diagnostics["preprocessed_score_detected"] = preprocessed_score_detected
    diagnostics["preprocess_required"] = bool(preprocess_required)
    if not preprocess_required:
        return None

    failed_rules: List[str] = []
    if has_multi_voice:
        failed_rules.append("complexity_signal.multi_voice_part")
    if has_missing_lyric_voice_parts:
        failed_rules.append("complexity_signal.missing_lyric_voice_parts")
    if not derived_by_index_delta:
        failed_rules.append("derived_detection.index_delta_not_met")
    if not derived_by_transform:
        failed_rules.append("derived_detection.transform_metadata_not_found")
    if preprocessed_score_detected and suggested_for_input_part:
        failed_rules.append("derived_detection.derived_target_not_selected_after_preprocess")

    if has_multi_voice and has_missing_lyric_voice_parts:
        reason = "complex_score_multi_voice_and_missing_lyrics_without_derived_target"
    elif has_multi_voice:
        reason = "complex_score_multi_voice_without_derived_target"
    else:
        reason = "complex_score_missing_lyrics_without_derived_target"
    message = (
        "This part requires voice-part preprocessing before synthesis. "
        "Run preprocess_voice_parts first, then synthesize the derived target."
    )
    if preprocessed_score_detected and suggested_for_input_part:
        reason = "preprocessed_score_without_derived_target_selection"
        message = (
            "Preprocessing already exists for this score, but synthesize targeted an original "
            "complex part. Retry synthesize using a derived part selection. "
            "Only rerun preprocess_voice_parts if the user asked for plan revisions."
        )

    return build_preprocessing_required_action(
        part_index=part_index,
        reason=reason,
        failed_validation_rules=failed_rules,
        diagnostics=diagnostics,
        message=message,
    )


def _normalize_voice_part_id(
    *, voice_part_id: Optional[str], voice_id: Optional[str]
) -> Optional[str]:
    if isinstance(voice_part_id, str) and voice_part_id.strip():
        return voice_part_id
    if isinstance(voice_id, str) and voice_id.strip():
        return voice_id
    return None


def _normalize_verse_number(raw_verse_number: Optional[str | int]) -> Optional[str]:
    if raw_verse_number is None:
        return None
    text = str(raw_verse_number).strip()
    return text or None


def _extract_verse_from_lyric(raw_lyric: Any) -> Optional[str]:
    if not isinstance(raw_lyric, str):
        return None
    match = re.match(r"^\s*(\d+)\.", raw_lyric)
    if match:
        return match.group(1)
    return None


def _voice_key(raw: Any) -> str:
    if raw is None:
        return "_default"
    text = str(raw).strip()
    return text if text else "_default"


def _avg_pitch(notes: Sequence[Dict[str, Any]]) -> float:
    pitches = [float(note["pitch_midi"]) for note in notes if note.get("pitch_midi") is not None]
    if not pitches:
        return float("-inf")
    return sum(pitches) / len(pitches)


def _has_lyric(note: Dict[str, Any]) -> bool:
    lyric = note.get("lyric")
    return isinstance(lyric, str) and lyric.strip() != ""


def _lyric_kind(note: Dict[str, Any]) -> str:
    lyric = note.get("lyric")
    if not isinstance(lyric, str):
        return "empty"
    text = lyric.strip()
    if not text:
        return "empty"
    if text.startswith("+") or bool(note.get("lyric_is_extended")):
        return "extension"
    return "word"


def _part_has_any_lyric(notes: Sequence[Dict[str, Any]]) -> bool:
    return any(_has_lyric(note) for note in notes if not note.get("is_rest"))
