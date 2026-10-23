"""Deterministic facts for the offline preprocess planner decision sheet."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _avg_pitch,
    _build_measure_lyric_coverage,
    _build_part_region_indices,
    _derived_part_indices,
    _is_countable_sung_note,
    _normalize_verse_number,
    _staff_scope_key,
    _staff_scope_measure_max_simultaneous_notes,
    _voice_key,
)


@dataclass(frozen=True)
class Lane:
    lane_id: str
    part_id: str
    part_index: int
    scope_key: str
    source_voice_part_id: str
    default_rank: int
    output_mode: str


@dataclass(frozen=True)
class MeasureSignature:
    density: int
    lanes: tuple[tuple[str, bool, str], ...]
    annotation: str | None


@dataclass(frozen=True)
class Segment:
    part_index: int
    scope_key: str
    start_measure: int
    end_measure: int
    signature: MeasureSignature


@dataclass
class SheetSkeleton:
    target_part_index: int | None = None
    verse_number: str | None = None
    lanes: list[Lane] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _target_part_index(parts: list[dict], planning_context: dict) -> int | None:
    target = planning_context.get("target") or {}
    requested = str(target.get("part_id") or "").strip()
    if not requested:
        return None
    exact = [i for i, part in enumerate(parts) if part.get("part_id") == requested]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    by_name = [
        i for i, part in enumerate(parts)
        if str(part.get("part_name") or "").strip().casefold() == requested.casefold()
    ]
    return by_name[0] if len(by_name) == 1 else None


def _verse_number(planning_context: dict) -> str | None:
    target = planning_context.get("target") or {}
    request = planning_context.get("request") or {}
    raw = target.get("verse_number")
    if raw is None:
        raw = request.get("verse_number")
    return _normalize_verse_number(raw)


def _existing_lane_slots(score: dict) -> set[tuple[int, str, int]]:
    """Existing visible derived lanes, addressed by the executor's per-source slot."""
    visible_parts = _derived_part_indices(score)
    slots: set[tuple[int, str, int]] = set()
    transforms = score.get("voice_part_transforms")
    if not isinstance(transforms, dict):
        return slots
    for value in transforms.values():
        if not isinstance(value, dict):
            continue
        lane = value.get("derived_lane")
        appended = value.get("appended_part_ref")
        if not isinstance(lane, dict) or not isinstance(appended, dict):
            continue
        if appended.get("part_index") not in visible_parts:
            continue
        source = lane.get("source") or {}
        part_index = source.get("part_index")
        voice_part_id = source.get("voice_part_id")
        slot = lane.get("slot")
        if isinstance(part_index, int) and isinstance(voice_part_id, str) and isinstance(slot, int):
            slots.add((part_index, voice_part_id, slot))
    return slots


def _annotations(score: dict, part: dict, scope: str) -> dict[int, str]:
    signals = score.get("voice_part_signals") or {}
    rows = signals.get("measure_annotations") or []
    text_by_measure: dict[int, str] = {}
    for part_row in rows:
        if part_row.get("part_id") != part.get("part_id"):
            continue
        for measure in part_row.get("measures") or []:
            entries = measure.get("direction_entries") or []
            if entries:
                texts = [
                    str(entry.get("text") or "").strip()
                    for entry in entries
                    if str(entry.get("staff") or scope) == scope
                    and str(entry.get("text") or "").strip()
                ]
            else:
                texts = [str(text).strip() for text in measure.get("directions") or [] if str(text).strip()]
            if texts:
                text_by_measure[int(measure["measure_number"])] = "; ".join(texts)
    return text_by_measure


def _segments(
    score: dict,
    part: dict,
    part_index: int,
    scope: str,
    lanes: list[Lane],
    voice_parts: list[dict],
    scope_notes: list[dict],
    start: int,
    end: int,
) -> list[Segment]:
    density = _staff_scope_measure_max_simultaneous_notes(score, part_index=part_index, staff_scope=scope)
    coverage_rows = _build_measure_lyric_coverage({"notes": scope_notes}, voice_parts)
    coverage = {
        (int(row["measure_number"]), vp["voice_part_id"]): vp
        for row in coverage_rows for vp in row["voice_parts"]
    }
    source_voice = {vp["voice_part_id"]: str(vp["source_voice_id"]) for vp in voice_parts}
    source_counts: dict[tuple[str, int, float], int] = {}
    for note in scope_notes:
        measure = int(note.get("measure_number") or 0)
        onset = round(float(note.get("offset_beats") or 0.0), 6)
        key = (_voice_key(note.get("voice")), measure, onset)
        source_counts[key] = source_counts.get(key, 0) + 1
    max_source_density: dict[tuple[str, int], int] = {}
    for (voice, measure, _), count in source_counts.items():
        key = (voice, measure)
        max_source_density[key] = max(max_source_density.get(key, 0), count)

    source_ordinal: dict[str, int] = {}
    lane_ordinals: list[int] = []
    for lane in lanes:
        ordinal = source_ordinal.get(lane.source_voice_part_id, 0)
        lane_ordinals.append(ordinal)
        source_ordinal[lane.source_voice_part_id] = ordinal + 1

    annotations = _annotations(score, part, scope)
    result: list[Segment] = []
    run_start = start
    previous: MeasureSignature | None = None
    for measure in range(start, end + 1):
        lane_facts: list[tuple[str, bool, str]] = []
        for lane, ordinal in zip(lanes, lane_ordinals):
            vp_id = lane.source_voice_part_id
            native = max_source_density.get((source_voice[vp_id], measure), 0) > ordinal
            stats = coverage.get((measure, vp_id)) or {}
            if not native or not stats.get("lyric_note_count"):
                lyric_kind = "none"
            elif stats.get("word_lyric_note_count"):
                lyric_kind = "words"
            else:
                lyric_kind = "extension_only"
            lane_facts.append((lane.lane_id, native, lyric_kind))
        signature = MeasureSignature(
            density=density.get(measure, 0),
            lanes=tuple(lane_facts),
            annotation=annotations.get(measure),
        )
        if previous is not None and signature != previous:
            result.append(Segment(part_index, scope, run_start, measure - 1, previous))
            run_start = measure
        previous = signature
    if previous is not None:
        result.append(Segment(part_index, scope, run_start, end, previous))
    return result


def build_sheet_skeleton(score: dict, planning_context: dict | None) -> SheetSkeleton:
    """Build scope lanes and exact signature runs without changing the score."""
    context = planning_context or {}
    parts = score.get("parts") or []
    skeleton = SheetSkeleton(verse_number=_verse_number(context))
    if str(context.get("other_instruction") or "").strip():
        skeleton.issues.append("intent_requires_llm")
    part_index = _target_part_index(parts, context)
    if part_index is None:
        skeleton.issues.append("target_unresolved")
        return skeleton

    skeleton.target_part_index = part_index
    part = parts[part_index]
    part_id = str(part["part_id"])
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis["voice_parts"]
    indices = _build_part_region_indices(part, voice_parts)
    span = indices["part_span"]
    start, end = span["start_measure"], span["end_measure"]
    existing_slots = _existing_lane_slots(score)
    notes_by_scope: dict[str, list[dict]] = {}
    for note in part.get("notes") or []:
        if _is_countable_sung_note(note):
            notes_by_scope.setdefault(_staff_scope_key(note), []).append(note)

    for scope in sorted(notes_by_scope):
        scope_notes = notes_by_scope[scope]
        by_voice: dict[str, list[dict]] = {}
        for note in scope_notes:
            by_voice.setdefault(_voice_key(note.get("voice")), []).append(note)
        scoped_parts = [vp for vp in voice_parts if str(vp["source_voice_id"]) in by_voice]
        scoped_parts.sort(
            key=lambda vp: (-_avg_pitch(by_voice[str(vp["source_voice_id"])]), vp["voice_part_id"])
        )
        measured_density = _staff_scope_measure_max_simultaneous_notes(
            score, part_index=part_index, staff_scope=scope
        )
        indexed_density = indices["staff_scope_simultaneous_density"].get(scope, {})
        max_density = max(max(measured_density.values(), default=1), indexed_density.get("max_simultaneous_notes", 1))
        if not scoped_parts or start <= 0 or end < start:
            continue
        chord_density = indices["source_voice_chord_density"]
        chordal = max(
            scoped_parts,
            key=lambda vp: (
                max(
                    (int(value) for value in chord_density.get(vp["voice_part_id"], {}).get(
                        "max_simultaneous_notes_by_measure", {}
                    ).values()),
                    default=1,
                ),
                -scoped_parts.index(vp),
            ),
        )
        sources = [vp["voice_part_id"] for vp in scoped_parts[:max_density]]
        sources.extend([chordal["voice_part_id"]] * (max_density - len(sources)))
        source_ordinal: dict[str, int] = {}
        scope_lanes: list[Lane] = []
        for rank, source in enumerate(sources):
            ordinal = source_ordinal.get(source, 0)
            source_ordinal[source] = ordinal + 1
            output_mode = (
                "update_existing_derived_lane"
                if (part_index, source, ordinal + 1) in existing_slots
                else "append_new_derived_lane"
            )
            scope_lanes.append(Lane(
                lane_id=f"{part_id}|{scope}|{source}|r{rank}",
                part_id=part_id,
                part_index=part_index,
                scope_key=scope,
                source_voice_part_id=source,
                default_rank=rank,
                output_mode=output_mode,
            ))
        skeleton.lanes.extend(scope_lanes)
        skeleton.segments.extend(_segments(
            score, part, part_index, scope, scope_lanes, scoped_parts, scope_notes, start, end
        ))
    skeleton.segments.sort(key=lambda segment: (
        segment.part_index, segment.scope_key, segment.start_measure
    ))
    return skeleton
