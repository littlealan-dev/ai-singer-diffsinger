"""Planner v2 chooser: ask the LLM for one listed option per open sheet slot.

Pure and offline: requests and replies are plain data, and the LLM call is
injected, so this module never talks to a provider itself.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, Awaitable, Callable

from src.api.preprocess_assembler import FreeformUnresolved
from src.api.preprocess_sheet import PINNABLE_KINDS, Lane, Sheet, Slot, build_sheet
from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _is_countable_sung_note,
    _staff_scope_key,
    _voice_key,
)


class ChooserError(RuntimeError):
    """The LLM's reply stayed invalid after one retry."""


@dataclass(frozen=True)
class ChooserRequest:
    dynamic_text: str            # compact JSON
    response_schema: dict        # Gemini OpenAPI subset
    key_to_slot: dict[str, str]  # "s1" -> slot_id
    lane_keys: dict[str, str]    # "L1" -> lane_id


@dataclass(frozen=True)
class ChooserStats:
    requests: int
    retries: int
    slots_asked: int
    second_pass_slots: int


RETRY_TEXT = "Your previous answer was invalid: {errors}. Answer every key with one listed id."

# Kinds whose option id alone does not say what the option does.
_KINDS_WITH_FIELDS = ("voice_assignment", "rank_assignment")


def _lane_height_midi(score: dict, lane: Lane) -> int | None:
    part = score["parts"][lane.part_index]
    voice = next((str(vp["source_voice_id"])
                  for vp in _analyze_part_voice_parts(part, lane.part_index)["voice_parts"]
                  if vp["voice_part_id"] == lane.source_voice_part_id), None)
    pitches = [float(note["pitch_midi"]) for note in part.get("notes") or []
               if _is_countable_sung_note(note) and _staff_scope_key(note) == lane.scope_key
               and _voice_key(note.get("voice")) == voice and note.get("pitch_midi") is not None]
    return round(median(pitches)) if pitches else None


def build_request_context(score: dict, planning_context: dict | None, sheet: Sheet) -> dict:
    """The request facts and lane heights that ``build_requests`` sends."""
    context = planning_context or {}
    target = context.get("target") if isinstance(context.get("target"), dict) else {}
    instruction = str(context.get("other_instruction") or "").strip()
    return {
        "request": {
            "target_part": target.get("part_id"),
            "verse": sheet.verse_number,
            "user_instruction": instruction or None,
        },
        "lane_height_midi": {lane.lane_id: _lane_height_midi(score, lane) for lane in sheet.lanes},
    }


def _lane_ids_in(value: Any, known: set[str], found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in known:
                found.add(key)
            _lane_ids_in(item, known, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _lane_ids_in(item, known, found)
    elif isinstance(value, str) and value in known:
        found.add(value)


def _rekey(value: Any, keys: dict[str, str]) -> Any:
    """Rewrite lane ids (as dict keys or string values) to their L keys."""
    if isinstance(value, dict):
        return {keys.get(key, key): _rekey(item, keys) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rekey(item, keys) for item in value]
    if isinstance(value, str):
        return keys.get(value, value)
    return value


def _sent_parts(slot: Slot) -> list[Any]:
    """The parts of a slot that go into the request, before lane ids are rewritten."""
    parts: list[Any] = [slot.evidence]
    for option in slot.options:
        parts.append(option.evidence)
        if slot.kind in _KINDS_WITH_FIELDS:
            parts.append(option.fields)
    return parts


def _request(sheet: Sheet, request_context: dict, slots: list[Slot]) -> ChooserRequest:
    known = {lane.lane_id for lane in sheet.lanes}
    used: set[str] = set()
    for slot in slots:
        used.update(slot.lane_ids)
        _lane_ids_in(_sent_parts(slot), known, used)
    lanes = [lane for lane in sheet.lanes if lane.lane_id in used]
    keys = {lane.lane_id: f"L{index}" for index, lane in enumerate(lanes, start=1)}
    heights = request_context.get("lane_height_midi") or {}
    slot_keys = {f"s{index}": slot for index, slot in enumerate(slots, start=1)}
    payload_slots = []
    for key, slot in slot_keys.items():
        entry: dict[str, Any] = {
            "key": key, "kind": slot.kind, "lanes": [keys[lane_id] for lane_id in slot.lane_ids],
            "bars": f"{slot.start_measure}-{slot.end_measure}",
        }
        if slot.evidence:
            entry["evidence"] = _rekey(slot.evidence, keys)
        options = []
        for option in slot.options:
            item: dict[str, Any] = {"id": option.option_id, "label": option.label}
            if slot.kind in _KINDS_WITH_FIELDS and option.fields:
                item["fields"] = _rekey(option.fields, keys)
            if option.flags:
                item["flags"] = list(option.flags)
            if option.evidence:
                item["evidence"] = _rekey(option.evidence, keys)
            options.append(item)
        entry["options"] = options
        payload_slots.append(entry)
    payload = {
        "request": request_context.get("request") or {},
        "lanes": [{
            "lane": keys[lane.lane_id], "part": lane.part_id, "staff": lane.scope_key,
            "voice": lane.source_voice_part_id, "height_midi": heights.get(lane.lane_id),
        } for lane in lanes],
        "slots": payload_slots,
    }
    return ChooserRequest(
        dynamic_text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        response_schema={
            "type": "object",
            "properties": {key: {"type": "string", "enum": [option.option_id for option in slot.options]}
                           for key, slot in slot_keys.items()},
            "required": list(slot_keys),
            "propertyOrdering": list(slot_keys),
        },
        key_to_slot={key: slot.slot_id for key, slot in slot_keys.items()},
        lane_keys={key: lane_id for lane_id, key in keys.items()},
    )


def build_requests(sheet: Sheet, request_context: dict, *, max_slots_per_request: int = 80) -> list[ChooserRequest]:
    """One or more requests per staff, cut only at segment boundaries."""
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    by_scope: dict[tuple[int, str], dict[tuple[int, int], list[Slot]]] = {}
    for slot in sheet.slots:
        if slot.kind == "freeform":
            continue
        lane = lanes[slot.lane_ids[0]]
        by_scope.setdefault((lane.part_index, lane.scope_key), {}).setdefault(
            (slot.start_measure, slot.end_measure), []
        ).append(slot)
    requests: list[ChooserRequest] = []
    for segments in by_scope.values():
        batch: list[Slot] = []
        for segment_slots in segments.values():
            if batch and len(batch) + len(segment_slots) > max_slots_per_request:
                requests.append(_request(sheet, request_context, batch))
                batch = []
            batch.extend(segment_slots)
        if batch:
            requests.append(_request(sheet, request_context, batch))
    return requests


def parse_reply(request: ChooserRequest, reply_text: str) -> tuple[dict[str, str], list[str]]:
    """Map a reply to slot-id choices, with every problem as a short error."""
    try:
        reply = json.loads(reply_text)
    except (TypeError, ValueError):
        return {}, ["reply: not valid JSON"]
    if not isinstance(reply, dict):
        return {}, ["reply: not a JSON object"]
    choices: dict[str, str] = {}
    errors: list[str] = []
    properties = request.response_schema["properties"]
    for key, slot_id in request.key_to_slot.items():
        if key not in reply:
            errors.append(f"{key}: missing")
        elif reply[key] not in properties[key]["enum"]:
            errors.append(f"{key}: unknown option {reply[key]!r}")
        else:
            choices[slot_id] = reply[key]
    errors.extend(f"{key}: unexpected key" for key in reply if key not in request.key_to_slot)
    return choices, errors


async def _ask(request: ChooserRequest, call: Callable[[ChooserRequest], Awaitable[str]]) -> tuple[dict[str, str], int]:
    choices, errors = parse_reply(request, await call(request))
    if not errors:
        return choices, 0
    retry = replace(request, dynamic_text=request.dynamic_text + "\n"
                    + RETRY_TEXT.format(errors="; ".join(errors)))
    choices, errors = parse_reply(retry, await call(retry))
    if errors:
        raise ChooserError(f"Invalid chooser reply after retry: {'; '.join(errors)}")
    return choices, 1


async def _ask_all(
    sheet: Sheet, request_context: dict, call: Callable[[ChooserRequest], Awaitable[str]],
    max_slots_per_request: int,
) -> tuple[dict[str, str], int, int]:
    requests = build_requests(sheet, request_context, max_slots_per_request=max_slots_per_request)
    answers = await asyncio.gather(*(_ask(request, call) for request in requests))
    choices: dict[str, str] = {}
    for answer, _ in answers:
        choices.update(answer)
    return choices, len(requests), sum(retried for _, retried in answers)


def _raise_on_freeform(sheet: Sheet) -> None:
    freeform = [slot.slot_id for slot in sheet.slots if slot.kind == "freeform"]
    if freeform:
        raise FreeformUnresolved(freeform)


def _menus(sheet: Sheet) -> dict[str, Slot]:
    return {slot.slot_id: slot for slot in sheet.slots if slot.kind != "freeform"}


async def choose(
    score: dict, planning_context: dict | None, *,
    call: Callable[[ChooserRequest], Awaitable[str]], max_slots_per_request: int = 80,
) -> tuple[Sheet, dict[str, str], ChooserStats]:
    """Choose every menu slot, re-asking the slots a non-default structure choice changes."""
    sheet = build_sheet(score, planning_context)
    _raise_on_freeform(sheet)
    context = build_request_context(score, planning_context, sheet)
    choices, requests, retries = await _ask_all(sheet, context, call, max_slots_per_request)
    slots_asked = len(_menus(sheet))
    second_pass_slots = 0

    pinned = {slot.slot_id: choices[slot.slot_id] for slot in _menus(sheet).values()
              if slot.kind in PINNABLE_KINDS and choices[slot.slot_id] != slot.options[0].option_id}
    if pinned:
        first = _menus(sheet)
        sheet = build_sheet(score, planning_context, pinned=pinned)
        _raise_on_freeform(sheet)
        changed = [slot for slot in sheet.slots if slot.kind != "freeform" and (
            slot.slot_id not in first
            or [option.option_id for option in slot.options]
            != [option.option_id for option in first[slot.slot_id].options]
            or choices.get(slot.slot_id) not in {option.option_id for option in slot.options}
        )]
        if changed:
            context = build_request_context(score, planning_context, sheet)
            again, more_requests, more_retries = await _ask_all(
                replace(sheet, slots=changed), context, call, max_slots_per_request
            )
            choices.update(again)
            requests += more_requests
            retries += more_retries
            second_pass_slots = len(changed)

    final = {slot_id: choices[slot_id] for slot_id in _menus(sheet)}
    return sheet, final, ChooserStats(
        requests=requests, retries=retries,
        slots_asked=slots_asked, second_pass_slots=second_pass_slots,
    )
