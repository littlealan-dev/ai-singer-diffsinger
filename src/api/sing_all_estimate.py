"""Read the score once to find Sing All rows and unresolved decisions."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from music21 import pitch

from src.api.preprocess_evidence import trim_musicxml
from src.api.preprocess_sheet import _lane_layout_evidence, build_sheet_skeleton
from src.api.voice_parts import part_needs_split_signal, sheet_lane_counts
from src.musicxml.parser import _summarize_weighted_pitch_range


def _has_words(part: Mapping[str, Any]) -> bool:
    return bool(part.get("has_lyrics"))


def _part_kind(part: Mapping[str, Any]) -> str:
    routes = part.get("instruments") or []
    if _has_words(part) or any(route.get("is_explicit_vocal") for route in routes):
        return "voice"
    if any(route.get("instrumental_role") == "not_instrumental" for route in routes):
        return "voice"
    if any(route.get("playback_preset") or route.get("instrumental_role") == "instrumental"
           for route in routes):
        return "instrument"
    if any(not route.get("synthetic", False) and not route.get("instrumental_candidate")
           for route in routes):
        return "instrument"
    return "undecided"


def _row(part: Mapping[str, Any], *, source_part_id: str, split: bool = False,
         row_id: str | None = None, name: str | None = None,
         part_id: str | None = None, estimated_range: Mapping[str, Any] | None = None) -> dict[str, Any]:
    key = row_id or str(part.get("part_id") or "")
    has_words = split or _has_words(part)
    return {
        "rowId": key,
        "sourcePartId": source_part_id,
        "partId": part_id if split else (part_id or key),
        "name": name or str(part.get("part_name") or key),
        "split": split,
        "hasWords": has_words,
        "note": None if has_words else "no_words",
        "voice": None,
        "voicePicked": "recommended",
        "pitchRange": dict(estimated_range or part.get("pitch_range") or {}),
    }


def _visible_derived_by_source(score: Mapping[str, Any]) -> dict[int, list[int]]:
    parts = score.get("parts") or []
    result: dict[int, list[int]] = {}
    transforms = score.get("voice_part_transforms") or {}
    if not isinstance(transforms, Mapping):
        return result
    for transform in transforms.values():
        if not isinstance(transform, Mapping):
            continue
        appended = transform.get("appended_part_ref")
        lane = transform.get("derived_lane")
        if not isinstance(appended, Mapping) or not isinstance(lane, Mapping):
            continue
        index = appended.get("part_index")
        source = lane.get("source")
        source_index = source.get("part_index") if isinstance(source, Mapping) else None
        if (appended.get("hidden_default_lane") or not isinstance(index, int)
                or index < 0 or index >= len(parts) or not isinstance(source_index, int)):
            continue
        result.setdefault(source_index, []).append(index)
    return {key: sorted(set(value)) for key, value in result.items()}


def _staff(note: Mapping[str, Any]) -> str:
    return str(note.get("staff") or "1")


def _range_for_lane(score: Mapping[str, Any], part_index: int, scope: str,
                    lane: Any, rank: int, *, shared: bool,
                    rows_in_pool: int, voice_id: str | None) -> dict[str, Any]:
    notes = [note for note in (score["parts"][part_index].get("notes") or [])
             if isinstance(note, dict) and not note.get("is_rest")
             and isinstance(note.get("pitch_midi"), (int, float))
             and _staff(note) == scope
             and (shared or str(note.get("voice") or "_default") == str(voice_id))]
    by_onset: dict[tuple[int, float], list[dict[str, Any]]] = {}
    for note in notes:
        key = (int(note.get("measure_number") or 0), float(note.get("offset_beats") or 0.0))
        by_onset.setdefault(key, []).append(note)
    weighted: list[tuple[float, float, str]] = []
    for group in by_onset.values():
        ordered = sorted(group, key=lambda item: float(item["pitch_midi"]), reverse=True)
        chosen = ordered[min(rank, len(ordered) - 1)]
        midi = float(chosen["pitch_midi"])
        weighted.append((midi, max(float(chosen.get("duration_beats") or 0.0), 0.001),
                         pitch.Pitch(midi=midi).nameWithOctave))
    return _summarize_weighted_pitch_range(weighted) or {}


def _lane_rows(score: Mapping[str, Any], part: Mapping[str, Any],
               part_index: int, scope: str, skeleton: Any,
               *, shared: bool) -> list[dict[str, Any]]:
    lanes = [lane for lane in skeleton.lanes if lane.scope_key == scope]
    signal_parts = (score.get("voice_part_signals") or {}).get("parts") or []
    signal = next((item for item in signal_parts if item.get("part_index") == part_index), {})
    voice_ids = signal.get("voice_part_id_to_source_voice_id") or {}
    pool_rank: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for lane in lanes:
        pool = "shared" if shared else lane.source_voice_part_id
        rank = pool_rank.get(pool, 0)
        pool_rank[pool] = rank + 1
        voice_id = voice_ids.get(lane.source_voice_part_id)
        name = f"{part.get('part_name') or part.get('part_id')} · staff {scope} · line {len(rows) + 1}"
        rows.append(_row(
            part, source_part_id=str(part.get("part_id") or ""), split=True,
            row_id=lane.lane_id, name=name,
            estimated_range=_range_for_lane(
                score, part_index, scope, lane, rank, shared=shared,
                rows_in_pool=len(lanes), voice_id=voice_id,
            ),
        ))
    return rows


def quote_facts(score: Mapping[str, Any], summary: Mapping[str, Any],
                voicebanks: Sequence[Mapping[str, Any]], *,
                verse_number: str | None = None,
                evidence_max_chars: int = 400000) -> dict[str, Any]:
    """Build deterministic rows and the questions the quote call must answer."""
    del voicebanks  # voice compatibility belongs to the judgment stage.
    score_parts = score.get("parts") or []
    summary_parts = summary.get("parts") or []
    signals = score.get("voice_part_signals") or {}
    signal_by_index = {
        item.get("part_index"): item for item in signals.get("parts") or []
        if isinstance(item, dict)
    }
    derived = _visible_derived_by_source(score)
    selected_verse = str(score.get("selected_verse_number") or "")
    target_verse = str(verse_number or selected_verse)
    has_lyric_sources = bool(signals.get("has_lyric_sources", True))
    rows: list[dict[str, Any]] = []
    open_staffs: list[dict[str, Any]] = []
    undecided_parts: list[dict[str, Any]] = []
    instruments: list[dict[str, Any]] = []
    notes: list[str] = []
    for index, part in enumerate(summary_parts):
        if not isinstance(part, dict) or part.get("is_derived_part") or index >= len(score_parts):
            continue
        score_part = score_parts[index]
        if not isinstance(score_part, dict) or score_part.get("hidden"):
            continue
        kind = _part_kind(part)
        if kind == "undecided":
            undecided_parts.append({"partId": part.get("part_id"), "part": part})
        if kind == "instrument":
            for route in part.get("instruments") or []:
                if route.get("eligible_for_instrumental_midi") and not route.get("playback_preset"):
                    instruments.append({"partId": part.get("part_id"), "route": route})
            continue
        source_id = str(part.get("part_id") or "")
        if derived.get(index) and target_verse == selected_verse:
            for derived_index in derived[index]:
                if derived_index < len(summary_parts) and isinstance(summary_parts[derived_index], dict):
                    child = summary_parts[derived_index]
                    rows.append(_row(child, source_part_id=source_id))
            continue
        if not part_needs_split_signal(signal_by_index.get(index, {}), has_lyric_sources):
            rows.append(_row(part, source_part_id=source_id))
            continue
        if not has_lyric_sources:
            notes.append(f"{source_id}:cannot_split_without_lyrics")
            continue
        context = {"target": {"part_id": score_part.get("part_id"), "verse_number": target_verse}}
        shared_skeleton = build_sheet_skeleton(score, context)
        separate_layouts: dict[tuple[int, str], str] = {}
        scopes = sorted({_staff(note) for note in score_part.get("notes") or []
                         if isinstance(note, dict) and not note.get("is_rest")})
        for scope in scopes:
            counts = sheet_lane_counts(score, part_index=index, staff_scope=scope)
            shared = max(counts.peak, counts.real_voices)
            if counts.most <= shared:
                rows.extend(_lane_rows(score, part, index, scope, shared_skeleton, shared=True))
                continue
            separate_layouts[(index, scope)] = "separate"
            separate_skeleton = build_sheet_skeleton(score, context, lane_layouts=separate_layouts)
            candidate_rows = {
                "shared": _lane_rows(score, part, index, scope, shared_skeleton, shared=True),
                "separate": _lane_rows(score, part, index, scope, separate_skeleton, shared=False),
            }
            evidence = _lane_layout_evidence(score, index, scope, counts.peak,
                                             shared, counts.most)
            bars = {int(note.get("measure_number") or 0) for note in score_part.get("notes") or []
                    if isinstance(note, dict) and _staff(note) == scope}
            xml = trim_musicxml(score, [str(score_part.get("raw_part_id") or score_part.get("part_id"))],
                                set(sorted(bars)[:4])) if bars else ""
            if len(xml) > evidence_max_chars:
                notes.append(f"{source_id}:cannot_split_evidence_too_large")
                continue
            open_staffs.append({
                "key": f"{source_id}|{scope}", "sourcePartId": source_id,
                "staff": scope, "evidence": {**evidence, "score_xml": xml},
                "rows_if": candidate_rows,
            })
    return {
        "rows": rows, "open_staffs": open_staffs,
        "undecided_parts": undecided_parts, "instruments": instruments,
        "notes": notes,
    }
