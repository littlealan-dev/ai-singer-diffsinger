"""Voice part analysis and preprocessing helpers for compact multi-voice scores."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence


def analyze_score_voice_parts(score: Dict[str, Any]) -> Dict[str, Any]:
    """Return multi-voice and missing-lyric signals for parsed score data."""
    parts = score.get("parts") or []
    all_sources = _collect_all_lyric_source_options(score)
    part_signals: List[Dict[str, Any]] = []
    for idx, part in enumerate(parts):
        full = _analyze_part_voice_parts(part, idx)
        part_signals.append(
            {
                "part_index": full["part_index"],
                "part_id": full["part_id"],
                "part_name": full["part_name"],
                "multi_voice_part": full["multi_voice_part"],
                "missing_lyric_voice_parts": full["missing_lyric_voice_parts"],
                "lyric_source_candidates": all_sources,
            }
        )
    return {
        "has_multi_voice_parts": any(p["multi_voice_part"] for p in part_signals),
        "has_missing_lyric_voice_parts": any(
            len(p["missing_lyric_voice_parts"]) > 0 for p in part_signals
        ),
        "parts": part_signals,
    }


def prepare_score_for_voice_part(
    score: Dict[str, Any],
    *,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    voice_part_id: Optional[str] = None,
    allow_lyric_propagation: bool = False,
    source_voice_part_id: Optional[str] = None,
    source_part_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Select/split a target voice part and optionally propagate lyrics.

    Returns either:
    - {"status": "ready", "score": transformed_score, "part_index": part_index}
    - {"status": "action_required", ...}
    """
    parts = score.get("parts") or []
    if not parts:
        return {
            "status": "action_required",
            "action": "missing_score_parts",
            "message": "Score has no parts to synthesize.",
        }
    if part_index < 0 or part_index >= len(parts):
        return {
            "status": "action_required",
            "action": "invalid_part_index",
            "message": f"part_index {part_index} is out of range.",
            "part_index": part_index,
        }

    source_score = deepcopy(score)
    part = source_score["parts"][part_index]
    analysis = _analyze_part_voice_parts(part, part_index)
    voice_parts = analysis["voice_parts"]
    if not voice_parts:
        return {
            "status": "action_required",
            "action": "missing_voice_parts",
            "message": "No voiced note events were found in the selected part.",
            "part_index": part_index,
        }

    target = _resolve_target_voice_part(voice_parts, voice_id=voice_id, voice_part_id=voice_part_id)
    if target is None:
        return {
            "status": "action_required",
            "action": "target_voice_part_not_found",
            "message": "Requested voice part was not found in the selected part.",
            "part_index": part_index,
            "voice_part_options": [vp["voice_part_id"] for vp in voice_parts],
        }

    target_voice = target["source_voice_id"]
    selected_notes = _select_part_notes_for_voice(part.get("notes") or [], target_voice)
    transformed_part = dict(part)
    transformed_part["notes"] = selected_notes
    transformed_part["part_name"] = target["voice_part_id"]

    if _part_has_any_lyric(selected_notes):
        source_score["parts"][part_index] = transformed_part
        _persist_transform_metadata(
            source_score,
            part_index=part_index,
            target_voice_part_id=target["voice_part_id"],
            source_voice_part_id=None,
            source_part_index=part_index,
            transformed_part=transformed_part,
            propagated=False,
        )
        return {"status": "ready", "score": source_score, "part_index": part_index}

    same_part_source_options = [
        _make_source_option(part_index, vp["voice_part_id"])
        for vp in voice_parts
        if vp["source_voice_id"] != target_voice and vp["lyric_note_count"] > 0
    ]
    global_source_options = _collect_global_lyric_source_options(
        source_score, exclude_part_index=part_index
    )
    source_options = same_part_source_options or global_source_options
    if not source_options:
        return {
            "status": "action_required",
            "action": "missing_lyrics_no_source",
            "message": (
                f"Voice part '{target['voice_part_id']}' has no lyrics and no lyric source "
                "voice part is available in this score."
            ),
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": [],
        }

    if not allow_lyric_propagation:
        return {
            "status": "action_required",
            "action": "confirm_lyric_propagation",
            "message": (
                f"Voice part '{target['voice_part_id']}' has no lyrics. "
                "Choose a source voice part and explicitly confirm lyric propagation."
            ),
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": source_options,
        }

    if source_part_index is None or not source_voice_part_id:
        return {
            "status": "action_required",
            "action": "select_source_voice_part",
            "message": (
                "Select a source lyric identifier with both source_part_index "
                "and source_voice_part_id."
            ),
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": source_options,
        }

    source_meta: Optional[Dict[str, Any]] = None
    source_notes: Optional[List[Dict[str, Any]]] = None
    source_part_used = source_part_index
    option_match = _match_source_option(
        source_options,
        source_part_index=source_part_index,
        source_voice_part_id=source_voice_part_id,
    )
    if option_match is None:
        return {
            "status": "action_required",
            "action": "invalid_source_voice_part",
            "message": "Selected source lyric identifier is invalid.",
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": source_options,
        }

    if source_part_index < 0 or source_part_index >= len(source_score["parts"]):
        return {
            "status": "action_required",
            "action": "invalid_source_part_index",
            "message": f"source_part_index {source_part_index} is out of range.",
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": source_options,
        }
    source_part = source_score["parts"][source_part_index]
    source_analysis = _analyze_part_voice_parts(source_part, source_part_index)
    source_meta = _find_voice_part_by_id(source_analysis["voice_parts"], source_voice_part_id)
    if source_meta is not None and source_meta["lyric_note_count"] > 0:
        source_notes = _select_part_notes_for_voice(
            source_part.get("notes") or [], source_meta["source_voice_id"]
        )

    if source_meta is None or source_notes is None:
        return {
            "status": "action_required",
            "action": "invalid_source_voice_part",
            "message": "Selected source voice part is invalid for lyric propagation.",
            "part_index": part_index,
            "target_voice_part": target["voice_part_id"],
            "source_voice_part_options": source_options,
        }

    propagated_notes = _propagate_lyrics_by_offset(
        target_notes=selected_notes,
        source_notes=source_notes,
    )
    transformed_part["notes"] = propagated_notes
    source_score["parts"][part_index] = transformed_part
    _persist_transform_metadata(
        source_score,
        part_index=part_index,
        target_voice_part_id=target["voice_part_id"],
        source_voice_part_id=source_meta["voice_part_id"],
        source_part_index=source_part_used,
        transformed_part=transformed_part,
        propagated=True,
    )
    return {"status": "ready", "score": source_score, "part_index": part_index}


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


def _find_voice_part_by_id(
    voice_parts: Sequence[Dict[str, Any]], voice_part_id: str
) -> Optional[Dict[str, Any]]:
    key = str(voice_part_id).strip().lower()
    for vp in voice_parts:
        if str(vp["voice_part_id"]).strip().lower() == key:
            return vp
    return None


def _select_part_notes_for_voice(notes: Sequence[Dict[str, Any]], voice_key: str) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for note in notes:
        note_voice = _voice_key(note.get("voice"))
        if note.get("is_rest"):
            if note_voice == voice_key:
                selected.append(dict(note))
            continue
        if note_voice == voice_key:
            selected.append(dict(note))
    return selected


def _propagate_lyrics_by_offset(
    *,
    target_notes: Sequence[Dict[str, Any]],
    source_notes: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    source_by_offset: Dict[float, List[Dict[str, Any]]] = {}
    for note in source_notes:
        if note.get("is_rest"):
            continue
        key = round(float(note.get("offset_beats") or 0.0), 6)
        source_by_offset.setdefault(key, []).append(note)

    out: List[Dict[str, Any]] = []
    for note in target_notes:
        copied = dict(note)
        if copied.get("is_rest") or _has_lyric(copied):
            out.append(copied)
            continue
        key = round(float(copied.get("offset_beats") or 0.0), 6)
        candidates = source_by_offset.get(key, [])
        source_note = next((n for n in candidates if _has_lyric(n)), None)
        if source_note is not None:
            copied["lyric"] = source_note.get("lyric")
            copied["syllabic"] = source_note.get("syllabic")
            copied["lyric_is_extended"] = bool(source_note.get("lyric_is_extended"))
        out.append(copied)
    return out


def _persist_transform_metadata(
    score: Dict[str, Any],
    *,
    part_index: int,
    target_voice_part_id: str,
    source_voice_part_id: Optional[str],
    source_part_index: Optional[int] = None,
    transformed_part: Dict[str, Any],
    propagated: bool,
) -> None:
    cache = score.setdefault("voice_part_transforms", {})
    key = (
        f"part:{part_index}|target:{target_voice_part_id}"
        f"|source_part:{source_part_index if source_part_index is not None else part_index}"
        f"|source:{source_voice_part_id or '-'}"
    )
    cache[key] = {
        "part_index": part_index,
        "target_voice_part_id": target_voice_part_id,
        "source_voice_part_id": source_voice_part_id,
        "source_part_index": source_part_index if source_part_index is not None else part_index,
        "propagated_lyrics": propagated,
        "part": transformed_part,
    }


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


def _part_has_any_lyric(notes: Sequence[Dict[str, Any]]) -> bool:
    return any(_has_lyric(note) for note in notes if not note.get("is_rest"))
