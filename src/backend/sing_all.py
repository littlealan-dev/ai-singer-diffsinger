"""Pure Sing All quote and card-opening helpers."""

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from music21 import pitch

from src.backend.language_selection import normalize_language_code
from src.backend.synthesis_pricing import (
    INSTRUMENTAL_CREDIT_DURATION_SECONDS, VOCAL_CREDIT_DURATION_SECONDS,
    credits_for_duration,
)
from src.musicxml.instrument_programs import (
    FLUIDR3_SOUNDFONT_ID, apply_llm_program_assignments,
)


def supported_singing_languages(voicebanks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the languages actually sung by enabled voicebanks."""
    supported: set[str] = set()
    for voicebank in voicebanks:
        declared = voicebank.get("languages") or ["en"]
        if isinstance(declared, list):
            supported.update(
                code for value in declared
                if (code := normalize_language_code(value)) is not None
            )
    return sorted(supported)


def open_sing_all_result(
    arguments: Mapping[str, Any], *, voicebanks: Sequence[Mapping[str, Any]],
    active_job_id: str | None = None,
) -> dict[str, Any]:
    """Validate the chat model's language before opening the card."""
    language = normalize_language_code(arguments.get("language"))
    supported = supported_singing_languages(voicebanks)
    solfege_only = arguments.get("solfegeOnly", False) is True
    reason: str | None = None
    if active_job_id:
        reason = "sing_all_already_running"
    elif language is None:
        reason = "language_required"
    elif language not in supported:
        reason = "no_voicebank_for_language"
    elif solfege_only and language != "en":
        reason = "solfege_only_needs_en"
    if reason is not None:
        return {
            "status": "action_required",
            "reason": reason,
            **({"language": language, "supported": supported}
               if reason == "no_voicebank_for_language" else {}),
        }
    return {
        "status": "ready",
        "singAll": {"language": language, "solfegeOnly": solfege_only},
    }


def choices_hash(choices: Mapping[str, Any], style: Mapping[str, Any] | None) -> str:
    """Bind the quoted work and its controls to the eventual Sing it request."""
    payload = {"choices": choices, "style": style or {}}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_items(
    rows: Sequence[Mapping[str, Any]], *, mode: str, duration_seconds: float,
    instrumental_charge_required: bool,
) -> list[dict[str, Any]]:
    """Price each included take, each needed split, and the one instrumental charge."""
    if mode not in {"lyrics", "solfege", "both"}:
        raise ValueError("Invalid singing mode.")
    vocal_credits = credits_for_duration(duration_seconds, VOCAL_CREDIT_DURATION_SECONDS)
    items: list[dict[str, Any]] = []
    split_sources: set[str] = set()
    for row in rows:
        if not row.get("include", True):
            continue
        row_id = str(row["rowId"])
        if row.get("split"):
            split_sources.add(str(row["sourcePartId"]))
        take_modes = (["solfege"] if mode == "solfege" or not row.get("hasWords")
                      else ["lyrics", "solfege"] if mode == "both" else ["lyrics"])
        for take_mode in take_modes:
            items.append({
                "itemId": f"take:{row_id}:{take_mode}", "type": "take",
                "rowId": row_id, "mode": take_mode,
                "estimatedSeconds": duration_seconds,
                "unitSeconds": VOCAL_CREDIT_DURATION_SECONDS,
                "range": None, "creditsReserved": vocal_credits,
                "creditsCharged": 0, "status": "queued", "reason": None,
            })
    for source in sorted(split_sources):
        items.append({
            "itemId": f"split:{source}", "type": "split", "sourcePartId": source,
            "estimatedSeconds": 0, "unitSeconds": None, "range": None,
            "creditsReserved": 0, "creditsCharged": 0,
            "status": "queued", "reason": None,
        })
    if instrumental_charge_required:
        items.append({
            "itemId": "instrumental", "type": "instrumental",
            "estimatedSeconds": duration_seconds,
            "unitSeconds": INSTRUMENTAL_CREDIT_DURATION_SECONDS,
            "range": None,
            "creditsReserved": credits_for_duration(
                duration_seconds, INSTRUMENTAL_CREDIT_DURATION_SECONDS),
            "creditsCharged": 0, "status": "queued", "reason": None,
        })
    return items


def quote_lines(items: Sequence[Mapping[str, Any]], *,
                duration_seconds: float, balance: int) -> list[dict[str, Any]]:
    """Return card-ready price lines from the same items used for the receipt."""
    takes = [item for item in items if item["type"] == "take"]
    split = any(item["type"] == "split" for item in items)
    instrumental = next((item for item in items if item["type"] == "instrumental"), None)
    total = sum(int(item["creditsReserved"]) for item in items)
    lines: list[dict[str, Any]] = [{
        "kind": "singing", "parts": len({item["rowId"] for item in takes}),
        "takes": len(takes), "secondsEach": duration_seconds,
        "credits": sum(int(item["creditsReserved"]) for item in takes),
    }]
    if split:
        lines.append({"kind": "preparing", "credits": 0})
    if instrumental is not None:
        lines.append({"kind": "instrumentals", "credits": instrumental["creditsReserved"]})
    lines.extend([{"kind": "total", "credits": total},
                  {"kind": "balance", "before": balance, "after": balance - total}])
    return lines


def materialize_rows(
    facts: Mapping[str, Any], judgment: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Keep only the chosen layout and fold user ticks and voice picks onto it."""
    choices = {str(item.get("rowId")): item for item in selected}
    rows = [deepcopy(row) for row in facts.get("rows") or []]
    for staff in facts.get("open_staffs") or []:
        layout = judgment.get("layouts", {}).get(staff["key"])
        if layout not in {"shared", "separate"}:
            raise ValueError("A staff layout is missing from the quote judgment.")
        rows.extend(deepcopy(row) for row in staff["rows_if"][layout])
    roles = judgment.get("roles") or {}
    voices = judgment.get("voices") or {}
    for row in rows:
        row_id = str(row["rowId"])
        role = roles.get(row["sourcePartId"])
        if role in {"instrument", "unsure"}:
            row["note"] = "likely_instrument" if role == "instrument" else "undecided"
        row["include"] = role not in {"instrument", "unsure"}
        row["voice"] = voices[row_id]
        chosen = choices.get(row_id)
        if chosen:
            row["include"] = bool(chosen.get("include", row["include"]))
            if chosen.get("voicePicked") == "user" and chosen.get("voice"):
                row["voice"] = chosen["voice"]
                row["voicePicked"] = "user"
    return rows


def mark_voice_ranges(rows: list[dict[str, Any]],
                      voicebanks: Mapping[str, Mapping[str, Any]]) -> None:
    """Flag a recommended or chosen voice outside its declared absolute range."""
    for row in rows:
        declared = voicebanks.get(str(row.get("voice")), {}).get("supported_range")
        bounds = re.findall(r"[A-G](?:#|b)?-?\d+", declared) if isinstance(declared, str) else []
        row_range = row.get("pitchRange") or {}
        low, high = row_range.get("lowest_note"), row_range.get("highest_note")
        if len(bounds) < 2 or not low or not high:
            continue
        try:
            if (pitch.Pitch(str(low)).midi < pitch.Pitch(bounds[0]).midi
                    or pitch.Pitch(str(high)).midi > pitch.Pitch(bounds[1]).midi):
                row["note"] = "voice_out_of_range"
        except (ValueError, pitch.PitchException):
            continue


def style_changes(
    rows: Sequence[Mapping[str, Any]], controls: Mapping[str, Mapping[str, Any]],
    voicebanks: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Show only departures from each row's own voicebank defaults."""
    changes: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = str(row["rowId"])
        if not row.get("include") or row_id not in controls:
            continue
        bank = voicebanks[row["voice"]]
        defaults = bank.get("synthesis_control_defaults") or {}
        current = controls[row_id]
        delta: dict[str, Any] = {}
        color = current.get("voiceColor")
        if color != bank.get("default_voice_color") and color is not None:
            delta["voiceColor"] = color
        for name in ("airiness", "clarity", "gender", "intensity"):
            value = current.get(name)
            default = defaults.get(name)
            if isinstance(value, (int, float)) and isinstance(default, (int, float)):
                difference = value - default
                if difference:
                    delta[name] = difference
        if delta:
            changes[row_id] = delta
    return changes


def summary_with_judged_instruments(
    summary: Mapping[str, Any], judgment: Mapping[str, Any],
    *, sung_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Price a copy of the score as if Sing it saved the quote's roles and sounds."""
    resolution = summary.get("instrument_program_resolution") or {}
    unresolved = resolution.get("unresolved_score_instrument_ids") or []
    roles = judgment.get("roles") or {}
    sounds = judgment.get("sounds") or {}
    routes = {
        route.get("score_instrument_id"): part.get("part_id")
        for part in summary.get("parts") or []
        for route in part.get("instruments") or []
    }
    assignments = []
    for route_id in unresolved:
        role = roles.get(routes.get(route_id))
        if role == "voice":
            assignments.append({"score_instrument_id": route_id, "source": "llm_inferred",
                                "role": "not_instrumental", "evidence": []})
            continue
        sound = sounds.get(route_id)
        assignments.append({
            "score_instrument_id": route_id, "source": "llm_inferred",
            "role": "instrumental", "evidence": (sound or {}).get("evidence") or [],
            "playback_preset": ({**sound, "soundfont_id": FLUIDR3_SOUNDFONT_ID}
                                if sound is not None else None),
        })
    amended, problem = apply_llm_program_assignments(
        summary, assignments, allow_user_choice=True)
    if problem:
        raise ValueError("The quote's instrument answers do not fit the score.")
    if sung_source_ids:
        # An undecided part the user ticks is a vocal choice, not a parallel
        # instrumental track. This copy is used only to price the quote.
        sung_route_ids = {
            route_id for route_id, part_id in routes.items()
            if part_id in sung_source_ids and part_id in roles
        }
        amended_resolution = amended.get("instrument_program_resolution") or {}
        amended_resolution["instrumental_score_instrument_ids"] = [
            route_id for route_id in amended_resolution.get("instrumental_score_instrument_ids") or []
            if route_id not in sung_route_ids
        ]
    return amended
