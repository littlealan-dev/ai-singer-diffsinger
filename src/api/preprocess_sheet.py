"""Deterministic facts for the offline preprocess planner decision sheet."""

from __future__ import annotations

import html
import re
from itertools import combinations, permutations, product
from math import factorial
from statistics import median
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _avg_pitch,
    _best_same_part_word_lyric_source_for_range,
    _build_part_region_indices,
    _derived_part_indices,
    _generated_solfege_lyric_stats_for_voice_part_range,
    _is_generated_solfege_lyric,
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
    sheet_lane_counts,
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
    lanes: tuple[tuple[str, bool], ...]
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
    "lane_layout", "mode", "melody_source", "lyric_source", "lyric_strategy", "lyric_line", "lyric_reading",
    "rank_fallback", "rank_assignment", "voice_assignment", "split_shared_note_policy", "unison",
    "unison_default", "one_line", "freeform",
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


@dataclass(frozen=True)
class StaffLyricLine:
    """Every word typed under one staff, by beat, whichever voice carries it."""

    words: dict[tuple[int, float], list[tuple[str, str]]]
    multiple: bool


@dataclass(frozen=True)
class LyricBarCase:
    case: str
    reasons: tuple[str, ...] = ()


def _beat(note: dict) -> tuple[int, float]:
    return int(note.get("measure_number") or 0), round(float(note.get("offset_beats") or 0.0), 6)


def _peak_onset_size(notes: Iterable[dict]) -> int:
    counts: dict[tuple[int, float], int] = {}
    for note in notes:
        beat = _beat(note)
        counts[beat] = counts.get(beat, 0) + 1
    return max(counts.values(), default=0)


def _word_text(note: dict) -> str | None:
    if _lyric_kind(note) != "word" or _is_generated_solfege_lyric(note):
        return None
    return str(note["lyric"]).strip()


def _voice_part_ids(part: dict, part_index: int) -> dict[str, str]:
    return {
        str(voice_part["source_voice_id"]): str(voice_part["voice_part_id"])
        for voice_part in _analyze_part_voice_parts(part, part_index)["voice_parts"]
    }


def staff_lyric_lines(score: dict) -> dict[tuple[int, str], StaffLyricLine]:
    """Step 1 of the lyric rule: the words typed at each beat on any voice of a staff."""
    collected: dict[tuple[int, str], dict[tuple[int, float], list[tuple[str, str]]]] = {}
    for part_index, part in enumerate(score.get("parts") or []):
        voice_part_ids: dict[str, str] | None = None
        for note in part.get("notes") or []:
            text = _word_text(note) if _is_countable_sung_note(note) else None
            if text is None:
                continue
            if voice_part_ids is None:
                voice_part_ids = _voice_part_ids(part, part_index)
            voice_part_id = voice_part_ids.get(_voice_key(note.get("voice")))
            if voice_part_id is None:
                continue
            words = collected.setdefault((part_index, _staff_scope_key(note)), {})
            words.setdefault(_beat(note), []).append((voice_part_id, text))
    return {
        staff: StaffLyricLine(
            words=dict(sorted(words.items())),
            multiple=any(len({text.casefold() for _, text in entries}) > 1
                         for entries in words.values()),
        )
        for staff, words in sorted(collected.items())
    }


def line_voice_for_staff(
    line: StaffLyricLine, part_index: int, line_voice: tuple[int, str] | None,
) -> str | None:
    """The chosen line's voice when it carries words on this staff, else None (the whole line)."""
    if line_voice is None or line_voice[0] != part_index:
        return None
    if any(voice_part_id == line_voice[1] for entries in line.words.values()
           for voice_part_id, _ in entries):
        return line_voice[1]
    return None


def read_lane_lyrics(
    line: StaffLyricLine, notes: list[dict], line_voice: str | None = None,
) -> list[str | None]:
    """Step 3 of the lyric rule: the word on each note's beat, else "+" after an earlier word."""
    words: dict[tuple[int, float], str] = {}
    for beat, entries in line.words.items():
        texts = [text for voice_part_id, text in entries
                 if line_voice is None or voice_part_id == line_voice]
        if texts:
            words[beat] = texts[0]
    first = min(words, default=None)
    readings: list[str | None] = []
    for note in notes:
        beat = _beat(note)
        if str(note.get("tie_type") or "").strip() in {"stop", "continue"}:
            own_word = _word_text(note)
            readings.append(own_word if own_word is not None else
                            ("+" if first is not None and first < beat else None))
        elif beat in words:
            readings.append(words[beat])
        elif first is not None and first < beat:
            readings.append("+")
        else:
            readings.append(None)
    return readings


def lyric_bar_cases(
    score: dict, lines: dict[tuple[int, str], StaffLyricLine], part_index: int,
    staff: str, lane_voice_part_id: str, line_voice: str | None,
) -> dict[int, LyricBarCase]:
    """Classify bars in one lane's staff lyric reading."""
    part = score["parts"][part_index]
    voices = {vp["voice_part_id"]: str(vp["source_voice_id"]) for vp in
              _analyze_part_voice_parts(part, part_index)["voice_parts"]}
    voice = voices.get(lane_voice_part_id)
    if voice is None or (part_index, staff) not in lines:
        return {}
    line = lines[(part_index, staff)]
    notes = sorted((note for note in part.get("notes") or []
                    if _is_countable_sung_note(note)
                    and _voice_key(note.get("voice")) == voice
                    and _staff_scope_key(note) == staff), key=_beat)
    all_notes = [note for note in part.get("notes") or []
                 if _is_countable_sung_note(note) and _staff_scope_key(note) == staff]
    notes_by_bar: dict[int, list[dict]] = {}
    for note in notes:
        notes_by_bar.setdefault(_beat(note)[0], []).append(note)
    readings = read_lane_lyrics(line, notes, line_voice)
    word_notes = []
    seen_word_beats: set[tuple[int, float]] = set()
    for note, reading in zip(notes, readings):
        if reading in {None, "+"} or _beat(note) in seen_word_beats:
            continue
        seen_word_beats.add(_beat(note))
        source = next((item for item in all_notes if _beat(item) == _beat(note)
                       and _word_text(item) == reading
                       and (line_voice is None or _voice_key(item.get("voice")) == voices.get(line_voice))),
                      None)
        word_notes.append((note, source or note))
    broken_bars: set[int] = set()
    for (previous, previous_source), (following, following_source) in zip(word_notes, word_notes[1:]):
        prev_syllabic = str(previous_source.get("syllabic") or "").lower()
        next_syllabic = str(following_source.get("syllabic") or "").lower()
        if not (prev_syllabic or next_syllabic):
            continue
        if (prev_syllabic in {"begin", "middle"}) != (next_syllabic in {"middle", "end"}):
            borrowed = any(
                not any(vp == lane_voice_part_id for vp, _ in line.words.get(_beat(note), []))
                for note in (previous, following)
            )
            if borrowed:
                broken_bars.add(_beat(following)[0])
    cases: dict[int, LyricBarCase] = {}
    for bar, lane_notes in notes_by_bar.items():
        words = {beat: [(vp, text) for vp, text in entries
                        if line_voice is None or vp == line_voice]
                 for beat, entries in line.words.items() if beat[0] == bar}
        words = {beat: entries for beat, entries in words.items() if entries}
        if not words:
            continue
        new_beats = {_beat(note) for note in lane_notes
                     if str(note.get("tie_type") or "").strip() not in {"stop", "continue"}
                     or _word_text(note) is not None}
        if not new_beats.intersection(words):
            cases[bar] = LyricBarCase("no_word_for_lane")
            continue
        carriers = {vp for entries in words.values() for vp, _ in entries}
        if len(carriers) == 1 or any(len({text.casefold() for _, text in entries}) > 1
                                     for entries in words.values()):
            cases[bar] = LyricBarCase("single")
            continue
        reasons: list[str] = []
        exclusive_carriers = {
            voice_part_id for voice_part_id in carriers
            if any(any(vp == voice_part_id for vp, _ in entries)
                   and not any(_beat(note) == beat
                               and _voice_key(note.get("voice")) != voices.get(voice_part_id)
                               for note in all_notes)
                   for beat, entries in words.items())
        }
        if len(exclusive_carriers) >= 2:
            reasons.append("independent_rhythm")
        if bar in broken_bars:
            reasons.append("broken_split_word")
        cases[bar] = LyricBarCase("ambiguous" if reasons else "shared", tuple(reasons))
    return cases


def note_staffs_by_measure(score: dict) -> dict[tuple[int, str], dict[int, set[tuple[int, str]]]]:
    """Per voice part and measure, the staffs its sung notes sit on (each note's line staff)."""
    index: dict[tuple[int, str], dict[int, set[tuple[int, str]]]] = {}
    for part_index, part in enumerate(score.get("parts") or []):
        voice_part_ids = _voice_part_ids(part, part_index)
        for note in part.get("notes") or []:
            voice_part_id = voice_part_ids.get(_voice_key(note.get("voice")))
            if voice_part_id is None or not _is_countable_sung_note(note):
                continue
            index.setdefault((part_index, voice_part_id), {}).setdefault(
                int(note.get("measure_number") or 0), set()
            ).add((part_index, _staff_scope_key(note)))
    return index


def melody_line_staffs(
    index: dict[tuple[int, str], dict[int, set[tuple[int, str]]]],
    part_index: int, voice_part_id: str, start: int, end: int,
) -> set[tuple[int, str]]:
    """The staffs a melody source's notes sit on in a measure range."""
    by_measure = index.get((part_index, voice_part_id), {})
    return set().union(*(by_measure.get(measure, set()) for measure in range(start, end + 1)))


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
    cuts: set[int] = frozenset(),
) -> list[Segment]:
    density = _staff_scope_measure_max_simultaneous_notes(score, part_index=part_index, staff_scope=scope)
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
        lane_facts: list[tuple[str, bool]] = []
        for lane, ordinal in zip(lanes, lane_ordinals):
            native = max_source_density.get((source_voice[lane.source_voice_part_id], measure), 0) > ordinal
            lane_facts.append((lane.lane_id, native))
        signature = MeasureSignature(
            density=density.get(measure, 0),
            lanes=tuple(lane_facts),
            annotation=annotations.get(measure),
        )
        if previous is not None and (signature != previous or measure in cuts):
            result.append(Segment(part_index, scope, run_start, measure - 1, previous))
            run_start = measure
        previous = signature
    if previous is not None:
        result.append(Segment(part_index, scope, run_start, end, previous))
    return result


def _eligible_lyric_sources(score: dict) -> list[dict]:
    """Non-derived voice parts with at least one non-generated lyric."""
    derived_indices = _derived_part_indices(score)
    sources: list[dict] = []
    for part_index, part in enumerate(score.get("parts") or []):
        if part_index in derived_indices:
            continue
        for voice_part in _analyze_part_voice_parts(part, part_index)["voice_parts"]:
            lyric_count = int(voice_part["lyric_note_count"])
            if lyric_count <= 0:
                continue
            voice = str(voice_part["source_voice_id"])
            notes = [note for note in part.get("notes") or []
                     if _is_countable_sung_note(note)
                     and _voice_key(note.get("voice")) == voice]
            generated_count = sum(
                _lyric_kind(note) != "empty" and _is_generated_solfege_lyric(note)
                for note in notes
            )
            if generated_count >= lyric_count:
                continue
            sources.append({
                "part_id": str(part.get("part_id") or ""),
                "part_index": part_index,
                "voice_part_id": str(voice_part["voice_part_id"]),
                "lyric_note_count": lyric_count,
                "scopes": {_staff_scope_key(note) for note in notes},
            })
    return sources


def build_sheet_skeleton(
    score: dict, planning_context: dict | None, *,
    cuts: dict[tuple[int, str], set[int]] | None = None,
    lane_layouts: dict[tuple[int, str], str] | None = None,
) -> SheetSkeleton:
    """Build scope lanes and exact signature runs without changing the score.

    ``cuts`` maps (part_index, scope_key) to measures that also start a new segment.
    """
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
    if not _eligible_lyric_sources(score):
        skeleton.issues.append("score_has_no_lyrics")
        return skeleton
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
        if (lane_layouts or {}).get((part_index, scope)) == "separate":
            voice_peaks: dict[str, int] = {}
            for voice, notes in by_voice.items():
                if voice == "_default":
                    continue
                voice_peaks[voice] = _peak_onset_size(notes)
            sources = [vp["voice_part_id"] for vp in lane_parts
                       for _ in range(voice_peaks.get(str(vp["source_voice_id"]), 0))]
        else:
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
            score, part, part_index, scope, scope_lanes, scoped_parts, scope_notes, start, end,
            set((cuts or {}).get((part_index, scope), ())),
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


def _line_label(notes: list[dict]) -> str:
    """Words in time order, joined by syllabic ("in ex-cel-sis"), at most 80 characters."""
    label = ""
    previous_syllabic = None
    for note in notes:
        if label:
            label += "-" if previous_syllabic in {"begin", "middle"} else " "
        label += _word_text(note) or ""
        previous_syllabic = note.get("syllabic")
    return label[:80]


def _lyric_line_options(
    score: dict, lane: Lane, segment: Segment, staffs: list[tuple[int, str]],
) -> tuple[Option, ...]:
    """One option per voice carrying words on these staffs in the segment (step 2, 2+ lines)."""
    carried: dict[tuple[int, str], dict[tuple[int, float], dict]] = {}
    for part_index, scope in staffs:
        part = score["parts"][part_index]
        voice_part_ids = _voice_part_ids(part, part_index)
        for note in part.get("notes") or []:
            if (not _is_countable_sung_note(note) or _staff_scope_key(note) != scope
                    or not segment.start_measure <= int(note.get("measure_number") or 0) <= segment.end_measure
                    or _word_text(note) is None):
                continue
            voice_part_id = voice_part_ids.get(_voice_key(note.get("voice")))
            if voice_part_id is not None:
                carried.setdefault((part_index, voice_part_id), {}).setdefault(_beat(note), note)
    own = (lane.part_index, lane.source_voice_part_id)
    ordered = sorted(carried, key=lambda key: (key != own, -len(carried[key]), key[1], key[0]))
    return tuple(Option(
        option_id=f"{score['parts'][part_index].get('part_id')}|{voice_part_id}",
        label=_line_label([note for _, note in sorted(carried[(part_index, voice_part_id)].items())]),
        fields={"lyric_line_voice": {"part_index": part_index, "voice_part_id": voice_part_id}},
        evidence={"word_count": len(carried[(part_index, voice_part_id)])},
        flags=(),
    ) for part_index, voice_part_id in ordered)


def _slot_id(lane_ids: tuple[str, ...], segment: Segment, kind: str, *, joint_id: str | None = None) -> str:
    return f"{joint_id or lane_ids[0]}|{segment.start_measure}-{segment.end_measure}|{kind}"


def _choice_slot(
    lane_ids: tuple[str, ...], segment: Segment, kind: str,
    options: tuple[Option, ...], *, joint_id: str | None = None, evidence: dict | None = None,
) -> Slot:
    return Slot(
        slot_id=_slot_id(lane_ids, segment, kind, joint_id=joint_id),
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


def _voice_assignment_options(
    score: dict, segment: Segment, lanes: list[Lane], candidates: list[dict],
) -> tuple[tuple[Option, ...] | None, dict, str | None]:
    """Assign chord coverage jointly, then rank every final source group."""
    local = [{**source, "notes": [note for note in source["notes"]
                                  if _staff_scope_key(note) == segment.scope_key]}
             for source in candidates
             if source["part_index"] == segment.part_index and segment.scope_key in source["scopes"]]
    needs: dict[str, int] = {}
    for source in local:
        counts: dict[tuple[int, float], int] = {}
        for note in source["notes"]:
            key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
            counts[key] = counts.get(key, 0) + 1
        needs[source["voice_part_id"]] = max(counts.values(), default=0)
    chord_sources = sorted(
        (source for source in local if needs[source["voice_part_id"]] >= 2),
        key=lambda source: (-median(float(note["pitch_midi"]) for note in source["notes"]),
                            source["voice_part_id"]),
    )
    if not chord_sources:
        return None, {}, None
    evidence = {
        "need": {source["voice_part_id"]: needs[source["voice_part_id"]]
                 for source in chord_sources},
        "median_pitch_midi": {source["voice_part_id"]: median(
            float(note["pitch_midi"]) for note in source["notes"]
        ) for source in chord_sources},
    }
    if sum(needs[source["voice_part_id"]] for source in chord_sources) > len(lanes):
        return (), evidence, "chord_claims_exceed_lanes"

    native = dict(segment.signature.lanes)
    source_voice = _source_voices(score, segment.part_index)
    whole_part = score["parts"][segment.part_index]
    heights: dict[str, float | None] = {}
    native_counts: dict[str, int] = {}
    for lane in lanes:
        notes = _lane_own_notes(whole_part, lane, source_voice)
        heights[lane.lane_id] = median(float(note["pitch_midi"]) for note in notes) if notes else None
        native_counts[lane.lane_id] = sum(
            segment.start_measure <= int(note["measure_number"]) <= segment.end_measure
            for note in notes
        )
    ordered_lanes = sorted(lanes, key=lambda lane: (
        heights[lane.lane_id] is None,
        -(heights[lane.lane_id] or 0), lane.default_rank,
    ))
    mandatory = {
        source["voice_part_id"]: [lane for lane in ordered_lanes
                                  if native[lane.lane_id]
                                  and lane.source_voice_part_id == source["voice_part_id"]]
        for source in chord_sources
    }
    if any(len(mandatory[source["voice_part_id"]]) > needs[source["voice_part_id"]]
           for source in chord_sources):
        return (), evidence, "chord_claims_exceed_lanes"
    mandatory_ids = {lane.lane_id for group in mandatory.values() for lane in group}
    owner_count = len(mandatory_ids)
    silent_count = sum(not native[lane.lane_id] for lane in lanes)
    minimum_moved = max(0, sum(needs[source["voice_part_id"]] for source in chord_sources)
                        - owner_count - silent_count)

    options: list[Option] = []
    seen: set[tuple] = set()

    def add_options(cover: dict[str, str]) -> None:
        source_by_lane: dict[str, dict] = {}
        for lane in ordered_lanes:
            selected = cover.get(lane.lane_id)
            if selected is None:
                if native[lane.lane_id]:
                    selected = lane.source_voice_part_id
                else:
                    melody = _melody_options(lane, candidates)
                    if not melody:
                        return
                    ref = melody[0].fields["melody_source"]
                    source = next((item for item in candidates
                                   if item["part_index"] == ref["part_index"]
                                   and item["voice_part_id"] == ref["voice_part_id"]), None)
                    if source is None:
                        return
                    source_by_lane[lane.lane_id] = source
                    continue
            source = next((item for item in candidates
                           if item["part_index"] == segment.part_index
                           and item["voice_part_id"] == selected), None)
            if source is None:
                return
            source_by_lane[lane.lane_id] = source
        groups: dict[tuple[int, str], list[Lane]] = {}
        for lane in ordered_lanes:
            source = source_by_lane[lane.lane_id]
            groups.setdefault((source["part_index"], source["voice_part_id"]), []).append(lane)
        ranked_groups = [group for group in groups.values() if len(group) > 1]
        for chosen_ranks in product(*(permutations(range(len(group))) for group in ranked_groups)):
            ranks = {lane.lane_id: rank for group, rank_tuple in zip(ranked_groups, chosen_ranks)
                     for lane, rank in zip(group, rank_tuple)}
            key = tuple((source_by_lane[lane.lane_id]["part_index"],
                         source_by_lane[lane.lane_id]["voice_part_id"], ranks.get(lane.lane_id))
                        for lane in ordered_lanes)
            if key in seen:
                continue
            seen.add(key)
            moved = any(native[lane.lane_id] and lane.lane_id in cover
                        and cover[lane.lane_id] != lane.source_voice_part_id for lane in lanes)
            options.append(Option(
                option_id=f"va:{len(options)}", label=f"Voice assignment {len(options) + 1}",
                fields={
                    "melody_source_by_lane": {
                        lane.lane_id: {"part_id": source_by_lane[lane.lane_id]["part_id"],
                                       "part_index": source_by_lane[lane.lane_id]["part_index"],
                                       "voice_part_id": source_by_lane[lane.lane_id]["voice_part_id"]}
                        for lane in ordered_lanes
                    },
                    "rank_index_by_lane": ranks,
                },
                evidence={"need": evidence["need"], "lane_height_midi": heights},
                flags=("P1 drops_native_notes",) if moved else (),
            ))
            if len(options) > 24:
                return

    def allocate(index: int, cover: dict[str, str]) -> None:
        if len(options) > 24:
            return
        if index == len(chord_sources):
            moved = sum(native[lane.lane_id] and cover.get(lane.lane_id) != lane.source_voice_part_id
                        for lane in lanes if lane.lane_id in cover)
            if moved == minimum_moved:
                add_options(cover)
            return
        source = chord_sources[index]
        source_id = source["voice_part_id"]
        owners = mandatory[source_id]
        need = needs[source_id] - len(owners)
        available = [lane for lane in ordered_lanes
                     if lane.lane_id not in cover and lane.lane_id not in mandatory_ids]
        available.sort(key=lambda lane: (
            native[lane.lane_id],
            native_counts[lane.lane_id] if native[lane.lane_id] else 0,
            heights[lane.lane_id] is None,
            -(heights[lane.lane_id] or 0), lane.default_rank,
        ))
        for selected in combinations(available, need):
            assigned = {**cover, **{lane.lane_id: source_id for lane in owners + list(selected)}}
            allocate(index + 1, assigned)
            if len(options) > 24:
                return

    allocate(0, {})
    if len(options) > 24:
        return (), evidence, "voice_assignment_over_24_options"
    if not options:
        return (), evidence, "chord_claims_exceed_lanes"
    return tuple(options), evidence, None


def _lane_own_notes(
    part: dict, lane: Lane, source_voice: dict[str, str], *, rests: bool = False,
) -> list[dict]:
    """What the score writes in the lane's own voice on its staff: sung notes, or its rests."""
    return [note for note in part.get("notes") or []
            if (bool(note.get("is_rest")) if rests else _is_countable_sung_note(note))
            and _staff_scope_key(note) == lane.scope_key
            and _voice_key(note.get("voice")) == source_voice[lane.source_voice_part_id]]


def _source_voices(score: dict, part_index: int) -> dict[str, str]:
    return {vp["voice_part_id"]: str(vp["source_voice_id"])
            for vp in _analyze_part_voice_parts(score["parts"][part_index], part_index)["voice_parts"]}


def _sounding(note: dict) -> tuple[float, float]:
    start = float(note.get("offset_beats") or 0.0)
    return start, start + float(note.get("duration_beats") or 0.0)


def _pitch_order_when_clear(
    score: dict, segment: Segment, lanes: list[Lane], options: tuple[Option, ...],
) -> Option | None:
    """The pitch-order assignment when nothing in the score could favour another one.

    The chooser keeps pitch order unless the voices cross, so code keeps it itself
    when the options differ only in which rank each lane takes (same voices, same
    flags), every lane has its own typical pitch and no two are equal, and the lanes'
    own notes do not cross in these bars or the bar on either side.
    """
    if len(options) < 2:
        return None
    first = options[0]
    if any(option.fields["melody_source_by_lane"] != first.fields["melody_source_by_lane"]
           or option.flags != first.flags for option in options):
        return None
    heights = first.evidence.get("lane_height_midi") or {}
    if any(heights.get(lane.lane_id) is None for lane in lanes) or \
            len({heights[lane.lane_id] for lane in lanes}) < len(lanes):
        return None
    ordered = sorted(lanes, key=lambda lane: -heights[lane.lane_id])
    groups: dict[tuple, list[str]] = {}
    for lane in ordered:
        source = first.fields["melody_source_by_lane"].get(lane.lane_id) or {}
        groups.setdefault((source.get("part_index"), source.get("voice_part_id")), []).append(lane.lane_id)
    expected = {lane_id: rank for group in groups.values() if len(group) > 1
                for rank, lane_id in enumerate(group)}
    chosen = next((option for option in options
                   if option.fields["rank_index_by_lane"] == expected), None)
    if chosen is None:
        return None
    part = score["parts"][segment.part_index]
    source_voice = _source_voices(score, segment.part_index)
    window = range(segment.start_measure - 1, segment.end_measure + 2)
    own = [[(*_sounding(note), float(note["pitch_midi"]))
            for note in _lane_own_notes(part, lane, source_voice)
            if int(note.get("measure_number") or 0) in window] for lane in ordered]
    for upper_index, upper in enumerate(own):
        for lower in own[upper_index + 1:]:
            if any(low_start < up_end and up_start < low_end and low_pitch > up_pitch
                   for up_start, up_end, up_pitch in upper
                   for low_start, low_end, low_pitch in lower):
                return None
    return chosen


def _bar_lengths(part: dict) -> dict[int, float]:
    """Each bar's length in beats, from where the part's notes and rests put it."""
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for note in part.get("notes") or []:
        measure = int(note.get("measure_number") or 0)
        start, end = _sounding(note)
        starts[measure] = min(starts.get(measure, start), start)
        ends[measure] = max(ends.get(measure, end), end)
    ordered = sorted(starts)
    return {measure: (starts[following] if following is not None else ends[measure]) - starts[measure]
            for measure, following in zip(ordered, [*ordered[1:], None])}


def _falls_back(count: int, rank: int | None) -> bool:
    """Whether the executor uses rank_fallback at an onset of `count` notes.

    It does only where a chord has no note at this lane's rank. A lone note is a
    unison, decided by the lane's shared-note policy or a unison answer, never by the fallback.
    """
    return rank is not None and 2 <= count <= rank


def _single_note_beats(
    score: dict, segment: Segment, lane: Lane, source_ref: dict, source_notes: list[dict],
) -> set[tuple[int, float]]:
    """Beats where the voice this lane splits has a single note: a unison.

    Where the lane's own voice writes a note or a rest, the executor keeps that
    instead, so those beats are left out.
    """
    onsets: dict[tuple[int, float], list[dict]] = {}
    for note in source_notes:
        key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
        onsets.setdefault(key, []).append(note)
    singles = [notes[0] for notes in onsets.values() if len(notes) == 1]
    own: list[tuple[float, float]] = []
    if singles and source_ref["part_index"] == lane.part_index \
            and source_ref["voice_part_id"] != lane.source_voice_part_id:
        part = score["parts"][lane.part_index]
        voices = _source_voices(score, lane.part_index)
        own = [_sounding(note) for rests in (False, True)
               for note in _lane_own_notes(part, lane, voices, rests=rests)
               if segment.start_measure <= int(note.get("measure_number") or 0) <= segment.end_measure]
    beats: set[tuple[int, float]] = set()
    for note in singles:
        start, end = _sounding(note)
        if not any(own_start < end - 1e-6 and start < own_end - 1e-6 for own_start, own_end in own):
            beats.add((int(note["measure_number"]), start))
    return beats


def _greedy_when_clear(
    score: dict, segment: Segment, lane: Lane, source_notes: list[dict], rank: int,
) -> bool:
    """Whether doubling a neighbouring note is the only reading where the chord thins.

    The chord thins where the fallback is used (see _falls_back). On a shared staff
    such a thinning is sung in unison unless the score says this lane stops: a
    direction in these bars ("solo", "1."), a rest written in the lane's own voice,
    or a thinning that lasts a whole bar, which may be a real drop-out.
    """
    onsets: dict[float, list[dict]] = {}
    for note in source_notes:
        onsets.setdefault(round(float(note.get("offset_beats") or 0.0), 6), []).append(note)
    times = sorted(onsets)
    thin = [time for time in times if _falls_back(len(onsets[time]), rank)]
    if thin and segment.signature.annotation is not None:
        return False
    if not thin:
        return True
    part = score["parts"][segment.part_index]
    rests = [_sounding(note) for note in _lane_own_notes(
        part, lane, _source_voices(score, segment.part_index), rests=True)]
    if any(start - 1e-6 <= time < end - 1e-6 for time in thin for start, end in rests):
        return False
    bar_lengths = _bar_lengths(part)
    index = 0
    while index < len(times):
        if not _falls_back(len(onsets[times[index]]), rank):
            index += 1
            continue
        run_start, first_note = times[index], onsets[times[index]][0]
        run_notes: list[dict] = []
        while index < len(times) and _falls_back(len(onsets[times[index]]), rank):
            run_notes.extend(onsets[times[index]])
            index += 1
        run_end = times[index] if index < len(times) else max(_sounding(note)[1] for note in run_notes)
        bar = bar_lengths.get(int(first_note.get("measure_number") or 0))
        if bar is None or run_end - run_start >= bar - 1e-6:
            return False
    return True


def _separate_voice_assignment(
    score: dict, segment: Segment, lanes: list[Lane], candidates: list[dict],
) -> tuple[Option, ...]:
    """Give each sounding voice only its own lanes, in pitch order."""
    local = {(source["part_index"], source["voice_part_id"]): source
             for source in candidates if source["part_index"] == segment.part_index
             and segment.scope_key in source["scopes"]}
    default_id = _voice_part_ids(score["parts"][segment.part_index], segment.part_index).get("_default")
    default = local.get((segment.part_index, default_id)) if default_id else None
    default_peak = _peak_onset_size(
        note for note in default["notes"] if _staff_scope_key(note) == segment.scope_key
    ) if default is not None else 0
    mapping: dict[str, dict] = {}
    ranks: dict[str, int] = {}
    source_ordinals: dict[str, int] = {}
    for lane in lanes:
        source = local.get((segment.part_index, lane.source_voice_part_id))
        chosen = source if source is not None else (default if lane.default_rank < default_peak else None)
        if chosen is None:
            continue
        mapping[lane.lane_id] = {
            "part_id": chosen["part_id"], "part_index": chosen["part_index"],
            "voice_part_id": chosen["voice_part_id"],
        }
        source_id = chosen["voice_part_id"]
        ranks[lane.lane_id] = source_ordinals.get(source_id, 0)
        source_ordinals[source_id] = ranks[lane.lane_id] + 1
    return (Option("va:0", "Voice assignment 1", {
        "melody_source_by_lane": mapping, "rank_index_by_lane": ranks,
    }, {"lane_height_midi": {lane.lane_id: None for lane in lanes}}, ()),)


PINNABLE_KINDS = ("lane_layout", "mode", "melody_source", "voice_assignment")


def _unison_default_slot(score: dict, sheet: Sheet, lane: Lane, one_line: list[Segment]) -> Slot:
    """Asked once for a lower lane: does it sing the part's one line with the top lane?

    The answer depends on who sings the part (a group or one person), which the LLM reads from the
    part name and the score; no counts are given. every_lane writes what the lane would get without
    the question, so a first-option plan is unchanged.
    """
    staff = [segment for segment in sheet.segments
             if segment.part_index == lane.part_index and segment.scope_key == lane.scope_key]
    span = Segment(lane.part_index, lane.scope_key, staff[0].start_measure, staff[-1].end_measure,
                   staff[0].signature)
    freeform = any(slot.kind == "freeform" and lane.lane_id in slot.lane_ids for slot in sheet.slots)
    every = ({"split_shared_note_policy": "duplicate_to_all"}
             if not freeform and _shared_notes_in_scope(score, lane) else {})
    top = {} if freeform else {"split_shared_note_policy": "assign_primary_only"}
    return _choice_slot((lane.lane_id,), span, "unison_default", (
        Option("every_lane", "every_lane", every, {}, ()),
        Option("top_lane_only", "top_lane_only", top, {}, ()),
    ), evidence={
        # These bars get their own unison question.
        "direction_bars": sorted(measure for segment in one_line if segment.signature.annotation is not None
                                 for measure in range(segment.start_measure, segment.end_measure + 1)),
    })


def _lane_layout_evidence(score: dict, part_index: int, scope: str, peak: int,
                          shared: int, most: int) -> dict:
    part = score["parts"][part_index]
    ids = _voice_part_ids(part, part_index)
    by_voice: dict[str, list[dict]] = {}
    for note in part.get("notes") or []:
        if _is_countable_sung_note(note) and _staff_scope_key(note) == scope:
            voice = _voice_key(note.get("voice"))
            if voice != "_default":
                by_voice.setdefault(voice, []).append(note)
    voices: dict[str, dict[str, int]] = {}
    for voice, notes in sorted(by_voice.items()):
        voices[ids[voice]] = {
            "notes": len(notes),
            "bars": len({int(note["measure_number"]) for note in notes}),
            "own_peak": _peak_onset_size(notes),
            "notes_with_words": sum(_word_text(note) is not None for note in notes),
        }
    return {"part_name": str(part.get("part_name") or ""), "staff_peak": peak,
            "lanes_if_shared": shared, "lanes_if_separate": most, "voices": voices}


def build_sheet(
    score: dict, planning_context: dict | None, *, pinned: dict[str, str] | None = None,
    cuts: dict[tuple[int, str], set[int]] | None = None,
    reopen: dict[str, list[tuple[int, int]]] | None = None,
) -> Sheet:
    """Enumerate deterministic defaults and legal choices for each segment.

    Lyrics follow the reading rule: when every melody option reads from staffs
    with a lyric line, code reads the words by beat and the sheet only asks which
    line (``lyric_line``) where a staff has two. Otherwise the lyric source and
    strategy menus remain; a non-native lane's lyric choices use its default
    melody source for coverage and onset evidence, and with no word-lyric donor
    its fallback lyric options mirror its melody options.

    ``pinned`` maps lane_layout / mode / melody_source / voice_assignment slot ids to a chosen
    option: that slot keeps only the pinned option (so it becomes fixed), and the
    slots that depend on it are built from it instead of from the first option.
    ``cuts`` adds segment starts (see ``build_sheet_skeleton``).

    Code answers a voice assignment or rank fallback itself when the score allows
    only one reading (see ``_pitch_order_when_clear`` and ``_greedy_when_clear``).
    ``reopen`` maps a lane id to bar spans where it does not: a repair re-asks
    those bars because the checks failed with the earlier answer.
    """
    pins = dict(pinned or {})
    used_pins: set[str] = set()

    def reopened(lane_ids: Iterable[str], segment: Segment) -> bool:
        return any(start <= segment.end_measure and segment.start_measure <= end
                   for lane_id in lane_ids for start, end in (reopen or {}).get(lane_id, []))

    def pin(slot_id: str, options: tuple[Option, ...]) -> tuple[Option, ...]:
        if slot_id not in pins:
            return options
        chosen = tuple(option for option in options if option.option_id == pins[slot_id])
        if not chosen:
            raise ValueError(f"Unknown option {pins[slot_id]!r} for pinned slot {slot_id}")
        used_pins.add(slot_id)
        return chosen

    shared_skeleton = build_sheet_skeleton(score, planning_context, cuts=cuts)
    layouts: dict[tuple[int, str], str] = {}
    layout_options: dict[tuple[int, str], tuple[Option, ...]] = {}
    layout_evidence: dict[tuple[int, str], dict] = {}
    for part_index, scope in sorted({(lane.part_index, lane.scope_key)
                                     for lane in shared_skeleton.lanes}):
        counts = sheet_lane_counts(score, part_index=part_index, staff_scope=scope)
        shared = max(counts.peak, counts.real_voices)
        if counts.most <= shared:
            continue
        segments = [segment for segment in shared_skeleton.segments
                    if (segment.part_index, segment.scope_key) == (part_index, scope)]
        span = Segment(part_index, scope, segments[0].start_measure,
                       segments[-1].end_measure, segments[0].signature)
        part_id = str(score["parts"][part_index]["part_id"])
        options = pin(_slot_id((), span, "lane_layout", joint_id=f"{part_id}|{scope}"), (
            Option("shared", "shared", {}, {}, ()),
            Option("separate", "separate", {}, {}, ()),
        ))
        key = (part_index, scope)
        layout_options[key] = options
        layout_evidence[key] = _lane_layout_evidence(
            score, part_index, scope, counts.peak, shared, counts.most
        )
        if options[0].option_id == "separate":
            layouts[key] = "separate"
    skeleton = (build_sheet_skeleton(score, planning_context, cuts=cuts, lane_layouts=layouts)
                if layouts else shared_skeleton)
    sheet = Sheet(
        target_part_index=skeleton.target_part_index,
        verse_number=skeleton.verse_number,
        lanes=list(skeleton.lanes),
        segments=list(skeleton.segments),
        issues=list(skeleton.issues),
    )
    if sheet.target_part_index is None or "score_has_no_lyrics" in sheet.issues:
        if pins:
            raise ValueError(f"Unknown pinned slot ids: {sorted(pins)}")
        return sheet
    for key, options in layout_options.items():
        part_index, scope = key
        segments = [segment for segment in sheet.segments
                    if (segment.part_index, segment.scope_key) == key]
        span = Segment(part_index, scope, segments[0].start_measure,
                       segments[-1].end_measure, segments[0].signature)
        lanes = [lane.lane_id for lane in sheet.lanes
                 if (lane.part_index, lane.scope_key) == key]
        _put_slot(sheet, _choice_slot(
            tuple(lanes), span, "lane_layout", options,
            joint_id=f"{score['parts'][part_index]['part_id']}|{scope}",
            evidence=layout_evidence[key],
        ))
    derived_indices = _derived_part_indices(score)
    eligible_lyrics = _eligible_lyric_sources(score)
    eligible_by_ref = {(source["part_index"], source["voice_part_id"])
                       for source in eligible_lyrics}
    lines = staff_lyric_lines(score)
    staff_index = note_staffs_by_measure(score)
    case_cache: dict[tuple[int, str, str, str | None], dict[int, LyricBarCase]] = {}
    one_line_segments: dict[str, list[Segment]] = {}
    for segment in sheet.segments:
        lanes = [lane for lane in sheet.lanes
                 if lane.part_index == segment.part_index and lane.scope_key == segment.scope_key]
        if not lanes:
            continue
        separate = layouts.get((segment.part_index, segment.scope_key)) == "separate"
        candidates = _source_candidates(score, segment, derived_indices)
        staff_line_id = _voice_part_ids(score["parts"][segment.part_index], segment.part_index).get("_default")
        top_rank = min(lane.default_rank for lane in lanes)
        segment_slots: list[Slot] = []
        segment_one_line: list[str] = []
        freeform_reason: str | None = None
        if separate:
            assignment = _separate_voice_assignment(score, segment, lanes, candidates)
            assignment_evidence = {}
        else:
            assignment, assignment_evidence, freeform_reason = _voice_assignment_options(
                score, segment, lanes, candidates
            )
        if freeform_reason is not None:
            sheet.slots.append(_freeform_slot(segment, lanes, freeform_reason))
            continue
        if assignment is not None:
            joint_id = f"{lanes[0].part_id}|{segment.scope_key}"
            assignment_id = _slot_id((), segment, "voice_assignment", joint_id=joint_id)
            if not separate and assignment_id not in pins and not reopened(
                    (lane.lane_id for lane in lanes), segment):
                clear = _pitch_order_when_clear(score, segment, lanes, assignment)
                if clear is not None:
                    assignment = (clear,)
            assignment = pin(assignment_id, assignment)
            segment_slots.append(_choice_slot(
                tuple(option_lane.lane_id for option_lane in sorted(
                    lanes, key=lambda option_lane: (
                        assignment[0].evidence["lane_height_midi"][option_lane.lane_id] is None,
                        -(assignment[0].evidence["lane_height_midi"][option_lane.lane_id] or 0),
                        option_lane.default_rank,
                    )
                )), segment, "voice_assignment", assignment,
                joint_id=joint_id,
                evidence=assignment_evidence,
            ))
        for lane in lanes:
            native = dict(segment.signature.lanes)[lane.lane_id]
            target_notes = _target_notes(score, lane, segment)
            assigned_ref = (assignment[0].fields["melody_source_by_lane"].get(lane.lane_id)
                            if separate else None)
            staff_line = {"part_index": lane.part_index, "voice_part_id": staff_line_id}
            # A one-line bar: the staff writes single notes outside any voice, for every lane.
            one_line = (
                assignment is None and not native and segment.signature.density == 1
                and staff_line_id is not None and staff_line_id != lane.source_voice_part_id
                and any(source["part_index"] == lane.part_index and source["voice_part_id"] == staff_line_id
                        and lane.scope_key in source["scopes"] for source in candidates)
            )
            # The same music written in the voice this lane splits: that voice sings one note at a
            # time here, whatever other voices on the staff do, so none is at this lane's rank.
            split_ranks = [other.default_rank for other in lanes
                           if other.source_voice_part_id == lane.source_voice_part_id]
            own_voice = next((source for source in candidates
                              if source["part_index"] == lane.part_index
                              and source["voice_part_id"] == lane.source_voice_part_id), None)
            own_onsets: dict[tuple[int, float], int] = {}
            for note in (own_voice or {}).get("notes") or []:
                if _staff_scope_key(note) == lane.scope_key:
                    key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                    own_onsets[key] = own_onsets.get(key, 0) + 1
            split_line = (
                not one_line and assignment is None and not native
                and len(split_ranks) > 1 and lane.default_rank != min(split_ranks)
                and bool(own_onsets) and max(own_onsets.values()) == 1
            )
            if assignment is None:
                melody = _melody_options(lane, candidates)
                own = tuple(option for option in melody if option.fields["melody_source"] == {
                    "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id,
                })
                if (native or split_line and not reopened((lane.lane_id,), segment)) and own:
                    # The score states these notes are this voice's: the lane sings them.
                    melody = own
                elif one_line and not reopened((lane.lane_id,), segment):
                    # A lane sings from its own staff when the staff writes one line.
                    melody = tuple(option for option in melody
                                   if option.fields["melody_source"] == staff_line)
                melody = pin(_slot_id((lane.lane_id,), segment, "melody_source"), melody)
            elif assigned_ref is None and separate:
                melody = ()
            else:
                source_ref = assigned_ref if separate else assignment[0].fields["melody_source_by_lane"][lane.lane_id]
                chosen_source = next(source for source in candidates
                                     if source["part_index"] == source_ref["part_index"]
                                     and source["voice_part_id"] == source_ref["voice_part_id"])
                melody = (_source_option(chosen_source, "melody_source"),)
            if native and not melody:
                freeform_reason = "singing_lane_without_melody"
                break
            may_take_chord = not separate and assignment is not None and any(
                option.fields["melody_source_by_lane"][lane.lane_id]["voice_part_id"]
                in assignment_evidence["need"] for option in assignment
            )
            mode_ids = (("derive",) if assigned_ref is not None else ("rest",)) if separate else (
                ("derive",) if native or may_take_chord else (
                ("derive", "rest") if melody else ("rest",)
            ))
            if one_line:
                # The top lane sings the staff's one line; a lower lane follows its unison answers.
                mode_ids = ("derive",) if lane.default_rank == top_rank else ()
            elif split_line:
                # The lane splits its own voice by rank; its unison answers decide the single notes.
                mode_ids = ("derive",)
                segment_one_line.append(lane.lane_id)
            if mode_ids:
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "mode", pin(
                    _slot_id((lane.lane_id,), segment, "mode"), tuple(
                        Option(mode, mode, {"mode": mode}, {"has_native_notes": native}, ())
                        for mode in mode_ids
                    ),
                )))
            if one_line and lane.default_rank != top_rank:
                if segment.signature.annotation is not None:
                    # A direction here ("Solo", "Tutti") may override the lane's unison default;
                    # as_lane (a marking about something else) leaves it to the one_line marker.
                    segment_slots.append(_choice_slot((lane.lane_id,), segment, "unison", (
                        Option("as_lane", "as_lane", {}, {}, ()),
                        Option("every_lane", "every_lane", {"mode": "derive"}, {}, ()),
                        Option("top_lane_only", "top_lane_only", {"mode": "rest"}, {}, ()),
                    ), evidence={"one_line_bars": list(range(segment.start_measure, segment.end_measure + 1))}))
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "one_line", (
                    Option("lane_default", "lane_default", {}, {}, ()),
                )))
                segment_one_line.append(lane.lane_id)
            if not melody:
                continue
            if assignment is None:
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "melody_source", melody))
            melody_refs = ([option.fields["melody_source"] for option in melody] if assignment is None
                           else [option.fields["melody_source_by_lane"][lane.lane_id] for option in assignment])
            staffs_by_ref = [melody_line_staffs(staff_index, ref["part_index"], ref["voice_part_id"],
                                                segment.start_measure, segment.end_measure)
                             for ref in melody_refs]
            lyric_needed = not all(staffs and staffs <= lines.keys() for staffs in staffs_by_ref)
            if not lyric_needed:
                # Lyric rule: code reads the page; the LLM only picks a line when a staff has two.
                line_options: tuple[Option, ...] = ()
                multiple = sorted({staff for staffs in staffs_by_ref for staff in staffs
                                   if lines[staff].multiple})
                if multiple:
                    line_options = _lyric_line_options(score, lane, segment, multiple)
                    if line_options:
                        segment_slots.append(_choice_slot(
                            (lane.lane_id,), segment, "lyric_line", line_options
                        ))
            assigned_elsewhere = assignment is not None and (
                melody[0].fields["melody_source"] != {
                    "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id
                }
            )
            if not native or assigned_elsewhere:
                default_melody = melody[0].fields["melody_source"]
                target_notes = next(
                    [note for note in source["notes"]
                     if _staff_scope_key(note) == lane.scope_key]
                    for source in candidates if
                    source["part_index"] == default_melody["part_index"] and
                    source["voice_part_id"] == default_melody["voice_part_id"]
                )
            else:
                default_melody = {"part_index": lane.part_index,
                                  "voice_part_id": lane.source_voice_part_id}
            if not lyric_needed:
                default_line = None
                if line_options:
                    line_ref = line_options[0].fields["lyric_line_voice"]
                    default_line = (line_ref["part_index"], line_ref["voice_part_id"])
                ambiguous: dict[str, list[str]] = {}
                for staff in sorted(staffs_by_ref[0]):
                    if staff[0] != default_melody["part_index"]:
                        continue
                    only = line_voice_for_staff(lines[staff], staff[0], default_line)
                    case_key = (staff[0], staff[1], default_melody["voice_part_id"], only)
                    if case_key not in case_cache:
                        case_cache[case_key] = lyric_bar_cases(score, lines, *case_key)
                    cases = case_cache[case_key]
                    for bar, case in cases.items():
                        if (segment.start_measure <= bar <= segment.end_measure
                                and case.case == "ambiguous"):
                            ambiguous[str(bar)] = list(case.reasons)
                if ambiguous:
                    segment_slots.append(_choice_slot(
                        (lane.lane_id,), segment, "lyric_reading", tuple(
                            Option(value, value, {"lyric_reading": value}, {}, ())
                            for value in ("own_words_only", "share_line")
                        ), evidence={"ambiguous_bars": ambiguous},
                    ))
            if lyric_needed:
                used_when = {"used_when": "melody from a staff without a lyric line"}
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
                        fallback = (_source_option({
                            "part_id": lane.part_id,
                            "part_index": lane.part_index,
                            "voice_part_id": lane.source_voice_part_id,
                        }, "lyric_source", evidence=reason),)
                    else:
                        fallback = tuple(Option(
                            option_id=option.option_id,
                            label=option.label,
                            fields={"lyric_source": dict(option.fields["melody_source"])},
                            evidence={**reason, "pairs_with_melody_option":
                                      assignment[0].option_id if assignment is not None else option.option_id},
                            flags=(),
                        ) for option in melody)
                    lyric = tuple(option for option in fallback
                                  if (option.fields["lyric_source"]["part_index"],
                                      option.fields["lyric_source"]["voice_part_id"]) in eligible_by_ref)
                    if not lyric:
                        ordered = sorted(eligible_lyrics, key=lambda source: (
                            0 if source["part_index"] == lane.part_index
                            and lane.scope_key in source["scopes"] else
                            1 if source["part_index"] == lane.part_index else 2,
                            -source["lyric_note_count"],
                            source["part_index"], source["voice_part_id"],
                        ))
                        lyric = tuple(_source_option(
                            source, "lyric_source",
                            evidence={"reason": "no_lyrics_in_source_voice"},
                        ) for source in ordered)
                    if not lyric:
                        freeform_reason = "no_valid_lyric_source"
                        break
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "lyric_source", lyric,
                                                  evidence=used_when))
                own_native = (
                    lyric[0].fields["lyric_source"] == {
                        "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id
                    } and native
                    and any(_lyric_kind(note) == "word" for note in _target_notes(score, lane, segment))
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
                    ), evidence=used_when))
            needs_fallback = False
            fallback_by_code = False
            if separate:
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "rank_fallback", (
                    Option("skip", "skip", {"rank_fallback": "skip"}, {}, ()),
                )))
                continue
            if assignment is not None:
                for option in assignment:
                    mapping = option.fields["melody_source_by_lane"]
                    source_ref = mapping[lane.lane_id]
                    rank = option.fields["rank_index_by_lane"].get(lane.lane_id)
                    source = next(item for item in candidates
                                  if item["part_index"] == source_ref["part_index"]
                                  and item["voice_part_id"] == source_ref["voice_part_id"])
                    counts: dict[tuple[int, float], int] = {}
                    for note in source["notes"]:
                        if _staff_scope_key(note) != lane.scope_key:
                            continue
                        key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                        counts[key] = counts.get(key, 0) + 1
                    if any(_falls_back(count, rank) for count in counts.values()):
                        needs_fallback = True
                        break
                if needs_fallback and len(assignment) == 1 and not reopened((lane.lane_id,), segment):
                    mapping = assignment[0].fields["melody_source_by_lane"]
                    rank = assignment[0].fields["rank_index_by_lane"].get(lane.lane_id)
                    source_ref = mapping[lane.lane_id]
                    source = next(item for item in candidates
                                  if item["part_index"] == source_ref["part_index"]
                                  and item["voice_part_id"] == source_ref["voice_part_id"])
                    needs_fallback = rank is not None and not _greedy_when_clear(
                        score, segment, lane,
                        [note for note in source["notes"] if _staff_scope_key(note) == lane.scope_key],
                        rank,
                    )
                    fallback_by_code = not needs_fallback
            else:
                siblings = [other for other in lanes
                            if other.source_voice_part_id == lane.source_voice_part_id]
                if len(siblings) > 1 and target_notes:
                    counts: dict[tuple[int, float], int] = {}
                    for note in target_notes:
                        key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0), 6))
                        counts[key] = counts.get(key, 0) + 1
                    # The rank_assignment answer may give this lane any rank among its siblings.
                    needs_fallback = any(_falls_back(count, len(siblings) - 1) for count in counts.values())
            if needs_fallback or fallback_by_code:
                # Code's answer stays on the sheet as a fixed greedy, so a repair can show it.
                segment_slots.append(_choice_slot((lane.lane_id,), segment, "rank_fallback", tuple(
                    Option(value, value, {"rank_fallback": value}, {}, ())
                    for value in (("greedy", "skip") if needs_fallback else ("greedy",))
                )))
            # Every lane sings a unison by default; a direction in these bars ("Solo", "1.")
            # may say only the top lane does, so the LLM reads it. The top lane always sings.
            if segment.signature.annotation is not None:
                beats: set[tuple[int, float]] = set()
                if assignment is not None:
                    for option in assignment:
                        if not option.fields["rank_index_by_lane"].get(lane.lane_id):
                            continue
                        source_ref = option.fields["melody_source_by_lane"][lane.lane_id]
                        source = next(item for item in candidates
                                      if item["part_index"] == source_ref["part_index"]
                                      and item["voice_part_id"] == source_ref["voice_part_id"])
                        beats |= _single_note_beats(
                            score, segment, lane, source_ref,
                            [note for note in source["notes"] if _staff_scope_key(note) == lane.scope_key],
                        )
                else:
                    siblings = sorted((other for other in lanes
                                       if other.source_voice_part_id == lane.source_voice_part_id),
                                      key=lambda other: other.default_rank)
                    if len(siblings) > 1 and siblings[0] is not lane and (segment.signature.density > 1 or split_line):
                        beats = _single_note_beats(score, segment, lane, default_melody, target_notes)
                if beats:
                    segment_slots.append(_choice_slot((lane.lane_id,), segment, "unison", (
                        Option("as_lane", "as_lane", {}, {}, ()),
                        Option("every_lane", "every_lane", {}, {}, ()),
                        Option("top_lane_only", "top_lane_only",
                               {"split_shared_note_policy": "assign_primary_only"}, {}, ()),
                    ), evidence={"single_note_bars": sorted({measure for measure, _ in beats})}))
        if not separate and freeform_reason is None and assignment is None and segment.signature.density > 1:
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
            for lane_id in segment_one_line:
                one_line_segments.setdefault(lane_id, []).append(segment)

    for lane in sheet.lanes:
        if layouts.get((lane.part_index, lane.scope_key)) == "separate":
            lane_segments = [segment for segment in sheet.segments
                             if segment.part_index == lane.part_index and segment.scope_key == lane.scope_key]
            span = Segment(lane.part_index, lane.scope_key,
                           lane_segments[0].start_measure, lane_segments[-1].end_measure,
                           lane_segments[0].signature)
            _put_slot(sheet, _choice_slot((lane.lane_id,), span, "split_shared_note_policy", (
                Option("assign_primary_only", "assign_primary_only",
                       {"split_shared_note_policy": "assign_primary_only"}, {}, ()),
            )))
            continue
        if lane.lane_id in one_line_segments:
            _put_slot(sheet, _unison_default_slot(score, sheet, lane, one_line_segments[lane.lane_id]))
            continue
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
        # A shared notehead is stated for every voice on it.
        _put_slot(sheet, _choice_slot((lane.lane_id,), span, "split_shared_note_policy", (
            Option("duplicate_to_all", "duplicate_to_all",
                   {"split_shared_note_policy": "duplicate_to_all"}, {}, ()),
        )))
    if pins.keys() - used_pins:
        raise ValueError(f"Unknown pinned slot ids: {sorted(pins.keys() - used_pins)}")
    sheet.slots.sort(key=lambda slot: (slot.start_measure, slot.slot_id))
    return sheet
