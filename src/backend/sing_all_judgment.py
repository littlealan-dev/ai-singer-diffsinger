"""Sing All's constrained quote and style replies."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence
import json

from src.backend.language_selection import normalize_language_code
from src.mcp.tools import list_tools
from src.musicxml.instrument_programs import (
    FLUIDR3_MELODIC_BANK,
    FLUIDR3_PERCUSSION_BANK,
    FLUIDR3_PERCUSSION_PROGRAMS,
    FLUIDR3_SOUNDFONT_ID,
    _normalise_preset,
    _valid_preset_for_instrument,
)


@dataclass(frozen=True)
class QuoteQuestions:
    request: dict[str, Any]
    parts: dict[str, dict[str, Any]]
    instruments: dict[str, dict[str, Any]]
    staffs: dict[str, dict[str, Any]]
    rows: dict[str, dict[str, Any]]
    automatic_voices: dict[str, str]

    @property
    def has_questions(self) -> bool:
        return any(self.request[key] for key in ("parts", "instruments", "staffs", "rows"))


def input_hash(request: Mapping[str, Any], prompt: str, model: str) -> str:
    payload = json.dumps([request, prompt, model], sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def compatible_voicebanks(voicebanks: Sequence[Mapping[str, Any]], language: str) -> list[str]:
    """Use the same declared-language rule as a take, with legacy English default."""
    selected = normalize_language_code(language)
    return [str(bank["id"]) for bank in voicebanks if bank.get("id") and selected in {
        normalize_language_code(value) for value in (bank.get("languages") or ["en"])
    }]


def _row_question(row: Mapping[str, Any], key: str, *,
                  staff_key: str | None = None, layout: str | None = None) -> dict[str, Any]:
    range_data = row.get("pitchRange") or {}
    return {
        "key": key,
        "staff": staff_key,
        "layout": layout,
        "part_name": row.get("sourcePartId"),
        "row_name": row.get("name"),
        "lowest_note": range_data.get("lowest_note"),
        "highest_note": range_data.get("highest_note"),
        "tessitura_low_note": range_data.get("tessitura_low_note"),
        "tessitura_high_note": range_data.get("tessitura_high_note"),
    }


def build_request(facts: Mapping[str, Any], language: str,
                  voicebanks: Sequence[Mapping[str, Any]], *,
                  previous: Mapping[str, Any] | None = None) -> QuoteQuestions:
    """Give every unresolved decision a short key and preserve its source map."""
    prior = previous or {}
    prior_roles = prior.get("roles") or {}
    prior_sounds = prior.get("sounds") or {}
    prior_layouts = prior.get("layouts") or {}
    parts: dict[str, dict[str, Any]] = {}
    instruments: dict[str, dict[str, Any]] = {}
    staffs: dict[str, dict[str, Any]] = {}
    rows: dict[str, dict[str, Any]] = {}
    request: dict[str, Any] = {
        "parts": [], "instruments": [], "staffs": [], "rows": [],
        "language": language,
        "voicebanks": [
            {key: bank.get(key) for key in (
                "id", "name", "languages", "language_details", "supported_voice_types",
                "supported_gender_presentations", "supported_range", "optimal_range",
                "selection_priority",
            )} for bank in voicebanks
        ],
    }
    all_parts = [item["part"] for item in facts.get("undecided_parts") or []]
    for item in facts.get("undecided_parts") or []:
        part_id = str(item["partId"])
        if part_id in prior_roles:
            continue
        key = f"p{len(parts) + 1}"
        parts[key] = item
        request["parts"].append({
            "key": key, "part": item["part"],
            "other_parts": [
                {"part_name": other.get("part_name"), "kind": other.get("instrumental_role")}
                for other in all_parts if other is not item["part"]
            ],
        })
    for item in facts.get("instruments") or []:
        route = item["route"]
        instrument_id = str(route.get("score_instrument_id") or "")
        if instrument_id in prior_sounds:
            continue
        key = f"i{len(instruments) + 1}"
        instruments[key] = item
        request["instruments"].append({
            "key": key, "part_name": item["partId"],
            "route": "percussion" if route.get("is_percussion") else "melodic",
            "reason": route.get("program_source"),
            "instrument": route,
        })
    for item in facts.get("open_staffs") or []:
        if item["key"] in prior_layouts:
            continue
        key = f"s{len(staffs) + 1}"
        staffs[key] = item
        request["staffs"].append({"key": key, **item["evidence"]})

    compatible = compatible_voicebanks(voicebanks, language)
    automatic_voices: dict[str, str] = {}
    candidates: list[tuple[dict[str, Any], str | None, str | None]] = [
        (row, None, None) for row in facts.get("rows") or []
    ]
    for staff in facts.get("open_staffs") or []:
        for layout, staff_rows in staff["rows_if"].items():
            candidates.extend((row, staff["key"], layout) for row in staff_rows)
    for row, staff_key, layout in candidates:
        key = f"r{len(rows) + len(automatic_voices) + 1}"
        identity = f"{staff_key or ''}|{layout or ''}|{row['rowId']}"
        if len(compatible) == 1:
            automatic_voices[identity] = compatible[0]
            continue
        rows[key] = {"row": row, "staff": staff_key, "layout": layout,
                     "identity": identity}
        request["rows"].append(_row_question(row, key, staff_key=staff_key, layout=layout))
    return QuoteQuestions(request, parts, instruments, staffs, rows, automatic_voices)


def _sound_schema(route: Mapping[str, Any]) -> dict[str, Any]:
    percussion = bool(route.get("is_percussion"))
    return {
        "type": "OBJECT", "nullable": True,
        "required": ["bank", "kind", "program", "evidence"],
        "properties": {
            "bank": {"type": "INTEGER", "enum": [
                FLUIDR3_PERCUSSION_BANK if percussion else FLUIDR3_MELODIC_BANK]},
            "kind": {"type": "STRING", "enum": [
                "percussion_kit" if percussion else "melodic"]},
            "program": ({"type": "INTEGER", "enum": sorted(FLUIDR3_PERCUSSION_PROGRAMS)}
                        if percussion else {"type": "INTEGER", "minimum": 0, "maximum": 127}),
            "evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    }


def quote_reply_schema(questions: QuoteQuestions,
                       compatible_ids: Sequence[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if questions.parts:
        properties["roles"] = {"type": "OBJECT", "required": list(questions.parts),
                               "properties": {key: {"type": "STRING", "enum": [
                                   "voice", "instrument", "unsure"]}
                                              for key in questions.parts}}
    if questions.parts or questions.instruments:
        sound_properties = {
            key: _sound_schema((item["part"].get("instruments") or [{}])[0])
            for key, item in questions.parts.items()
        }
        sound_properties.update({key: _sound_schema(item["route"])
                                 for key, item in questions.instruments.items()})
        properties["sounds"] = {"type": "OBJECT", "required": list(sound_properties),
                                "properties": sound_properties}
    if questions.staffs:
        properties["layouts"] = {"type": "OBJECT", "required": list(questions.staffs),
                                 "properties": {key: {"type": "STRING", "enum": [
                                     "shared", "separate"]} for key in questions.staffs}}
    if questions.rows:
        properties["voices"] = {"type": "OBJECT", "required": list(questions.rows),
                                "properties": {key: {"type": "STRING", "enum": list(compatible_ids)}
                                               for key in questions.rows}}
    return {"type": "OBJECT", "required": list(properties), "properties": properties}


def check_reply(questions: QuoteQuestions, reply: Any,
                compatible_ids: Sequence[str]) -> tuple[list[str], set[str]]:
    """Validate every exact key and the sound route; classify sound-only errors."""
    errors: list[str] = []
    sound_errors: set[str] = set()
    if not isinstance(reply, Mapping):
        return ["reply must be an object"], sound_errors
    schema = quote_reply_schema(questions, compatible_ids)
    if set(reply) - {"sounds"} != set(schema["required"]) - {"sounds"}:
        errors.append("reply sections do not match the request")
    for section in schema["required"]:
        value = reply.get(section)
        expected = schema["properties"][section]["required"]
        if section == "sounds":
            if not isinstance(value, Mapping) or set(value) - set(expected):
                errors.append("sounds keys do not match the request")
        elif not isinstance(value, Mapping) or set(value) != set(expected):
            errors.append(f"{section} keys do not match the request")
    roles = reply.get("roles") if isinstance(reply.get("roles"), Mapping) else {}
    for key in questions.parts:
        if roles.get(key) not in {"voice", "instrument", "unsure"}:
            errors.append(f"{key}: invalid role")
    layouts = reply.get("layouts") if isinstance(reply.get("layouts"), Mapping) else {}
    for key in questions.staffs:
        if layouts.get(key) not in {"shared", "separate"}:
            errors.append(f"{key}: invalid layout")
    voices = reply.get("voices") if isinstance(reply.get("voices"), Mapping) else {}
    for key in questions.rows:
        if voices.get(key) not in compatible_ids:
            errors.append(f"{key}: invalid voice")
    sounds = reply.get("sounds") if isinstance(reply.get("sounds"), Mapping) else {}
    for key in list(questions.parts) + list(questions.instruments):
        value = sounds.get(key)
        if key not in sounds:
            sound_errors.add(key)
            continue
        if key in questions.parts and roles.get(key) != "instrument":
            if value is not None:
                sound_errors.add(key)
            continue
        if value is None:
            continue
        route = (questions.parts[key]["part"].get("instruments") or [{}])[0] \
            if key in questions.parts else questions.instruments[key]["route"]
        preset = _normalise_preset({**value, "soundfont_id": FLUIDR3_SOUNDFONT_ID}) \
            if isinstance(value, Mapping) else None
        if (not preset or not _valid_preset_for_instrument(preset, route)
                or not isinstance(value.get("evidence"), list)
                or not all(isinstance(item, str) for item in value["evidence"])
                or set(value) != {"bank", "kind", "program", "evidence"}):
            sound_errors.add(key)
    errors.extend(f"{key}: invalid sound" for key in sorted(sound_errors))
    return errors, sound_errors


def style_reply_schema(rows: Sequence[Mapping[str, Any]],
                       voicebanks_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Read control ranges and descriptions from synthesize's own tool schema."""
    tool = list_tools({"synthesize"})[0]
    tool_properties = tool["inputSchema"]["properties"]
    row_properties: dict[str, Any] = {}
    for row in rows:
        voicebank = voicebanks_by_id[str(row["voice"])]
        colors = [str(item["name"]) for item in voicebank.get("voice_colors") or []
                  if isinstance(item, Mapping) and item.get("name")]
        controls = {
            name: {"type": "NUMBER", "minimum": tool_properties[name]["minimum"],
                   "maximum": tool_properties[name]["maximum"],
                   "description": tool_properties[name]["description"]}
            for name in ("airiness", "clarity", "gender", "intensity")
        }
        controls["voiceColor"] = {
            "type": "STRING", "nullable": True, "enum": colors,
            "description": tool_properties["voice_color"]["description"],
        }
        row_properties[str(row["rowId"])] = {
            "type": "OBJECT", "required": ["voiceColor", "airiness", "clarity", "gender", "intensity"],
            "properties": controls,
        }
    return {"type": "OBJECT", "required": ["rows"], "properties": {
        "rows": {"type": "OBJECT", "required": list(row_properties),
                 "properties": row_properties}
    }}


def check_style_reply(reply: Any, schema: Mapping[str, Any]) -> list[str]:
    if not isinstance(reply, Mapping) or set(reply) != {"rows"}:
        return ["style reply needs rows"]
    actual = reply.get("rows")
    expected = schema["properties"]["rows"]["properties"]
    if not isinstance(actual, Mapping) or set(actual) != set(expected):
        return ["style row keys do not match"]
    errors: list[str] = []
    for row_id, row_schema in expected.items():
        row = actual[row_id]
        if not isinstance(row, Mapping) or set(row) != set(row_schema["required"]):
            errors.append(f"{row_id}: controls do not match")
            continue
        for name, control in row_schema["properties"].items():
            value = row[name]
            if name == "voiceColor":
                if value is not None and value not in control["enum"]:
                    errors.append(f"{row_id}: invalid voiceColor")
            elif (isinstance(value, bool) or not isinstance(value, (int, float))
                  or not control["minimum"] <= value <= control["maximum"]):
                errors.append(f"{row_id}: invalid {name}")
    return errors
