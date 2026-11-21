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
    INSTRUMENTAL_CREDIT_DURATION_SECONDS, MIX_CREDITS_PER_FILE, VOCAL_CREDIT_DURATION_SECONDS,
    credits_for_duration,
)
from src.musicxml.instrument_programs import (
    FLUIDR3_SOUNDFONT_ID, apply_llm_program_assignments,
)

# A job holds the one-active guard and its Song's lock in these statuses.
ACTIVE_JOB_STATUSES = frozenset({"running", "needs_decision"})
# An item in one of these won't change again: made, failed, stopped, or left out at a decision.
ENDED_ITEM_STATUSES = frozenset({"done", "failed", "stopped", "skipped"})
# How long a paused job waits for its owner before it is cancelled and refunded.
DECISION_WINDOW = timedelta(hours=24)


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
        elif item["type"] == "mixes":
            item["reservationId"] = f"{job_id}-mixes"
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


def _retry_failed(item: Mapping[str, Any]) -> bool:
    # Solfège that couldn't be added would fail the same way again.
    return (item.get("type") == "take" and item.get("status") == "failed"
            and item.get("reason") != "solfege_failed")


def retryable(job: Mapping[str, Any]) -> bool:
    """A take failed while singing, and the job hasn't been retried (step 6).

    instrumentalsPrepared is set when singing starts, so a job that failed before
    singing (a split, the score changed) has nothing a retry can repeat.
    """
    return (job.get("status") in {"partial", "failed"} and bool(job.get("instrumentalsPrepared"))
            and not job.get("retriedBy")
            and any(_retry_failed(item) for item in job.get("items") or []))


def retry_items(original: Mapping[str, Any], *, job_id: str,
                instrumental_required: bool) -> list[dict[str, Any]]:
    """The retry job's items: the takes made, carried at 0 credits, and the failed ones again.

    A failed take costs what the first quote reserved for it. The mixes are made again
    with every part; they are included (0 credits) when the first job charged its mixes.
    """
    items: list[dict[str, Any]] = []
    for item in original.get("items") or []:
        if item.get("type") != "take":
            continue
        if item.get("status") == "done":
            items.append({**deepcopy(item), "creditsReserved": 0, "creditsCharged": 0,
                          "reason": None, "carried": True})
        elif _retry_failed(item):
            items.append({**deepcopy(item), "childJobId": uuid4().hex, "status": "queued",
                          "reason": None, "creditsCharged": 0})
            for key in ("startedAt", "startLeaseUntil"):
                items[-1].pop(key, None)
    instrumental = next((item for item in original.get("items") or []
                         if item.get("type") == "instrumental"), None)
    if instrumental and instrumental_required:
        items.append({**deepcopy(instrumental), "reservationId": f"{job_id}-instrumentals",
                      "status": "queued", "reason": None, "creditsCharged": 0})
    mixes = next((item for item in original.get("items") or [] if item.get("type") == "mixes"), None)
    if mixes:
        files = int(mixes.get("files") or 0)
        included = int(mixes.get("creditsCharged") or 0) > 0
        items.append({"itemId": "mixes", "type": "mixes", "files": files,
                      "reservationId": f"{job_id}-mixes", "estimatedSeconds": 0,
                      "unitSeconds": None, "range": None,
                      "creditsReserved": 0 if included else files * MIX_CREDITS_PER_FILE,
                      "creditsCharged": 0, "status": "queued", "reason": None,
                      "included": included})
    return items


def retry_quote(original: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The mini quote the card shows before "Sing it" on a retry."""
    rows = {row["rowId"]: row for row in original.get("rows") or []}
    mixes = next((item for item in items if item["type"] == "mixes"), None)
    return {
        "jobId": original["jobId"],
        "takes": [{"rowId": item["rowId"], "name": str(rows.get(item["rowId"], {}).get("name") or ""),
                   "mode": item["mode"],
                   "voiceName": str(rows.get(item["rowId"], {}).get("voiceName") or ""),
                   "credits": int(item["creditsReserved"])}
                  for item in items if item["type"] == "take" and not item.get("carried")],
        "instrumentals": sum(int(item["creditsReserved"]) for item in items
                             if item["type"] == "instrumental"),
        "mixes": ({"files": int(mixes["files"]), "credits": int(mixes["creditsReserved"]),
                   "included": bool(mixes.get("included"))} if mixes else None),
        "total": sum(int(item["creditsReserved"]) for item in items),
    }


def retry_job(original: Mapping[str, Any], *, job_id: str,
              items: list[dict[str, Any]], notify_email: str | None) -> dict[str, Any]:
    """A new parent that sings the failed takes on the score the original ended on."""
    now = datetime.now(timezone.utc)
    job = {
        "jobId": job_id, "uid": original["uid"], "sessionId": original["sessionId"],
        "scoreId": original["scoreId"], "title": original["title"], "kind": "full",
        "retryOf": original["jobId"], "status": "running", "stage": "singing", "revision": 0,
        "choices": deepcopy(original.get("choices") or {}),
        "style": deepcopy(original.get("style") or {}),
        "judgment": deepcopy(original.get("judgment") or {}),
        "rows": deepcopy(original.get("rows") or []), "items": items,
        # The score already has the splits and the solfège the original made.
        "solfegePrepared": True,
        "solfegeSelections": deepcopy(original.get("solfegeSelections") or {}),
        "quote": {"quoteId": None, "total": sum(int(item["creditsReserved"]) for item in items),
                  "scoreVersion": original.get("finalScoreVersion")},
        "cancelRequested": False, "endReason": None,
        "notes": deepcopy(original.get("notes") or []),
        "render": {"state": "none"}, "createdAt": now, "updatedAt": now,
        "lastProgressAt": now, "endedAt": None, "finalScoreVersion": None,
    }
    if notify_email:
        job["notifyEmail"] = notify_email
    return job


def _own_takes(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    # A retry job's carried takes were sung and paid for by an earlier job.
    return [item for item in items if item.get("type") == "take" and not item.get("carried")]


def wanted_takes(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The takes the owner still wants: every take but those left out at a pause.

    A job whose wanted takes are all made is completed, not partial.
    """
    return [item for item in items if item.get("type") == "take" and item.get("status") != "skipped"]


def _as_datetime(value: Any) -> datetime | None:
    """A Firestore time, or its ISO text in tests, as an aware datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _held(item: Mapping[str, Any]) -> int:
    return int(item.get("creditsReserved") or 0) if item.get("status") in {"queued", "running"} else 0


def decision_choice(job: Mapping[str, Any], accept: Sequence[str]) -> dict[str, int]:
    """The rows sung and credits still held if the owner goes on, accepting these review parts."""
    parts = (job.get("decision") or {}).get("parts") or []
    accepted = {part["sourcePartId"] for part in parts
                if part["state"] == "review" and part["sourcePartId"] in set(accept)}
    left_out = {row_id for part in parts if part["sourcePartId"] not in accepted
                for row_id in part["rowIds"]}
    items = job.get("items") or []
    kept_rows = {item["rowId"] for item in items if item.get("type") == "take"
                 and item.get("status") in {"queued", "running"} and item["rowId"] not in left_out}
    return {"rows": len(kept_rows),
            "heldCredits": sum(_held(item) for item in items
                               if not (item.get("type") == "take" and item["rowId"] in left_out))}


def _public_decision(job: Mapping[str, Any]) -> dict[str, Any]:
    decision = job.get("decision") or {}
    parts = decision.get("parts") or []
    items = job.get("items") or []
    keep = decision_choice(job, [])
    return {
        "pause": int(decision.get("pause") or 1), "stage": decision.get("stage"),
        "deadline": (_as_datetime(decision.get("deadline")) or datetime.now(timezone.utc)).isoformat(),
        "parts": [{
            "sourcePartId": part["sourcePartId"], "name": part["name"], "state": part["state"],
            "reason": part["reason"], "message": part.get("message"),
            "madeParts": list(part.get("madeParts") or []), "rows": len(part["rowIds"]),
            "heldCredits": sum(_held(item) for item in items
                               if item.get("type") == "take" and item["rowId"] in part["rowIds"]),
        } for part in parts],
        "keep": keep,
    }


def progress(
    job: Mapping[str, Any], *, now: datetime | None = None,
    split_estimate_seconds: int = 240, singing_start_seconds: int = 60,
    emails_on: bool = False,
) -> dict[str, Any]:
    """Present a parent record as the public Sing All progress and receipt."""
    instant = now or datetime.now(timezone.utc)
    created = job["createdAt"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    ended = job.get("status") not in ACTIVE_JOB_STATUSES
    paused = job.get("status") == "needs_decision"
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
                   "reason": item.get("reason"), **({"carried": True} if item.get("carried") else {})}
                  for item in takes if item.get("rowId") == row["rowId"]],
    } for row in rows]
    charged = sum(int(item.get("creditsCharged") or 0) for item in items)
    refunded = sum(max(0, int(item.get("creditsReserved") or 0)
                       - int(item.get("creditsCharged") or 0))
                   for item in items if item.get("status") in ENDED_ITEM_STATUSES)
    total = int((job.get("quote") or {}).get("total") or 0)
    receipt = []
    if ended:
        for item in items:
            kind = item.get("type")
            reserved = int(item.get("creditsReserved") or 0)
            item_charged = int(item.get("creditsCharged") or 0)
            if kind == "mixes":
                receipt.extend(_mixes_receipt_rows(item))
                continue
            receipt.append({
                "itemId": item["itemId"], "type": kind,
                "rowId": item.get("rowId") if kind == "take" else None,
                "mode": item.get("mode") if kind == "take" else None,
                "name": (names.get(str(item.get("rowId"))) if kind == "take" else
                         source_names.get(str(item.get("sourcePartId"))) if kind == "split"
                         else None),
                "credits": item_charged if item_charged else reserved,
                "outcome": "earlier" if item.get("carried") else
                           "included" if reserved == 0 else
                           "charged" if item_charged else "refunded",
                "reason": item.get("reason"), "files": None,
            })
    remaining = None
    if not ended and not paused:
        pending_splits = sum(item.get("status") != "done" for item in splits)
        singing_seconds = max((float(item.get("estimatedSeconds") or 0)
                               for item in takes if item.get("status") not in
                               ENDED_ITEM_STATUSES), default=0)
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
        "emailOnEnd": emails_on and bool(job.get("notifyEmail")),
        "decision": _public_decision(job) if paused else None,
        "retryable": retryable(job), "retryOf": job.get("retryOf"),
        "retriedBy": job.get("retriedBy"),
        "choices": {**{key: choices[key] for key in
                       ("language", "solfegeOnly", "mode", "verse", "repeats")},
                    "mixes": choices.get("mixes") or "none"},
        "package": ({"state": job["package"]["state"],
                     "mixes": {"done": int((job["package"].get("mixes") or {}).get("done") or 0),
                               "total": int((job["package"].get("mixes") or {}).get("total") or 0)}}
                    if job.get("stage") in {"packaging", "done"} and
                    any(item.get("status") == "done" for item in takes) and
                    isinstance(job.get("package"), Mapping) else None),
        "counts": {
            "splits": {"done": sum(item.get("status") == "done" for item in splits),
                       "total": len(splits)},
            "takes": {"done": sum(item.get("status") == "done" for item in takes),
                      "failed": sum(item.get("status") in {"failed", "stopped"}
                                    for item in takes),
                      "skipped": sum(item.get("status") == "skipped" for item in takes),
                      "total": len(takes)},
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


def _mixes_receipt_rows(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The files made (charged) and the files not made (refunded), each as its own row."""
    files = int(item.get("files") or 0)
    made = int(item.get("filesMade") or 0) if item.get("status") == "done" else 0
    charged = int(item.get("creditsCharged") or 0)
    rows = []
    if item.get("included"):
        # A retry's mixes when the first job paid for its own (step 6): nothing charged or refunded.
        return [{"itemId": "mixes", "type": "mixes", "rowId": None, "mode": None, "name": None,
                 "credits": 0, "outcome": "included", "reason": item.get("reason"),
                 "files": made or files}]
    if made:
        rows.append({"itemId": "mixes", "type": "mixes", "rowId": None, "mode": None, "name": None,
                     "credits": charged, "outcome": "charged", "reason": None, "files": made})
    if files > made:
        rows.append({"itemId": "mixes:refunded", "type": "mixes", "rowId": None, "mode": None,
                     "name": None, "credits": int(item.get("creditsReserved") or 0) - charged,
                     "outcome": "refunded", "reason": item.get("reason"), "files": files - made})
    return rows


def add_mixes_item(
    items: list[dict[str, Any]], rows: Sequence[Mapping[str, Any]], *,
    mixes: str, instruments_sound: bool,
) -> dict[str, int]:
    """Count each mix choice's files for the priced takes, and price the chosen one as one item."""
    from src.backend.sing_all_mix import TakeRef, plan_mixes

    row_by_id = {row["rowId"]: row for row in rows}
    refs = [TakeRef(str(row_by_id[item["rowId"]].get("partId") or item["rowId"]),
                    str(row_by_id[item["rowId"]].get("name") or item["rowId"]),
                    item["mode"])
            for item in items if item["type"] == "take"]
    mix_files = {choice: len(plan_mixes(refs, choice, instruments_sound))
                 for choice in ("all", "full")}
    files = mix_files.get(mixes, 0)
    if files:
        items.append({
            "itemId": "mixes", "type": "mixes", "files": files,
            "estimatedSeconds": 0, "unitSeconds": None, "range": None,
            "creditsReserved": files * MIX_CREDITS_PER_FILE, "creditsCharged": 0,
            "status": "queued", "reason": None,
        })
    return mix_files


def _release_action(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The release of an item's own reservation; splits, carried takes and included mixes hold none."""
    if item.get("carried") or item.get("included"):
        return None
    if item["type"] == "take":
        return {"kind": "release", "reservationId": item["childJobId"]}
    if item["type"] in {"instrumental", "mixes"}:
        return {"kind": "release", "reservationId": item["reservationId"]}
    return None


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
    mixes = next((item for item in items if item["type"] == "mixes"), None)
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
    if mixes is not None:
        lines.append({"kind": "mixes", "files": mixes["files"], "credits": mixes["creditsReserved"]})
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


def quoted_instruments_sound(summary: Mapping[str, Any]) -> bool:
    """Predict whether M4 will have a sound after Sing it builds performance MIDI."""
    from src.backend.sing_all_package import track_sounds

    if any(sound is not None for sound in track_sounds(dict(summary))):
        return True
    resolution = summary.get("instrument_program_resolution") or {}
    instrumental_ids = set(resolution.get("instrumental_score_instrument_ids") or [])
    for part in summary.get("parts") or []:
        for instrument in part.get("instruments") or []:
            if instrument.get("score_instrument_id") not in instrumental_ids:
                continue
            if not instrument.get("eligible_for_instrumental_midi"):
                continue
            if instrument.get("program_source") == "user_assignable":
                continue
            preset = instrument.get("playback_preset") or {}
            if preset.get("program") is not None:
                return True
    return False


def decide(
    job: Mapping[str, Any], children: Mapping[str, Mapping[str, Any]],
    score_state: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Choose one idempotent parent transition without reading Firestore.

    The caller commits the returned fields with a revision check before carrying
    out actions. A repeated call then sees the new state and cannot start a
    second child or move the same reservation twice.
    """
    if job.get("status") not in ACTIVE_JOB_STATUSES:
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
                if release := _release_action(item):
                    actions.append(release)
        actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"}])
        # The owner who stops or cancels saw it end; only the unanswered pause is emailed.
        if status != "cancelled" or reason == "decision_timeout":
            actions.append({"kind": "queue_email"})
        return {"items": items, "status": status, "stage": "done", "endReason": reason}, actions

    def pause(stage: str, parts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Wait for the owner, holding every reservation (step 6)."""
        instant = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        previous = job.get("decision") or {}
        decision = {"pause": int(previous.get("pause") or 0) + 1, "stage": stage,
                    "deadline": instant + DECISION_WINDOW, "parts": parts, "answer": None}
        return ({"status": "needs_decision", "items": items, "rows": rows, "decision": decision},
                [{"kind": "append_pause_history"}, {"kind": "queue_email"}])

    def singable_without(parts: Sequence[Mapping[str, Any]]) -> bool:
        paused_rows = {row_id for part in parts for row_id in part["rowIds"]}
        return any(item["type"] == "take" and item.get("status") == "queued"
                   and item["rowId"] not in paused_rows for item in items)

    if job.get("status") == "needs_decision":
        decision = dict(job.get("decision") or {})
        if job.get("cancelRequested"):
            return finish("cancelled", "stopped")
        answer = decision.get("answer")
        if not answer:
            deadline = _as_datetime(decision.get("deadline"))
            if (score_state.get("sweep") and isinstance(now, datetime)
                    and deadline is not None and now >= deadline):
                return finish("cancelled", "decision_timeout")
            return {}, []
        if answer.get("action") == "cancel":
            return finish("cancelled", "decision_cancelled")
        accepted = set(answer.get("accept") or [])
        actions = []
        for part in decision.get("parts") or []:
            keep = part["state"] == "review" and part["sourcePartId"] in accepted
            if decision.get("stage") == "splitting":
                for item in items:
                    if item["type"] == "split" and item.get("sourcePartId") == part["sourcePartId"]:
                        item["status"] = "done" if keep else "failed"
            if keep:
                continue
            for item in items:
                if (item["type"] == "take" and item["rowId"] in part["rowIds"]
                        and item.get("status") == "queued"):
                    item.update(status="skipped", reason=part["reason"])
                    if release := _release_action(item):
                        actions.append(release)
        return {"status": "running", "items": items,
                "stage": "solfege" if decision.get("stage") == "splitting" else "singing",
                "decision": {**decision, "answer": {**answer, "appliedAt": now}}}, actions

    def enter_packaging(settled_items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from src.backend.sing_all_mix import TakeRef, plan_mixes
        from src.backend.sing_all_package import track_sounds

        row_by_id = {row["rowId"]: row for row in rows}
        refs = [TakeRef(str((row_by_id.get(item["rowId"]) or {}).get("partId")
                            or item["rowId"]),
                        str((row_by_id.get(item["rowId"]) or {}).get("name")
                            or item["rowId"]), item["mode"])
                for item in settled_items if item["type"] == "take"
                and item["status"] == "done"]
        sounds = track_sounds(score_state.get("scoreSummary") or {})
        planned = plan_mixes(refs, (job.get("choices") or {}).get("mixes") or "none",
                             any(sound is not None for sound in sounds))
        package = {"state": "mixing", "mixes": {"done": 0, "total": len(planned)},
                   "startedAt": now, "updatedAt": now}
        return {"stage": "packaging", "package": package,
                "items": settled_items}, [{"kind": "start_package"}]

    if score_state.get("terminalFailure"):
        return finish("failed", str(score_state["terminalFailure"]))

    stage = job.get("stage")
    if stage == "packaging":
        package = job.get("package") or {}
        state = package.get("state")
        updated = package.get("updatedAt")
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        stale = (score_state.get("sweep") and isinstance(now, datetime)
                 and isinstance(updated, datetime) and now - updated > timedelta(minutes=10))
        if state not in {"done", "failed"} and not stale:
            return {}, []
        takes = wanted_takes(items)
        done = sum(item.get("status") == "done" for item in takes)
        # The mixes are charged for the files made, and the rest is refunded.
        mixes_actions = []
        mixes_item = next((item for item in items if item["type"] == "mixes"
                           and item.get("status") == "queued"), None)
        if mixes_item is not None:
            made = (min(int((package.get("mixes") or {}).get("done") or 0), int(mixes_item["files"]))
                    if state == "done" and not stale else 0)
            if made and mixes_item.get("included"):
                mixes_item.update(status="done", filesMade=made, creditsCharged=0)
            elif made:
                mixes_item.update(status="done", filesMade=made,
                                  creditsCharged=made * MIX_CREDITS_PER_FILE)
                mixes_actions.append({"kind": "settle_mixes",
                                      "reservationId": mixes_item["reservationId"],
                                      "credits": made * MIX_CREDITS_PER_FILE})
            else:
                mixes_item.update(status="failed", reason="package_failed")
                if release := _release_action(mixes_item):
                    mixes_actions.append(release)
        fields, actions = finish("completed" if done == len(takes) else "partial")
        if stale:
            fields["package"] = {**package, "state": "failed"}
        if state == "done":
            actions.insert(0, {"kind": "set_opening_balance"})
        return fields, mixes_actions + actions
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
                if release := _release_action(item):
                    actions.append(release)
            done = sum(item["status"] == "done" for item in _own_takes(items))
            actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"},
                            {"kind": "queue_email"}])
            return {"items": items, "status": "partial" if done else "failed",
                    "stage": "done", "endReason": "stuck"}, actions
    if job.get("cancelRequested"):
        fields, actions = wind_down(job, children, packaging_follows=stage == "singing")
        if stage == "singing" and fields.get("status") == "completed":
            package_fields, package_actions = enter_packaging(fields["items"])
            return package_fields, [action for action in actions
                                    if action["kind"] == "release"] + package_actions
        return fields, actions
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
            if item["type"] != "split" or item.get("status") in {"done", "review", "failed"}:
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
            # A bad split no longer ends the job: every split runs, then the job pauses once.
            if child.get("status") != "completed":
                item.update(status="failed", reason="split_failed",
                            message=child.get("errorMessage") or child.get("message") or None)
                return {"items": items}, []
            produced = {str(part["laneKey"]): part for part in child.get("parts") or []}
            affected = [row for row in rows if row.get("sourcePartId") == item["sourcePartId"]
                        and row.get("split")]
            if set(produced) != {row["rowId"] for row in affected} or len(produced) != len(affected):
                item.update(status="failed", reason="parts_changed", message=None)
                return {"items": items}, []
            for row in affected:
                part = produced[row["rowId"]]
                row["partId"] = part["partId"]
                row["name"] = part.get("name") or row.get("name")
            if child.get("reviewRequired"):
                # The parts are made, but the planner's own check flagged them.
                item.update(status="review", reason="split_review",
                            message=child.get("message") or None,
                            madeParts=[row.get("name") or row["partId"] for row in affected])
            else:
                item["status"] = "done"
            return {"rows": rows, "items": items}, []
        parts = []
        for item in items:
            if item["type"] != "split" or item.get("status") not in {"review", "failed"}:
                continue
            affected = [row for row in rows if row.get("sourcePartId") == item["sourcePartId"]
                        and row.get("split")]
            parts.append({
                "sourcePartId": item["sourcePartId"],
                "name": next((str(row.get("partName") or "") for row in affected
                              if row.get("partName")), item["sourcePartId"]),
                "state": item["status"], "reason": item.get("reason") or "split_failed",
                "message": item.get("message"), "madeParts": item.get("madeParts") or [],
                "rowIds": [row["rowId"] for row in affected],
            })
        if not parts:
            return {"stage": "solfege"}, []
        if not singable_without(parts) and not any(part["state"] == "review" for part in parts):
            # Cancel would be the only answer, so there is nothing to ask.
            return finish("failed", "split_failed")
        return pause("splitting", parts)

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
        if failed_rows and (job.get("choices") or {}).get("mode") == "solfege":
            # Solfège only: those parts would get no take at all, so the owner decides.
            parts = [{"sourcePartId": str(row["partId"]),
                      "name": str(row.get("name") or row["partId"]),
                      "state": "failed", "reason": "solfege_failed", "message": None,
                      "madeParts": [], "rowIds": [row["rowId"]]}
                     for row in rows if row["rowId"] in failed_rows]
            if parts and singable_without(parts):
                return pause("solfege", parts)
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
        # Charged when this job's first take succeeds; a retry's carried takes don't count.
        if instrumental and any(item["status"] == "done" for item in _own_takes(items)):
            if instrumental["status"] == "queued":
                instrumental["status"] = "running"
                return {"items": items}, [{"kind": "settle_instrumental"}]
            if instrumental["status"] == "running":
                return {}, [{"kind": "settle_instrumental"}]
        for item in items:
            if item["type"] != "take" or item.get("status") in ENDED_ITEM_STATUSES:
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
            if all(item["type"] != "take" or item["status"] in {"failed", "stopped", "skipped"}
                   for item in items):
                instrumental["status"] = "failed"
                return {"items": items}, [{"kind": "release", "reservationId": instrumental["reservationId"]}]
        takes = [item for item in items if item["type"] == "take"]
        if all(item["status"] in ENDED_ITEM_STATUSES for item in takes):
            done = sum(item["status"] == "done" for item in _own_takes(takes))
            if not done:
                return finish("failed")
            return enter_packaging(items)
        return {}, []

    return {}, []


def wind_down(
    job: Mapping[str, Any], children: Mapping[str, Mapping[str, Any]],
    *, packaging_follows: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stop unstarted work, but wait for children already past their stop point.

    The mixes are made after the takes: they stay reserved when every take is done
    and the job goes on to packaging, and are refunded otherwise.
    """
    items = deepcopy(job.get("items") or [])
    actions: list[dict[str, Any]] = []
    waiting = False
    changed = False
    for item in items:
        if item.get("status") not in {"queued", "running"} or item["type"] == "mixes":
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
            if release := _release_action(item):
                actions.append(release)
        changed = True
    if waiting:
        return ({"items": items} if changed else {}), actions
    takes = wanted_takes(items)
    completed = all(item["status"] == "done" for item in takes)
    if not (completed and packaging_follows):
        for item in items:
            if item["type"] == "mixes" and item.get("status") == "queued":
                item.update(status="stopped", reason="stopped")
                if release := _release_action(item):
                    actions.append(release)
    actions.extend([{"kind": "clear_active"}, {"kind": "append_end_history"}])
    return {"items": items, "status": "completed" if completed else "cancelled",
            "stage": "done", "endReason": None if completed else "stopped"}, actions
