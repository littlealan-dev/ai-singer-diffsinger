"""Pure Sing All quote and card-opening helpers."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from music21 import pitch

from src.backend.language_selection import normalize_language_code
from src.backend.credits import _user_credits_from_data
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


def singing_language_options(voicebanks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expose labels from voicebank metadata for every supported language."""
    labels: dict[str, str] = {}
    for bank in voicebanks:
        details = bank.get("language_details") or {}
        if not isinstance(details, Mapping):
            continue
        for code in supported_singing_languages([bank]):
            detail = details.get(code)
            label = detail.get("label") if isinstance(detail, Mapping) else None
            if isinstance(label, str) and label.strip():
                labels.setdefault(code, label)
    return [{"code": code, "label": labels.get(code)}
            for code in supported_singing_languages(voicebanks)]


def quote_notes(
    facts: Mapping[str, Any], judgment: Mapping[str, Any],
    summary: Mapping[str, Any], *, repeats_available: bool,
) -> list[dict[str, Any]]:
    """Present score warnings as the card's structured note values."""
    part_names = {str(part.get("part_id")): str(part.get("part_name") or part.get("part_id"))
                  for part in summary.get("parts") or [] if isinstance(part, Mapping)}
    notes: list[dict[str, Any]] = []
    for raw in facts.get("notes") or []:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        part_id, code = raw.rsplit(":", 1)
        if code in {"cannot_split_without_lyrics", "cannot_split_evidence_too_large"}:
            notes.append({"code": code, "partId": part_id,
                          "partName": part_names.get(part_id)})
    sounds = judgment.get("sounds") or {}
    missing_sound_parts: set[str] = set()
    for item in facts.get("instruments") or []:
        route = item.get("route") or {}
        route_id = str(route.get("score_instrument_id") or "")
        part_id = str(item.get("partId") or "")
        if (part_id and route_id in sounds and sounds[route_id] is None
                and part_id not in missing_sound_parts):
            missing_sound_parts.add(part_id)
            notes.append({"code": "no_sound_chosen", "partId": part_id,
                          "partName": part_names.get(part_id)})
    if not repeats_available:
        notes.append({"code": "repeats_written_order", "partId": None, "partName": None})
    return notes


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


def job_from_quote(quote: Mapping[str, Any], *, title: str, job_id: str) -> dict[str, Any]:
    """Freeze one quote into a parent with fixed child and reservation ids."""
    now = datetime.now(timezone.utc)
    items = deepcopy(quote["items"])
    for item in items:
        if item["type"] in {"split", "take"}:
            item["childJobId"] = uuid4().hex
        elif item["type"] == "instrumental":
            item["reservationId"] = f"{job_id}-instrumentals"
    rows = deepcopy(quote["rows"])
    for row in rows:
        row["partId"] = None if row.get("split") else row.get("sourcePartId")
        row["name"] = row.get("partName") or row.get("name") or row.get("sourcePartId")
    return {
        "jobId": job_id, "uid": quote["uid"], "sessionId": quote["sessionId"],
        "scoreId": quote["scoreId"], "title": title, "kind": "full",
        "status": "running", "stage": "reading", "revision": 0,
        "choices": deepcopy(quote["choices"]), "style": deepcopy(quote.get("style") or {}),
        "quote": {"quoteId": quote["quoteId"], "total": quote["total"],
                  "choicesHash": quote["choicesHash"], "scoreVersion": quote["scoreVersion"]},
        "judgment": deepcopy(quote["judgment"]), "rows": rows, "items": items,
        "cancelRequested": False, "endReason": None,
        "notes": deepcopy(quote.get("notes") or []),
        "render": {"state": "none"}, "createdAt": now, "updatedAt": now,
        "lastProgressAt": now, "endedAt": None, "finalScoreVersion": None,
    }


def progress(
    job: Mapping[str, Any], *, now: datetime | None = None,
    split_estimate_seconds: int = 240, singing_start_seconds: int = 60,
) -> dict[str, Any]:
    """Present a parent record as the public Sing All progress and receipt."""
    instant = now or datetime.now(timezone.utc)
    created = job["createdAt"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    ended = job.get("status") != "running"
    ended_at = job.get("endedAt")
    end_time = datetime.fromisoformat(ended_at) if isinstance(ended_at, str) else ended_at
    if isinstance(end_time, datetime) and end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    elapsed_until = end_time if ended and isinstance(end_time, datetime) else instant
    elapsed = max(0, int((elapsed_until - created).total_seconds()))
    items = list(job.get("items") or [])
    splits = [item for item in items if item.get("type") == "split"]
    takes = [item for item in items if item.get("type") == "take"]
    rows = [row for row in job.get("rows") or [] if row.get("include")]
    names = {str(row.get("rowId")): str(row.get("name") or "") for row in rows}
    source_names = {str(row.get("sourcePartId")): str(row.get("partName") or
                    row.get("name") or "") for row in rows}
    public_rows = [{
        "rowId": str(row["rowId"]), "name": names[str(row["rowId"])],
        "voice": str(row.get("voice") or ""),
        "voiceName": str(row.get("voiceName") or row.get("voice") or ""),
        "split": bool(row.get("split")),
        "takes": [{"mode": item["mode"], "status": item.get("status", "queued"),
                   "reason": item.get("reason")}
                  for item in takes if item.get("rowId") == row["rowId"]],
    } for row in rows]
    charged = sum(int(item.get("creditsCharged") or 0) for item in items)
    refunded = sum(max(0, int(item.get("creditsReserved") or 0)
                       - int(item.get("creditsCharged") or 0))
                   for item in items if item.get("status") in {"done", "failed", "stopped"})
    total = int((job.get("quote") or {}).get("total") or 0)
    receipt = []
    if ended:
        for item in items:
            kind = item.get("type")
            reserved = int(item.get("creditsReserved") or 0)
            item_charged = int(item.get("creditsCharged") or 0)
            receipt.append({
                "itemId": item["itemId"], "type": kind,
                "rowId": item.get("rowId") if kind == "take" else None,
                "mode": item.get("mode") if kind == "take" else None,
                "name": (names.get(str(item.get("rowId"))) if kind == "take" else
                         source_names.get(str(item.get("sourcePartId"))) if kind == "split"
                         else None),
                "credits": item_charged if item_charged else reserved,
                "outcome": "included" if reserved == 0 else
                           "charged" if item_charged else "refunded",
                "reason": item.get("reason"),
            })
    remaining = None
    if not ended:
        pending_splits = sum(item.get("status") != "done" for item in splits)
        singing_seconds = max((float(item.get("estimatedSeconds") or 0)
                               for item in takes if item.get("status") not in
                               {"done", "failed", "stopped"}), default=0)
        startup = singing_start_seconds if job.get("stage") in {
            "reading", "splitting", "solfege"} else 0
        remaining = int(pending_splits * split_estimate_seconds + startup + singing_seconds)
    choices = job.get("choices") or {}
    reason = job.get("endReason")
    if reason == "instrumental_failed":
        reason = "instrumentals_failed"
    render = job.get("render") or {}
    return {
        "jobId": job["jobId"], "songId": job["sessionId"], "title": job["title"],
        "status": job["status"], "stage": job["stage"], "endReason": reason,
        "cancelRequested": bool(job.get("cancelRequested")),
        "choices": {key: choices[key] for key in
                    ("language", "solfegeOnly", "mode", "verse", "repeats")},
        "counts": {
            "splits": {"done": sum(item.get("status") == "done" for item in splits),
                       "total": len(splits)},
            "takes": {"done": sum(item.get("status") == "done" for item in takes),
                      "failed": sum(item.get("status") in {"failed", "stopped"}
                                    for item in takes), "total": len(takes)},
        },
        "rows": public_rows,
        "credits": {"reserved": max(0, total - charged - refunded),
                    "charged": charged, "refunded": refunded},
        "receipt": receipt, "notes": deepcopy(job.get("notes") or []),
        "elapsedS": elapsed, "remainingS": remaining,
        "render": {"state": render.get("state", "none"),
                   "placeInLine": render.get("placeInLine") if render.get("state") == "waiting" else None},
        "createdAt": created.isoformat(),
        "endedAt": (ended_at.isoformat() if isinstance(ended_at, datetime)
                    else ended_at),
    }


def available_balance_from_user(user: Mapping[str, Any]) -> int:
    """Read the same monthly plus top-up availability as UserCredits."""
    return _user_credits_from_data(dict(user)).available_balance


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
        part_id = routes.get(route_id)
        role = roles.get(part_id)
        if role == "voice" or (part_id in roles and part_id in (sung_source_ids or set())):
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
    return amended


def decide(
    job: Mapping[str, Any], children: Mapping[str, Mapping[str, Any]],
    score_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Choose one idempotent parent transition without reading Firestore.

    The caller commits the returned fields with a revision check before carrying
    out actions. A repeated call then sees the new state and cannot start a
    second child or move the same reservation twice.
    """
    if job.get("status") != "running":
        return {}, []
    items = deepcopy(job.get("items") or [])
    rows = deepcopy(job.get("rows") or [])
    now = score_state.get("now")

    def finish(status: str, reason: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        actions: list[dict[str, Any]] = []
        for item in items:
            if item.get("status") in {"queued", "running"}:
                item["status"] = "stopped" if status == "cancelled" else "failed"
                item["reason"] = reason
                if item["type"] == "take":
                    actions.append({"kind": "release", "reservationId": item["childJobId"]})
                elif item["type"] == "instrumental":
                    actions.append({"kind": "release", "reservationId": item["reservationId"]})
        actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"}])
        return {"items": items, "status": status, "stage": "done", "endReason": reason}, actions

    if score_state.get("terminalFailure"):
        return finish("failed", str(score_state["terminalFailure"]))

    stage = job.get("stage")
    if score_state.get("sweep") and isinstance(now, datetime):
        stuck = False
        if stage == "splitting":
            stuck = any(
                item["type"] == "split" and item.get("status") == "running"
                and isinstance(item.get("startedAt"), datetime)
                and now - item["startedAt"] > timedelta(minutes=30)
                for item in items
            )
        elif stage == "singing" and not score_state.get("renderTransport"):
            started = job.get("singingStartedAt")
            latest = job.get("lastTakeAt") or started
            stuck = (isinstance(started, datetime) and isinstance(latest, datetime)
                     and now - latest > timedelta(minutes=20)
                     and any(item["type"] == "take" and item.get("status")
                             in {"queued", "running"} for item in items))
        if stuck:
            actions = []
            for item in items:
                if item.get("status") not in {"queued", "running"}:
                    continue
                child_id = item.get("childJobId")
                if child_id and children.get(child_id, {}).get("status") in {"queued", "running"}:
                    actions.append({"kind": "fail_stuck_child", "childJobId": child_id,
                                    "jobKind": "preprocess" if item["type"] == "split"
                                    else "synthesis"})
                item.update(status="failed", reason="stuck")
                if item["type"] == "take":
                    actions.append({"kind": "release", "reservationId": child_id})
                elif item["type"] == "instrumental":
                    actions.append({"kind": "release", "reservationId": item["reservationId"]})
            takes = [item for item in items if item["type"] == "take"]
            done = sum(item["status"] == "done" for item in takes)
            actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"}])
            return {"items": items, "status": "partial" if done else "failed",
                    "stage": "done", "endReason": "stuck"}, actions
    if job.get("cancelRequested"):
        return wind_down(job, children)
    if stage == "reading":
        if score_state.get("scoreId") != job.get("scoreId"):
            return finish("failed", "score_replaced")
        if score_state.get("verse") != job.get("choices", {}).get("verse"):
            return {}, [{"kind": "reparse_verse", "verse": job["choices"]["verse"]}]
        expected = [(row["rowId"], row.get("sourcePartId"), row.get("include")) for row in rows]
        actual = [(row["rowId"], row.get("sourcePartId"), row.get("include"))
                  for row in score_state.get("rows") or []]
        if expected != actual:
            return finish("failed", "quote_changed")
        return {"stage": "splitting"}, [{"kind": "save_assignments"}]

    if stage == "splitting":
        for item in items:
            if item["type"] != "split" or item.get("status") == "done":
                continue
            child_id = item["childJobId"]
            child = children.get(child_id)
            if child is None or child.get("status") == "queued":
                now = score_state.get("now")
                lease = item.get("startLeaseUntil")
                if (item.get("status") == "running" and isinstance(now, datetime)
                        and isinstance(lease, datetime) and lease > now):
                    return {}, []
                item["status"] = "running"
                if isinstance(now, datetime):
                    item.setdefault("startedAt", now)
                if isinstance(now, datetime):
                    item["startLeaseUntil"] = now + timedelta(seconds=60)
                return {"items": items}, [{"kind": "start_split", "itemId": item["itemId"]}]
            if child.get("status") == "running":
                return {}, []
            if child.get("status") != "completed" or child.get("reviewRequired"):
                return finish("failed", "split_failed")
            produced = {str(part["laneKey"]): part for part in child.get("parts") or []}
            affected = [row for row in rows if row.get("sourcePartId") == item["sourcePartId"]
                        and row.get("split")]
            if set(produced) != {row["rowId"] for row in affected} or len(produced) != len(affected):
                return finish("failed", "quote_changed")
            for row in affected:
                part = produced[row["rowId"]]
                row["partId"] = part["partId"]
                row["name"] = part.get("name") or row.get("name")
            item["status"] = "done"
            return {"rows": rows, "items": items}, []
        return {"stage": "solfege"}, []

    if stage == "solfege":
        if not any(item["type"] == "take" and item["mode"] == "solfege"
                   and item.get("status") == "queued" for item in items):
            return {"stage": "singing"}, []
        if not job.get("solfegePrepared"):
            now = score_state.get("now")
            lease = job.get("solfegeStartLeaseUntil")
            if isinstance(now, datetime) and isinstance(lease, datetime) and lease > now:
                return {}, []
            fields = ({"solfegeStartLeaseUntil": now + timedelta(seconds=60)}
                      if isinstance(now, datetime) else {})
            return fields, [{"kind": "add_solfege"}]
        failed_rows = set(job.get("solfegeFailedRows") or [])
        actions = []
        for item in items:
            if item["type"] == "take" and item["mode"] == "solfege" and item["rowId"] in failed_rows:
                item.update(status="failed", reason="solfege_failed")
                actions.append({"kind": "release", "reservationId": item["childJobId"]})
        if not any(item["type"] == "take" and item.get("status") == "queued" for item in items):
            return finish("failed", "nothing_to_sing")
        return {"stage": "singing", "items": items}, actions

    if stage == "singing":
        if not job.get("instrumentalsPrepared"):
            return {}, [{"kind": "prepare_instrumentals"}]
        instrumental = next((item for item in items if item["type"] == "instrumental"), None)
        if instrumental and any(item["type"] == "take" and item["status"] == "done"
                                for item in items):
            if instrumental["status"] == "queued":
                instrumental["status"] = "running"
                return {"items": items}, [{"kind": "settle_instrumental"}]
            if instrumental["status"] == "running":
                return {}, [{"kind": "settle_instrumental"}]
        for item in items:
            if item["type"] != "take" or item.get("status") in {"done", "failed", "stopped"}:
                continue
            child = children.get(item["childJobId"])
            if child is None or child.get("status") == "queued":
                if score_state.get("renderTransport"):
                    continue
                now = score_state.get("now")
                lease = item.get("startLeaseUntil")
                if (item.get("status") == "running" and isinstance(now, datetime)
                        and isinstance(lease, datetime) and lease > now):
                    continue
                item["status"] = "running"
                if isinstance(now, datetime):
                    item.setdefault("startedAt", now)
                if isinstance(now, datetime):
                    item["startLeaseUntil"] = now + timedelta(seconds=60)
                fields = {"items": items}
                if isinstance(now, datetime) and not job.get("singingStartedAt"):
                    fields["singingStartedAt"] = now
                return fields, [{"kind": "start_take", "itemId": item["itemId"]}]
            if child.get("status") == "running":
                continue
            if child.get("status") == "completed":
                item["status"] = "done"
                item["creditsCharged"] = child.get("creditsCharged", item["creditsReserved"])
            else:
                item["status"] = "stopped" if child.get("status") == "cancelled" else "failed"
                item["reason"] = child.get("error") or "take_failed"
            return {"items": items, **({"lastTakeAt": now} if isinstance(now, datetime) else {})}, []
        if instrumental and instrumental["status"] == "queued":
            if all(item["type"] != "take" or item["status"] in {"failed", "stopped"} for item in items):
                instrumental["status"] = "failed"
                return {"items": items}, [{"kind": "release", "reservationId": instrumental["reservationId"]}]
        takes = [item for item in items if item["type"] == "take"]
        if all(item["status"] in {"done", "failed", "stopped"} for item in takes):
            done = sum(item["status"] == "done" for item in takes)
            return finish("completed" if done == len(takes) else "partial" if done else "failed")
        return {}, []

    return {}, []


def wind_down(
    job: Mapping[str, Any], children: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stop unstarted work, but wait for children already past their stop point."""
    items = deepcopy(job.get("items") or [])
    actions: list[dict[str, Any]] = []
    waiting = False
    changed = False
    for item in items:
        if item.get("status") not in {"queued", "running"}:
            continue
        child = children.get(item.get("childJobId")) or {}
        child_status = child.get("status")
        if child_status == "running":
            waiting = True
            continue
        if child_status == "completed":
            item["status"] = "done"
            if item["type"] == "take":
                item["creditsCharged"] = child.get("creditsCharged", item["creditsReserved"])
        else:
            item["status"] = "stopped"
            item["reason"] = "stopped"
            if item["type"] == "take":
                actions.append({"kind": "release", "reservationId": item["childJobId"]})
            elif item["type"] == "instrumental":
                actions.append({"kind": "release", "reservationId": item["reservationId"]})
        changed = True
    if waiting:
        return ({"items": items} if changed else {}), actions
    takes = [item for item in items if item["type"] == "take"]
    completed = all(item["status"] == "done" for item in takes)
    actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"}])
    return {"items": items, "status": "completed" if completed else "cancelled",
            "stage": "done", "endReason": None if completed else "stopped"}, actions
