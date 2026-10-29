"""Deterministic facts for the offline preprocess planner decision sheet."""

from __future__ import annotations

import html
import re
from itertools import permutations
from math import factorial
from dataclasses import dataclass, field
from typing import Any

from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _avg_pitch,
    _best_same_part_word_lyric_source_for_range,
    _build_measure_lyric_coverage,
    _build_part_region_indices,
    _derived_part_indices,
    _generated_solfege_lyric_stats_for_voice_part_range,
    _is_countable_sung_note,
    _is_cross_staff_lyric_source_with_stronger_local_alternative,
    _is_cross_staff_weak_lyric_source_with_better_alternative,
    _is_weak_word_lyric_source_with_better_alternative,
    _lyric_kind,
    _lyric_stats_for_voice_part_range,
    _note_signature,
    _normalize_verse_number,
    _staff_scope_key,
    _staff_scope_measure_max_simultaneous_notes,
    _target_sung_note_count_for_section,
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
    existing_derived_lane_id: str | None = None


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


SLOT_KINDS = (
    "mode", "melody_source", "lyric_source", "lyric_strategy", "lyric_policy",
    "rank_fallback", "rank_assignment", "split_shared_note_policy", "freeform",
)


@dataclass(frozen=True)
class Option:
    option_id: str
    label: str
    fields: dict
    evidence: dict
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Slot:
    slot_id: str
    kind: str
    lane_ids: tuple[str, ...]
    start_measure: int
    end_measure: int
    options: tuple[Option, ...]
    evidence: dict


@dataclass
class Sheet(SheetSkeleton):
    slots: list[Slot] = field(default_factory=list)
    fixed: dict[str, Option] = field(default_factory=dict)


def _unescaped(value: Any) -> str:
    text = str(value or "").strip()
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text.strip()


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
    unescaped = [
        i for i, part in enumerate(parts)
        if _unescaped(part.get("part_id")) == _unescaped(requested)
    ]
    if len(unescaped) == 1:
        return unescaped[0]
    if unescaped:
        return None
    by_name = [
        i for i, part in enumerate(parts)
        if _unescaped(part.get("part_name")).casefold() == _unescaped(requested).casefold()
    ]
    return by_name[0] if len(by_name) == 1 else None


def _verse_number(planning_context: dict) -> str | None:
    target = planning_context.get("target") or {}
    request = planning_context.get("request") or {}
    raw = target.get("verse_number")
    if raw is None:
        raw = request.get("verse_number")
    return _normalize_verse_number(raw)


def _existing_lane_slots(score: dict) -> dict[tuple[int, str, int], str | None]:
    """Existing visible derived lanes, addressed by the executor's per-source slot."""
    visible_parts = _derived_part_indices(score)
    slots: dict[tuple[int, str, int], str | None] = {}
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
            slots[(part_index, voice_part_id, slot)] = str(lane.get("derived_lane_id") or "").strip() or None
    return slots


def _annotations(score: dict, part: dict, scope: str) -> dict[int, str]:
    signals = score.get("voice_part_signals") or {}
    rows = signals.get("measure_annotations") or []
    text_by_measure: dict[int, str] = {}
    raw_part_id = part.get("raw_part_id") or part.get("part_id")
    split_staff = re.search(r"-Staff(\d+)$", str(part.get("part_id") or ""))
    raw_staff = split_staff.group(1) if split_staff else scope
    for part_row in rows:
        if part_row.get("part_id") != raw_part_id:
            continue
        for measure in part_row.get("measures") or []:
            try:
                measure_number = int(measure["measure_number"])
            except (KeyError, TypeError, ValueError):
                continue
            entries = measure.get("direction_entries") or []
            if entries:
                texts = [
                    str(entry.get("text") or "").strip()
                    for entry in entries
                    if str(entry.get("staff") or raw_staff) == raw_staff
                    and str(entry.get("text") or "").strip()
                ]
            else:
                texts = [str(text).strip() for text in measure.get("directions") or [] if str(text).strip()]
            if texts:
                text_by_measure[measure_number] = "; ".join(texts)
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
        lane_parts = [vp for vp in scoped_parts if str(vp["source_voice_id"]) != "_default"] or scoped_parts
        chordal = max(
            lane_parts,
            key=lambda vp: (
                max(
                    (int(value) for value in chord_density.get(vp["voice_part_id"], {}).get(
                        "max_simultaneous_notes_by_measure", {}
                    ).values()),
                    default=1,
                ),
                -lane_parts.index(vp),
            ),
        )
        sources = [vp["voice_part_id"] for vp in lane_parts]
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
                existing_derived_lane_id=existing_slots.get((part_index, source, ordinal + 1)),
            ))
        skeleton.lanes.extend(scope_lanes)
        skeleton.segments.extend(_segments(
            score, part, part_index, scope, scope_lanes, scoped_parts, scope_notes, start, end
        ))
    skeleton.segments.sort(key=lambda segment: (
        segment.part_index, segment.scope_key, segment.start_measure
    ))
    return skeleton


def _notes_in_range(part: dict, voice: str, start: int, end: int) -> list[dict]:
    return [
        note for note in part.get("notes") or []
        if _is_countable_sung_note(note)
        and _voice_key(note.get("voice")) == voice
        and start <= int(note.get("measure_number") or 0) <= end
    ]


def _source_candidates(score: dict, segment: Segment, derived_indices: set[int]) -> list[dict]:
    candidates: list[dict] = []
    for part_index, part in enumerate(score.get("parts") or []):
        if segment.part_index not in derived_indices and part_index in derived_indices:
            continue
        for voice_part in _analyze_part_voice_parts(part, part_index)["voice_parts"]:
            notes = _notes_in_range(
                part, str(voice_part["source_voice_id"]),
                segment.start_measure, segment.end_measure,
            )
            if notes:
                candidates.append({
                    "part_index": part_index,
                    "part_id": str(part.get("part_id") or ""),
                    "voice_part_id": str(voice_part["voice_part_id"]),
                    "notes": notes,
                    "scopes": {_staff_scope_key(note) for note in notes},
                })
    return candidates


def _source_option(candidate: dict, kind: str, *, flags: tuple[str, ...] = (), evidence: dict | None = None) -> Option:
    part_id = candidate["part_id"]
    voice_part_id = candidate["voice_part_id"]
    return Option(
        option_id=f"{part_id}|{voice_part_id}",
        label=f"{part_id}: {voice_part_id}",
        fields={kind: {"part_index": candidate["part_index"], "voice_part_id": voice_part_id}},
        evidence=evidence or {},
        flags=flags,
    )


def _melody_options(lane: Lane, candidates: list[dict]) -> tuple[Option, ...]:
    local = [
        source for source in candidates
        if source["part_index"] == lane.part_index and lane.scope_key in source["scopes"]
    ]
    ordered = sorted(candidates, key=lambda source: (
        0 if (source["part_index"] == lane.part_index
              and source["voice_part_id"] == lane.source_voice_part_id
              and lane.scope_key in source["scopes"]) else
        1 if source in local else 2,
        source["part_index"], source["voice_part_id"],
    ))
    return tuple(_source_option(
        source, "melody_source",
        flags=("P1 cross_staff_melody_source_when_local_available",)
        if local and source not in local else (),
        evidence={"sung_note_count": len(source["notes"])},
    ) for source in ordered)


def _lyric_options(
    score: dict, lane: Lane, segment: Segment, candidates: list[dict],
    target_notes: list[dict], target_sung_note_count: int, derived_indices: set[int],
) -> tuple[Option, ...]:
    target_onsets = {
        (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
        for note in target_notes
    }
    target_onset_count = len(target_onsets)
    valid: list[tuple[dict, dict]] = []
    for source in candidates:
        if source["part_index"] in derived_indices:
            continue
        if not any(_lyric_kind(note) == "word" for note in source["notes"]):
            continue
        generated = _generated_solfege_lyric_stats_for_voice_part_range(
            score=score, part_index=source["part_index"],
            voice_part_id=source["voice_part_id"],
            start_measure=segment.start_measure, end_measure=segment.end_measure,
        )
        if int(generated["generated_solfege_lyric_note_count"]) > 0:
            continue
        stats = _lyric_stats_for_voice_part_range(
            score=score, part_index=source["part_index"],
            voice_part_id=source["voice_part_id"],
            start_measure=segment.start_measure, end_measure=segment.end_measure,
            target_sung_note_count=target_sung_note_count,
        )
        if int(stats["word_lyric_note_count"]) > 0:
            donor_onsets = {
                (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                for note in source["notes"] if _lyric_kind(note) == "word"
            }
            stats = dict(stats)
            stats["aligned_word_note_count"] = len(target_onsets & donor_onsets)
            stats["aligned_word_coverage_ratio"] = round(
                len(target_onsets & donor_onsets) / max(1, target_onset_count), 4
            )
            valid.append((source, stats))
    valid.sort(key=lambda item: (
        0 if (
            item[0]["part_index"] == lane.part_index
            and item[0]["voice_part_id"] == lane.source_voice_part_id
            and target_sung_note_count > 0
            and int(item[1]["word_lyric_note_count"]) >= target_sung_note_count
        ) else 1,
        -float(item[1]["word_lyric_coverage_ratio"]),
        -float(item[1]["aligned_word_coverage_ratio"]),
        0 if item[0]["part_index"] == lane.part_index else 1,
        item[0]["voice_part_id"], item[0]["part_index"],
    ))
    options: list[Option] = []
    for source, stats in valid:
        flags: list[str] = []
        if source["part_index"] == lane.part_index:
            alternative = _best_same_part_word_lyric_source_for_range(
                score=score, part_index=lane.part_index,
                start_measure=segment.start_measure, end_measure=segment.end_measure,
                exclude_voice_part_id=source["voice_part_id"],
                target_sung_note_count=target_sung_note_count,
            )
            if alternative is not None and _is_weak_word_lyric_source_with_better_alternative(
                selected_stats=stats, alternative_stats=alternative["stats"],
            ):
                flags.append("P1 weak_lyric_source_with_better_alternative")
        else:
            local_alternative = _best_same_part_word_lyric_source_for_range(
                score=score, part_index=lane.part_index,
                start_measure=segment.start_measure, end_measure=segment.end_measure,
                exclude_voice_part_id="",
                target_sung_note_count=target_sung_note_count,
            )
            if local_alternative is not None and _is_cross_staff_lyric_source_with_stronger_local_alternative(
                selected_stats=stats, local_alternative_stats=local_alternative["stats"],
            ):
                flags.append("P1 cross_staff_lyric_source_with_stronger_local_alternative")
            donor_alternative = _best_same_part_word_lyric_source_for_range(
                score=score, part_index=source["part_index"],
                start_measure=segment.start_measure, end_measure=segment.end_measure,
                exclude_voice_part_id=source["voice_part_id"],
                target_sung_note_count=target_sung_note_count,
            )
            if donor_alternative is not None and _is_cross_staff_weak_lyric_source_with_better_alternative(
                selected_stats=stats, donor_alternative_stats=donor_alternative["stats"],
            ):
                flags.append("P1 cross_staff_weak_lyric_source_with_better_alternative")
        options.append(_source_option(source, "lyric_source", flags=tuple(flags), evidence=dict(stats)))
    return tuple(options)


def _choice_slot(
    lane_ids: tuple[str, ...], segment: Segment, kind: str,
    options: tuple[Option, ...], *, joint_id: str | None = None, evidence: dict | None = None,
) -> Slot:
    prefix = joint_id or lane_ids[0]
    return Slot(
        slot_id=f"{prefix}|{segment.start_measure}-{segment.end_measure}|{kind}",
        kind=kind,
        lane_ids=lane_ids,
        start_measure=segment.start_measure,
        end_measure=segment.end_measure,
        options=options,
        evidence=evidence or {},
    )


def _put_slot(sheet: Sheet, slot: Slot) -> None:
    if len(slot.options) == 1 and slot.kind != "freeform":
        sheet.fixed[slot.slot_id] = slot.options[0]
    else:
        sheet.slots.append(slot)


def _target_notes(score: dict, lane: Lane, segment: Segment) -> list[dict]:
    part = score["parts"][lane.part_index]
    voice_parts = _analyze_part_voice_parts(part, lane.part_index)["voice_parts"]
    source = next((vp for vp in voice_parts if vp["voice_part_id"] == lane.source_voice_part_id), None)
    if source is None:
        return []
    return [
        note for note in _notes_in_range(
            part, str(source["source_voice_id"]), segment.start_measure, segment.end_measure
        ) if _staff_scope_key(note) == lane.scope_key
    ]


def _shared_notes_in_scope(score: dict, lane: Lane) -> bool:
    notes = [note for note in score["parts"][lane.part_index].get("notes") or []
             if _is_countable_sung_note(note) and _staff_scope_key(note) == lane.scope_key]
    voices_by_signature: dict[tuple, set[str]] = {}
    for note in notes:
        signature = (int(note.get("measure_number") or 0), *_note_signature(note))
        voices_by_signature.setdefault(signature, set()).add(_voice_key(note.get("voice")))
    return any(len(voices) > 1 for voices in voices_by_signature.values())


def _freeform_slot(segment: Segment, lanes: list[Lane], reason: str) -> Slot:
    return _choice_slot(
        tuple(lane.lane_id for lane in lanes), segment, "freeform", (),
        joint_id=f"{lanes[0].part_id}|{segment.scope_key}",
        evidence={"reason": reason, "per_measure_facts": [
            {"measure": measure, "signature": {
                "density": segment.signature.density,
                "lanes": segment.signature.lanes,
                "annotation": segment.signature.annotation,
            }} for measure in range(segment.start_measure, segment.end_measure + 1)
        ]},
    )


def _rank_assignment_options(groups: list[list[Lane]]) -> tuple[Option, ...]:
    """Enumerate joint assignments with each lane's existing rank first."""
    assignments: list[dict[str, int]] = [{}]
    for group in groups:
        ranks = tuple(range(len(group)))
        assignments = [
            {**assignment, **{lane.lane_id: rank for lane, rank in zip(group, chosen)}}
            for assignment in assignments for chosen in permutations(ranks)
        ]
    return tuple(Option(
        option_id="perm:" + "-".join(str(rank) for rank in assignment.values()),
        label="Ranks " + ", ".join(str(rank) for rank in assignment.values()),
        fields={"rank_index_by_lane": assignment}, evidence={}, flags=(),
    ) for assignment in assignments)


def build_sheet(score: dict, planning_context: dict | None) -> Sheet:
    """Enumerate deterministic defaults and legal choices for each segment.

    A non-native lane's lyric choices use its default melody source for coverage
    and onset evidence. With no word-lyric donor, its fallback lyric options
    mirror its melody options when the lane copies another voice.
    """
    skeleton = build_sheet_skeleton(score, planning_context)
    sheet = Sheet(
        target_part_index=skeleton.target_part_index,
        verse_number=skeleton.verse_number,
        lanes=list(skeleton.lanes),
        segments=list(skeleton.segments),
        issues=list(skeleton.issues),
    )
    if sheet.target_part_index is None:
        return sheet
    derived_indices = _derived_part_indices(score)
    for segment in sheet.segments:
        lanes = [lane for lane in sheet.lanes
                 if lane.part_index == segment.part_index and lane.scope_key == segment.scope_key]
        if not lanes:
            continue
        candidates = _source_candidates(score, segment, derived_indices)
        segment_slots: list[Slot] = []
        freeform_reason: str | None = None
        for lane in lanes:
            native = next(fact[1] for fact in segment.signature.lanes if fact[0] == lane.lane_id)
            native_lyric_kind = next(fact[2] for fact in segment.signature.lanes if fact[0] == lane.lane_id)
            target_notes = _target_notes(score, lane, segment)
            melody = _melody_options(lane, candidates)
            if native and not melody:
                freeform_reason = "singing_lane_without_melody"
                break
            mode_ids = ("derive",) if native else ("derive", "rest") if melody else ("rest",)
            segment_slots.append(_choice_slot((lane.lane_id,), segment, "mode", tuple(
                Option(mode, mode, {"mode": mode}, {"has_native_notes": native}, ())
                for mode in mode_ids
            )))
            if not melody:
                continue
            segment_slots.append(_choice_slot((lane.lane_id,), segment, "melody_source", melody))
            if not native:
                default_melody = melody[0].fields["melody_source"]
                target_notes = next(source["notes"] for source in candidates if
                                    source["part_index"] == default_melody["part_index"] and
                                    source["voice_part_id"] == default_melody["voice_part_id"])
            else:
                default_melody = {"part_index": lane.part_index,
                                  "voice_part_id": lane.source_voice_part_id}
            target_sung_note_count = _target_sung_note_count_for_section(
                score=score, target_part_index=lane.part_index,
                target_voice_part_id=lane.source_voice_part_id,
                start_measure=segment.start_measure, end_measure=segment.end_measure,
                melody_source_part_index=default_melody["part_index"],
                melody_source_voice_part_id=default_melody["voice_part_id"],
            )
            lyric = _lyric_options(
                score, lane, segment, candidates, target_notes,
                target_sung_note_count, derived_indices,
            )
            if not lyric:
                reason = {"reason": "no_word_lyric_donor"}
                source_sings = bool(_target_notes(score, lane, segment))
                if native or source_sings:
                    lyric = (_source_option({
                        "part_id": lane.part_id,
                        "part_index": lane.part_index,
                        "voice_part_id": lane.source_voice_part_id,
                    }, "lyric_source", evidence=reason),)
                else:
                    lyric = tuple(Option(
                        option_id=option.option_id,
                        label=option.label,
                        fields={"lyric_source": dict(option.fields["melody_source"])},
                        evidence={**reason, "pairs_with_melody_option": option.option_id},
                        flags=(),
                    ) for option in melody)
            segment_slots.append(_choice_slot((lane.lane_id,), segment, "lyric_source", lyric))
            own_native = (
                lyric[0].fields["lyric_source"] == {
                    "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id
                } and native_lyric_kind == "words"
            )
            if not own_native:
                donor_ref = lyric[0].fields["lyric_source"]
                donor = next((source for source in candidates if
                              source["part_index"] == donor_ref["part_index"] and
                              source["voice_part_id"] == donor_ref["voice_part_id"]), None)
                target_onsets = {
                    (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                    for note in target_notes
                }
                donor_onsets = {
                    (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                    for note in (donor["notes"] if donor is not None else target_notes)
                    if _lyric_kind(note) == "word"
                }
                strategies = ("strict_onset", "overlap_best_match", "syllable_flow")
                if not target_onsets.issubset(donor_onsets):
                    strategies = ("overlap_best_match", "strict_onset", "syllable_flow")
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "lyric_strategy", tuple(
                    Option(value, value, {"lyric_strategy": value}, {
                        "aligned_onsets": len(target_onsets & donor_onsets),
                        "target_onsets": len(target_onsets),
                    }, ()) for value in strategies
                )))
            if native_lyric_kind == "extension_only":
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "lyric_policy", tuple(
                    Option(value, value, {"lyric_policy": value}, {}, ())
                    for value in ("fill_missing_only", "replace_all")
                )))
            siblings = [other for other in lanes
                        if other.source_voice_part_id == lane.source_voice_part_id]
            if len(siblings) > 1 and target_notes:
                counts: dict[tuple[int, float], int] = {}
                for note in target_notes:
                    key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                    counts[key] = counts.get(key, 0) + 1
                if any(count < len(siblings) for count in counts.values()):
                    segment_slots.append(_choice_slot((lane.lane_id,), segment, "rank_fallback", tuple(
                        Option(value, value, {"rank_fallback": value}, {}, ())
                        for value in ("greedy", "skip")
                    )))
        if freeform_reason is None and segment.signature.density > 1:
            by_source: dict[str, list[Lane]] = {}
            for lane in lanes:
                by_source.setdefault(lane.source_voice_part_id, []).append(lane)
            groups = [sorted(siblings, key=lambda lane: lane.default_rank)
                      for siblings in by_source.values() if len(siblings) > 1]
            if groups:
                option_count = 1
                for group in groups:
                    option_count *= factorial(len(group))
                if option_count > 24:
                    freeform_reason = "rank_assignment_over_24_permutations"
                else:
                    ordered_lanes = [lane for group in groups for lane in group]
                    rank_options = _rank_assignment_options(groups)
                    segment_slots.append(_choice_slot(
                        tuple(lane.lane_id for lane in ordered_lanes), segment, "rank_assignment",
                        rank_options, joint_id=f"{ordered_lanes[0].part_id}|{segment.scope_key}",
                    ))
        if freeform_reason is not None:
            sheet.slots.append(_freeform_slot(segment, lanes, freeform_reason))
        else:
            for slot in segment_slots:
                _put_slot(sheet, slot)

    for lane in sheet.lanes:
        if not _shared_notes_in_scope(score, lane):
            continue
        lane_segments = [segment for segment in sheet.segments
                         if segment.part_index == lane.part_index and segment.scope_key == lane.scope_key]
        if not lane_segments:
            continue
        span = Segment(lane.part_index, lane.scope_key,
                       lane_segments[0].start_measure, lane_segments[-1].end_measure,
                       lane_segments[0].signature)
        if any(slot.kind == "freeform" and lane.lane_id in slot.lane_ids for slot in sheet.slots):
            continue
        _put_slot(sheet, _choice_slot((lane.lane_id,), span, "split_shared_note_policy", tuple(
            Option(value, value, {"split_shared_note_policy": value}, {}, ())
            for value in ("duplicate_to_all", "assign_primary_only")
        )))
    sheet.slots.sort(key=lambda slot: (slot.start_measure, slot.slot_id))
    return sheet
