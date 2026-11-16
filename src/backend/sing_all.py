"""Pure Sing All quote and card-opening helpers."""

import hashlib
import json
from typing import Any, Mapping, Sequence

from src.backend.language_selection import normalize_language_code
from src.backend.synthesis_pricing import (
    INSTRUMENTAL_CREDIT_DURATION_SECONDS, VOCAL_CREDIT_DURATION_SECONDS,
    credits_for_duration,
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
