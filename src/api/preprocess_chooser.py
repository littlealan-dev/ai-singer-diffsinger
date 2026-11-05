"""Planner v2 chooser: ask the LLM for one listed option per open sheet slot.

Pure and offline: requests and replies are plain data, and the LLM call is
injected, so this module never talks to a provider itself. A freeform slot has
no menu: the LLM writes its sections, one list per lane.
"""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any, Awaitable, Callable

from src.api.preprocess_assembler import AssemblyError, assemble_plan, expand
from src.api.preprocess_evidence import EvidenceUnavailable, trim_musicxml
from src.api.preprocess_sheet import PINNABLE_KINDS, Lane, Sheet, Slot, build_sheet
from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _derived_part_indices,
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
    freeform_bars: dict[str, tuple[int, int]] = field(default_factory=dict)  # freeform key -> bars
    evidence_chars: int = 0
    evidence_unavailable: str | None = None


@dataclass(frozen=True)
class ChooserStats:
    requests: int
    retries: int
    slots_asked: int
    second_pass_slots: int


@dataclass(frozen=True)
class SlotFinding:
    rule: str
    severity: str
    message: str
    start_measure: int
    end_measure: int


RETRY_TEXT = "Your previous answer was invalid: {errors}. Answer every key with one listed id."

# Kinds whose option id alone does not say what the option does.
_KINDS_WITH_FIELDS = ("voice_assignment", "rank_assignment")
# Schema keywords Gemini's response schema accepts.
_GEMINI_SCHEMA_KEYS = (
    "type", "format", "description", "nullable", "enum", "items", "minItems", "maxItems",
    "minimum", "maximum", "properties", "required", "propertyOrdering",
)

Call = Callable[[ChooserRequest], Awaitable[str]]


def _gemini_schema(schema: dict) -> dict:
    """Keep only the keywords Gemini accepts; a ["x", "null"] type becomes nullable x."""
    result: dict[str, Any] = {}
    for key in _GEMINI_SCHEMA_KEYS:
        if key not in schema:
            continue
        value = schema[key]
        if key == "type" and isinstance(value, list):
            types = [item for item in value if item != "null"]
            value = types[0] if types else "string"
            if "null" in schema[key]:
                result["nullable"] = True
        elif key == "properties":
            value = {name: _gemini_schema(item) for name, item in value.items()}
        elif key == "items":
            value = _gemini_schema(value)
        result[key] = value
    return result


@functools.lru_cache(maxsize=1)
def _section_schema() -> dict:
    """The plan section the preprocess tool takes, in Gemini's schema form."""
    from src.mcp.tools import _PREPROCESS_PLAN_SECTION_SCHEMA

    return _gemini_schema(_PREPROCESS_PLAN_SECTION_SCHEMA)


def _lane_height_midi(score: dict, lane: Lane) -> int | None:
    part = score["parts"][lane.part_index]
    voice = next((str(vp["source_voice_id"])
                  for vp in _analyze_part_voice_parts(part, lane.part_index)["voice_parts"]
                  if vp["voice_part_id"] == lane.source_voice_part_id), None)
    pitches = [float(note["pitch_midi"]) for note in part.get("notes") or []
               if _is_countable_sung_note(note) and _staff_scope_key(note) == lane.scope_key
               and _voice_key(note.get("voice")) == voice and note.get("pitch_midi") is not None]
    return round(median(pitches)) if pitches else None


def build_request_context(
    score: dict, planning_context: dict | None, sheet: Sheet, *,
    evidence_max_chars: int = 400000,
) -> dict:
    """The request facts and lane heights that ``build_requests`` sends."""
    context = planning_context or {}
    target = context.get("target") if isinstance(context.get("target"), dict) else {}
    instruction = str(context.get("other_instruction") or "").strip()
    summary = score.get("score_summary") if isinstance(score.get("score_summary"), dict) else {}
    score_fields = {field: summary[key] for field, key in (
        ("title", "title"), ("composer", "composer"),
        ("selected_verse", "selected_verse_number"), ("available_verses", "available_verses"),
    ) if key in summary}
    future = context.get("future_synthesis_request")
    requested = context.get("request")
    language = (str(future.get("language") or "").strip() if isinstance(future, dict) else "")
    if not language:
        language = (str(requested.get("language") or "").strip()
                    if isinstance(requested, dict) else "")
    request = {
        "target_part": target.get("part_id"), "verse": sheet.verse_number,
        "user_instruction": instruction or None,
    }
    if language:
        request["language"] = language
    lyric_sample = None
    if not language and sheet.target_part_index is not None:
        summary_parts = summary.get("parts") or []
        part_summary = next((part for part in summary_parts
                             if part.get("part_index") == sheet.target_part_index), None)
        if part_summary is not None:
            verse = str(summary.get("selected_verse_number") or sheet.verse_number or "")
            matching = next((item for item in part_summary.get("lyric_verses") or []
                             if str(item.get("verse_number") or "") == verse), None)
            if matching is not None:
                lyric_sample = matching.get("sample")
    lane_details = {}
    for lane in sheet.lanes:
        part = score["parts"][lane.part_index]
        voice = next((str(vp["source_voice_id"])
                      for vp in _analyze_part_voice_parts(part, lane.part_index)["voice_parts"]
                      if vp["voice_part_id"] == lane.source_voice_part_id), None)
        lane_details[lane.lane_id] = {
            "part_name": part.get("part_name"),
            "xml_part": part.get("raw_part_id") or part.get("part_id"),
            "xml_voice": voice,
        }
    return {
        "request": request,
        "score": score_fields,
        "lyric_sample": lyric_sample,
        "tempos": score.get("tempos"),
        "lane_details": lane_details,
        "lane_height_midi": {lane.lane_id: _lane_height_midi(score, lane) for lane in sheet.lanes},
        "_score": score,
        "_evidence_max_chars": evidence_max_chars,
    }


def _evidence_bars(score: dict, slots: list[Slot]) -> set[int]:
    numbers = []
    for part in score.get("parts") or []:
        for note in part.get("notes") or []:
            try:
                numbers.append(int(note.get("measure_number")))
            except (TypeError, ValueError):
                continue
    if not numbers:
        return set()
    low, high = min(numbers), max(numbers)
    return {bar for slot in slots
            for bar in range(max(low, slot.start_measure - 1),
                             min(high, slot.end_measure + 1) + 1)}


def _request_evidence(
    sheet: Sheet, score: dict, slots: list[Slot], limit: int,
) -> tuple[dict[str, str], int, str | None]:
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    lane = lanes[slots[0].lane_ids[0]]
    part = score["parts"][lane.part_index]
    raw_part_id = str(part.get("raw_part_id") or part.get("part_id") or "")
    score_xml = trim_musicxml(score, [raw_part_id], _evidence_bars(score, slots))
    if len(score_xml) > limit:
        raise EvidenceUnavailable("too_large")
    evidence = {"score_xml": score_xml}
    reading = [slot for slot in slots if slot.kind == "lyric_reading"]
    if reading:
        derived = _derived_part_indices(score)
        other_ids = list(dict.fromkeys(
            str(other.get("raw_part_id") or other.get("part_id"))
            for index, other in enumerate(score.get("parts") or [])
            if index not in derived and index != lane.part_index
            and str(other.get("raw_part_id") or other.get("part_id")) != raw_part_id
        ))
        if other_ids:
            other_xml = trim_musicxml(score, other_ids, _evidence_bars(score, reading))
            if len(score_xml) + len(other_xml) > limit:
                return evidence, len(score_xml), "other_parts_too_large"
            evidence["score_xml_other_parts"] = other_xml
    return evidence, sum(len(xml) for xml in evidence.values()), None


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


def _request(
    sheet: Sheet, request_context: dict, slots: list[Slot], extras: dict[str, dict] | None = None,
) -> ChooserRequest:
    extras = extras or {}
    known = {lane.lane_id for lane in sheet.lanes}
    used: set[str] = set()
    for slot in slots:
        used.update(slot.lane_ids)
        _lane_ids_in(_sent_parts(slot), known, used)
    lanes = [lane for lane in sheet.lanes if lane.lane_id in used]
    keys = {lane.lane_id: f"L{index}" for index, lane in enumerate(lanes, start=1)}
    heights = request_context.get("lane_height_midi") or {}
    details = request_context.get("lane_details") or {}
    slot_keys = {f"s{index}": slot for index, slot in enumerate(slots, start=1)}
    payload_slots = []
    properties: dict[str, dict] = {}
    for key, slot in slot_keys.items():
        entry: dict[str, Any] = {
            "key": key, "kind": slot.kind, "lanes": [keys[lane_id] for lane_id in slot.lane_ids],
            "bars": f"{slot.start_measure}-{slot.end_measure}",
        }
        if slot.evidence:
            entry["evidence"] = _rekey(slot.evidence, keys)
        if slot.kind == "freeform":
            properties[key] = {
                "type": "array", "minItems": len(slot.lane_ids), "maxItems": len(slot.lane_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "lane": {"type": "string", "enum": [keys[lane_id] for lane_id in slot.lane_ids]},
                        "sections": {"type": "array", "minItems": 1, "items": _section_schema()},
                    },
                    "required": ["lane", "sections"],
                },
            }
        else:
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
            properties[key] = {"type": "string", "enum": [option.option_id for option in slot.options]}
        entry.update(_rekey(extras.get(slot.slot_id, {}), keys))
        payload_slots.append(entry)
    payload = {
        "request": request_context.get("request") or {},
        "lanes": [{
            "lane": keys[lane.lane_id], "part": lane.part_id, "staff": lane.scope_key,
            "voice": lane.source_voice_part_id, "height_midi": heights.get(lane.lane_id),
            **details.get(lane.lane_id, {}),
        } for lane in lanes],
        "slots": payload_slots,
    }
    if request_context.get("score"):
        payload["score"] = request_context["score"]
    if request_context.get("lyric_sample"):
        payload["lyric_sample"] = request_context["lyric_sample"]
    if request_context.get("tempos") is not None:
        payload["tempos"] = request_context["tempos"]
    evidence, evidence_chars, evidence_unavailable = (
        _request_evidence(sheet, request_context["_score"], slots,
                          request_context["_evidence_max_chars"])
        if "_score" in request_context else ({}, 0, None)
    )
    payload.update(evidence)
    return ChooserRequest(
        dynamic_text=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        response_schema={
            "type": "object",
            "properties": properties,
            "required": list(slot_keys),
            "propertyOrdering": list(slot_keys),
        },
        key_to_slot={key: slot.slot_id for key, slot in slot_keys.items()},
        lane_keys={key: lane_id for lane_id, key in keys.items()},
        freeform_bars={key: (slot.start_measure, slot.end_measure)
                       for key, slot in slot_keys.items() if slot.kind == "freeform"},
        evidence_chars=evidence_chars,
        evidence_unavailable=evidence_unavailable,
    )


def build_requests(
    sheet: Sheet, request_context: dict, *, max_slots_per_request: int = 80,
    extras: dict[str, dict] | None = None,
) -> list[ChooserRequest]:
    """One or more requests per staff, cut only at segment boundaries.

    ``extras`` adds fields to a slot's entry, by slot id (a repair's findings).
    """
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    by_scope: dict[tuple[int, str], dict[tuple[int, int], list[Slot]]] = {}
    for slot in sheet.slots:
        lane = lanes[slot.lane_ids[0]]
        by_scope.setdefault((lane.part_index, lane.scope_key), {}).setdefault(
            (slot.start_measure, slot.end_measure), []
        ).append(slot)
    requests: list[ChooserRequest] = []
    for segments in by_scope.values():
        batch: list[Slot] = []
        for segment_slots in segments.values():
            if batch and len(batch) + len(segment_slots) > max_slots_per_request:
                requests.append(_request(sheet, request_context, batch, extras))
                batch = []
            batch.extend(segment_slots)
        if batch:
            requests.append(_request(sheet, request_context, batch, extras))
    return requests


def _parse_freeform(
    request: ChooserRequest, key: str, value: Any,
) -> tuple[dict[str, list[dict]] | None, list[str]]:
    if not isinstance(value, list):
        return None, [f"{key}: not a list"]
    allowed = request.response_schema["properties"][key]["items"]["properties"]["lane"]["enum"]
    start, end = request.freeform_bars[key]
    by_lane: dict[str, list[dict]] = {}
    errors: list[str] = []
    for entry in value:
        lane = entry.get("lane") if isinstance(entry, dict) else None
        if lane not in allowed:
            errors.append(f"{key}: unknown lane {lane!r}")
            continue
        if lane in by_lane:
            errors.append(f"{key}: lane {lane} given twice")
            continue
        sections = entry.get("sections")
        try:
            covered = set(expand(sections if isinstance(sections, list) else [None]))
        except AssemblyError as exc:
            errors.append(f"{key}: lane {lane}: {exc}")
            continue
        if covered != set(range(start, end + 1)):
            errors.append(f"{key}: lane {lane}: sections must cover bars {start}-{end} exactly")
            continue
        by_lane[lane] = sections
    errors.extend(f"{key}: lane {lane} missing" for lane in allowed if lane not in by_lane)
    if errors:
        return None, errors
    return {request.lane_keys[lane]: sections for lane, sections in by_lane.items()}, []


def parse_reply(request: ChooserRequest, reply_text: str) -> tuple[dict[str, Any], list[str]]:
    """Map a reply to slot-id choices, with every problem as a short error.

    A menu slot's choice is an option id; a freeform slot's is {lane_id: sections}.
    """
    try:
        reply = json.loads(reply_text)
    except (TypeError, ValueError):
        return {}, ["reply: not valid JSON"]
    if not isinstance(reply, dict):
        return {}, ["reply: not a JSON object"]
    choices: dict[str, Any] = {}
    errors: list[str] = []
    properties = request.response_schema["properties"]
    for key, slot_id in request.key_to_slot.items():
        if key not in reply:
            errors.append(f"{key}: missing")
        elif key in request.freeform_bars:
            sections, problems = _parse_freeform(request, key, reply[key])
            errors.extend(problems)
            if sections is not None:
                choices[slot_id] = sections
        elif reply[key] not in properties[key]["enum"]:
            errors.append(f"{key}: unknown option {reply[key]!r}")
        else:
            choices[slot_id] = reply[key]
    errors.extend(f"{key}: unexpected key" for key in reply if key not in request.key_to_slot)
    return choices, errors


async def _ask(request: ChooserRequest, call: Call) -> tuple[dict[str, Any], int]:
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
    sheet: Sheet, request_context: dict, call: Call, max_slots_per_request: int,
    extras: dict[str, dict] | None = None,
) -> tuple[dict[str, Any], int, int]:
    requests = build_requests(sheet, request_context, max_slots_per_request=max_slots_per_request,
                              extras=extras)
    answers = await asyncio.gather(*(_ask(request, call) for request in requests))
    choices: dict[str, Any] = {}
    for answer, _ in answers:
        choices.update(answer)
    return choices, len(requests), sum(retried for _, retried in answers)


def _menus(sheet: Sheet) -> dict[str, Slot]:
    return {slot.slot_id: slot for slot in sheet.slots if slot.kind != "freeform"}


def _structure_pins(sheet: Sheet, choices: dict[str, Any]) -> dict[str, str]:
    """Structure choices that are not the slot's first option."""
    return {slot.slot_id: choices[slot.slot_id] for slot in _menus(sheet).values()
            if slot.kind in PINNABLE_KINDS and slot.slot_id in choices
            and choices[slot.slot_id] != slot.options[0].option_id}


def _still_valid(slot: Slot, choice: Any) -> bool:
    if slot.kind == "freeform":
        return isinstance(choice, dict) and set(choice) == set(slot.lane_ids)
    return choice in {option.option_id for option in slot.options}


async def _second_pass(
    score: dict, planning_context: dict | None, sheet: Sheet, choices: dict[str, Any], *,
    pinned: dict[str, str], cuts: dict | None, call: Call, max_slots_per_request: int,
    evidence_max_chars: int,
) -> tuple[Sheet, dict[str, Any], int, int, int]:
    """Rebuild with the non-default structure choices pinned and re-ask the slots that changed."""
    pins = {**pinned, **_structure_pins(sheet, choices)}
    if pins == pinned:
        return sheet, choices, 0, 0, 0
    previous = {slot.slot_id: slot for slot in sheet.slots}
    rebuilt = build_sheet(score, planning_context, pinned=pins, cuts=cuts)
    changed = [slot for slot in rebuilt.slots if (
        slot.slot_id not in previous
        or [option.option_id for option in slot.options]
        != [option.option_id for option in previous[slot.slot_id].options]
        or not _still_valid(slot, choices.get(slot.slot_id))
    )]
    if not changed:
        return rebuilt, choices, 0, 0, 0
    context = build_request_context(score, planning_context, rebuilt,
                                    evidence_max_chars=evidence_max_chars)
    again, requests, retries = await _ask_all(
        replace(rebuilt, slots=changed), context, call, max_slots_per_request
    )
    return rebuilt, {**choices, **again}, requests, retries, len(changed)


def _final(sheet: Sheet, choices: dict[str, Any]) -> dict[str, Any]:
    return {slot.slot_id: choices[slot.slot_id] for slot in sheet.slots}


async def choose(
    score: dict, planning_context: dict | None, *,
    call: Call, max_slots_per_request: int = 80, evidence_max_chars: int = 400000,
) -> tuple[Sheet, dict[str, Any], ChooserStats]:
    """Choose every open slot, re-asking the slots a non-default structure choice changes."""
    sheet = build_sheet(score, planning_context)
    context = build_request_context(score, planning_context, sheet,
                                    evidence_max_chars=evidence_max_chars)
    choices, requests, retries = await _ask_all(sheet, context, call, max_slots_per_request)
    slots_asked = len(sheet.slots)
    sheet, choices, more_requests, more_retries, second_pass_slots = await _second_pass(
        score, planning_context, sheet, choices, pinned={}, cuts=None,
        call=call, max_slots_per_request=max_slots_per_request,
        evidence_max_chars=evidence_max_chars,
    )
    return sheet, _final(sheet, choices), ChooserStats(
        requests=requests + more_requests, retries=retries + more_retries,
        slots_asked=slots_asked, second_pass_slots=second_pass_slots,
    )


def assemble_chosen(sheet: Sheet, choices: dict[str, Any], *, score: dict) -> dict:
    """Assemble the plan from menu choices and freeform sections."""
    freeform = {slot.slot_id for slot in sheet.slots if slot.kind == "freeform"}
    return assemble_plan(
        sheet, {slot_id: value for slot_id, value in choices.items() if slot_id not in freeform},
        freeform_sections={slot_id: choices[slot_id] for slot_id in freeform if slot_id in choices},
        score=score,
    )


def _slot_key(slot_id: str) -> tuple[str, int, int, str]:
    prefix, span, kind = slot_id.rsplit("|", 2)
    start, end = span.split("-", 1)
    return prefix, int(start), int(end), kind


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= span_end and span_start <= end for span_start, span_end in spans)


def _clip_sections(sections: list[dict], start: int, end: int) -> list[dict]:
    """The sections' parts inside bars start..end."""
    return [{**section, "start_measure": max(section["start_measure"], start),
             "end_measure": min(section["end_measure"], end)}
            for section in sections
            if section["start_measure"] <= end and start <= section["end_measure"]]


def _selection_lookup(selected: dict[str, Any]) -> Callable[[str], Any]:
    """The earlier choice for a slot id: same lane prefix and kind, covering its bars.

    Freeform sections are clipped to the slot's bars, since a cut can shrink the slot.
    """
    by_key = [(_slot_key(slot_id), value) for slot_id, value in selected.items()]

    def lookup(slot_id: str) -> Any:
        prefix, start, end, kind = _slot_key(slot_id)
        for (old_prefix, old_start, old_end, old_kind), value in by_key:
            if (old_prefix, old_kind) == (prefix, kind) and old_start <= start and end <= old_end:
                if kind == "freeform" and isinstance(value, dict):
                    return {lane_id: _clip_sections(sections, start, end)
                            for lane_id, sections in value.items()}
                return value
        return None

    return lookup


def _repair_slot(slot: Slot, previous: str | None, findings: list[SlotFinding]) -> Slot:
    """Drop an option that failed with a P0; flag one that failed otherwise."""
    if previous is None or not findings or slot.kind == "freeform":
        return slot
    if any(finding.severity == "P0" for finding in findings):
        kept = tuple(option for option in slot.options if option.option_id != previous)
        return replace(slot, options=kept or slot.options)
    flag = "failed_last_attempt: " + ", ".join(sorted({finding.rule for finding in findings}))
    return replace(slot, options=tuple(
        replace(option, flags=(*option.flags, flag)) if option.option_id == previous else option
        for option in slot.options
    ))


async def choose_repair(
    score: dict, planning_context: dict | None, *,
    sheet: Sheet, choices: dict[str, Any],
    failing: dict[str, list[tuple[int, int]]],
    findings_by_lane: dict[str, list[SlotFinding]],
    pinned: dict[str, str] | None,
    call: Call, max_slots_per_request: int = 80, evidence_max_chars: int = 400000,
) -> tuple[Sheet, dict[str, Any], ChooserStats]:
    """Re-ask only the slots over the failing lanes and bars; carry every other choice over.

    ``failing`` maps a lane id to its failing (start, end) bar spans. Each span
    becomes its own segment, so the repair can change just those bars. The sheet's
    segments stay cut as they are, so an earlier repair's bars keep their own choices.
    """
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    cuts: dict[tuple[int, str], set[int]] = {}
    for segment in sheet.segments:
        cuts.setdefault((segment.part_index, segment.scope_key), set()).add(segment.start_measure)
    for lane_id, spans in failing.items():
        lane = lanes[lane_id]
        for start, end in spans:
            cuts.setdefault((lane.part_index, lane.scope_key), set()).update({start, end + 1})
    selected = {**{slot.slot_id: slot.options[0].option_id for slot in sheet.slots
                    if slot.kind == "lane_layout"},
                **{slot_id: option.option_id for slot_id, option in sheet.fixed.items()},
                **(pinned or {}), **choices}
    earlier = _selection_lookup(selected)
    layout_pins = {slot_id: value for slot_id, value in selected.items()
                   if slot_id.endswith("|lane_layout") and value == "separate"}

    def failing_here(slot: Slot) -> bool:
        if slot.kind == "lane_layout":
            return False
        return any(_overlaps(slot.start_measure, slot.end_measure, failing.get(lane_id, []))
                   for lane_id in slot.lane_ids)

    draft = build_sheet(score, planning_context, pinned=layout_pins, cuts=cuts)
    pins = {**layout_pins, **{slot.slot_id: earlier(slot.slot_id)
            for slot in _menus(draft).values()
            if slot.kind in PINNABLE_KINDS and not failing_here(slot)
            and _still_valid(slot, earlier(slot.slot_id))
            and earlier(slot.slot_id) != slot.options[0].option_id}}
    new_sheet = build_sheet(score, planning_context, pinned=pins, cuts=cuts) if pins != layout_pins else draft
    carried: dict[str, Any] = {}
    ask: list[Slot] = []
    extras: dict[str, dict] = {}
    current = {**{slot_id: option.option_id for slot_id, option in new_sheet.fixed.items()}}
    for slot in new_sheet.slots:
        value = earlier(slot.slot_id)
        if not failing_here(slot) and _still_valid(slot, value):
            carried[slot.slot_id] = value
            current[slot.slot_id] = value
    for slot in new_sheet.slots:
        if slot.slot_id in carried:
            continue
        previous = earlier(slot.slot_id) if _still_valid(slot, earlier(slot.slot_id)) else None
        findings = [finding for lane_id in slot.lane_ids for finding in findings_by_lane.get(lane_id, [])
                    if _overlaps(slot.start_measure, slot.end_measure,
                                 [(finding.start_measure, finding.end_measure)])]
        ask.append(_repair_slot(slot, previous if slot.kind != "freeform" else None, findings))
        prefix, start, end, _ = _slot_key(slot.slot_id)
        neighbours: dict[str, dict[str, str]] = {"before": {}, "after": {}}
        for slot_id, value in current.items():
            other_prefix, other_start, other_end, kind = _slot_key(slot_id)
            if other_prefix != prefix or not isinstance(value, str):
                continue
            if other_end == start - 1:
                neighbours["before"][kind] = value
            elif other_start == end + 1:
                neighbours["after"][kind] = value
        extra: dict[str, Any] = {"neighbours": neighbours}
        if findings:
            extra["findings"] = [{
                "rule": finding.rule, "severity": finding.severity, "message": finding.message,
                "bars": f"{finding.start_measure}-{finding.end_measure}",
            } for finding in findings]
        if previous is not None and slot.kind != "freeform":
            extra["previous"] = previous
        extras[slot.slot_id] = extra
    if not ask:
        return new_sheet, _final(new_sheet, carried), ChooserStats(0, 0, 0, 0)
    context = build_request_context(score, planning_context, new_sheet,
                                    evidence_max_chars=evidence_max_chars)
    answers, requests, retries = await _ask_all(
        replace(new_sheet, slots=ask), context, call, max_slots_per_request, extras
    )
    final_sheet, final_choices, more_requests, more_retries, second_pass_slots = await _second_pass(
        score, planning_context, new_sheet, {**carried, **answers}, pinned=pins, cuts=cuts,
        call=call, max_slots_per_request=max_slots_per_request,
        evidence_max_chars=evidence_max_chars,
    )
    return final_sheet, _final(final_sheet, final_choices), ChooserStats(
        requests=requests + more_requests, retries=retries + more_retries,
        slots_asked=len(ask), second_pass_slots=second_pass_slots,
    )
