"""Planner v2 chooser core (SIG-163): requests, replies, retry, parallelism, second pass."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from xml.etree import ElementTree as ET

import pytest

from src.api import parse_score
from src.api import preprocess_assembler as assembler
from src.api import preprocess_chooser as chooser
from src.api.preprocess_evidence import EvidenceUnavailable
from src.api.preprocess_sheet import Lane, MeasureSignature, Option, Sheet, Slot, build_sheet
from tests.test_preprocess_evidence import _gap_score, _xml
from tests.test_preprocess_sheet import (
    SATB, SHARED_LINE, _chord_score, _four_note_chord_with_native_lanes, _no_word_two_part_score,
    _note, _parse_inline, _rest, _silence_voice_in_bar, _single_staff_score, _too_many_chord_claims,
    _two_chord_voices_with_silent_lanes, _unnumbered_chord_with_opposite_voice_heights, lyric_line_xml,
)


def _context(score, part_index=0):
    return {"target": {"part_id": score["parts"][part_index]["part_id"]}}


def _two_staff_part():
    """SATB as one part on two staves; the alto (bar 1) and bass (bar 2) are silent."""
    score = parse_score(SATB, verse_number=1)
    combined = deepcopy(score["parts"][0])
    combined["part_id"] = "P1-Combined"
    combined["notes"] = [{**note, "staff": str(staff)}
                         for staff, part in enumerate(score["parts"], start=1) for note in part["notes"]]
    score["parts"] = [combined]
    _silence_voice_in_bar(score, 0, "2", 1)
    return _silence_voice_in_bar(score, 0, "6", 2)


def _chord_beside_a_line(tmp_path):
    """Bar 1: a high single line, a chord below it, two silent voices; bar 2: all four sing."""
    first = (_note("G", voice="1", lyric="high")
             + '<backup><duration>4</duration></backup>'
             + _note("C", voice="2", lyric="low") + _note("E", voice="2", chord=True, lyric=None)
             + '<backup><duration>4</duration></backup>' + _rest(voice="3", duration=4)
             + '<backup><duration>4</duration></backup>' + _rest(voice="4", duration=4))
    second = (_note("G", voice="1", lyric="a") + '<backup><duration>4</duration></backup>'
              + _note("E", voice="2", lyric="b") + '<backup><duration>4</duration></backup>'
              + _note("D", voice="3", lyric="c") + '<backup><duration>4</duration></backup>'
              + _note("C", voice="4", lyric="d"))
    return _single_staff_score(tmp_path, [first, second])


def _fixtures(tmp_path):
    """(score, planning context) for every sheet fixture without a freeform slot."""
    builders = {
        "chord": _chord_score, "unnumbered": _unnumbered_chord_with_opposite_voice_heights,
        "two_chord_silent": _two_chord_voices_with_silent_lanes,
        "four_note": _four_note_chord_with_native_lanes, "chord_beside_line": _chord_beside_a_line,
        "no_word_two_part": _no_word_two_part_score,
        "shared_line": lambda path: _parse_inline(path, lyric_line_xml(SHARED_LINE)),
    }
    cases = []
    for name, build in builders.items():
        directory = tmp_path / name
        directory.mkdir()
        score = build(directory)
        cases.append((name, score, _context(score)))
    satb = parse_score(SATB, verse_number=1)
    cases.append(("satb", satb, {"target": {"part_id": "P1-Staff1"}}))
    cases.append(("two_staff_part", _two_staff_part(), {"target": {"part_id": "P1-Combined"}}))
    return cases


def _first_answer(request):
    return json.dumps({key: prop["enum"][0] for key, prop in request.response_schema["properties"].items()})


async def _first_option_call(request):
    return _first_answer(request)


def _scope(sheet, request):
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    return {(lanes[lane_id].part_index, lanes[lane_id].scope_key) for lane_id in request.lane_keys.values()}


# --- Sheet trims -------------------------------------------------------------

def test_native_lane_sings_its_own_voice_without_a_melody_slot():
    score = parse_score(SATB, verse_number=1)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    lanes = {lane.lane_id: lane for lane in sheet.lanes}
    native = [(lane_id, segment) for segment in sheet.segments
              for lane_id, has_notes in segment.signature.lanes if has_notes]
    assert native
    assert not any(slot.kind == "melody_source" for slot in sheet.slots)
    for lane_id, segment in native:
        fixed = sheet.fixed[f"{lane_id}|{segment.start_measure}-{segment.end_measure}|melody_source"]
        assert fixed.fields["melody_source"] == {
            "part_index": lanes[lane_id].part_index, "voice_part_id": lanes[lane_id].source_voice_part_id,
        }


def test_shared_notehead_goes_to_every_voice_without_a_slot():
    score = parse_score(SATB, verse_number=1)
    notes = score["parts"][0]["notes"]
    soprano = next(note for note in notes if note.get("voice") == "1")
    alto_index = next(index for index, note in enumerate(notes) if note.get("voice") == "2")
    notes[alto_index] = {**soprano, "voice": "2"}
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    assert not any(slot.kind == "split_shared_note_policy" for slot in sheet.slots)
    assert any(slot_id.endswith("|split_shared_note_policy") for slot_id in sheet.fixed)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert {target.get("split_shared_note_policy") for target in plan["targets"]} == {"duplicate_to_all"}


def test_pinned_none_or_empty_is_unchanged_and_unknown_pins_raise(tmp_path):
    for _, score, context in _fixtures(tmp_path):
        sheet = build_sheet(score, context)
        assert build_sheet(score, context, pinned=None) == sheet
        assert build_sheet(score, context, pinned={}) == sheet
    score = parse_score(SATB, verse_number=1)
    with pytest.raises(ValueError):
        build_sheet(score, {"target": {"part_id": "P1-Staff1"}}, pinned={"no|1-1|mode": "rest"})
    two_staff = _two_staff_part()
    sheet = build_sheet(two_staff, {"target": {"part_id": "P1-Combined"}})
    melody = next(slot for slot in sheet.slots if slot.kind == "melody_source")
    with pytest.raises(ValueError):
        build_sheet(two_staff, {"target": {"part_id": "P1-Combined"}}, pinned={melody.slot_id: "nope"})
    lyric = next(slot for slot in sheet.slots if slot.kind == "lyric_strategy")
    with pytest.raises(ValueError):
        build_sheet(two_staff, {"target": {"part_id": "P1-Combined"}},
                    pinned={lyric.slot_id: lyric.options[0].option_id})


def test_pinned_melody_fixes_the_slot_and_rebuilds_its_lyric_slots():
    score = _two_staff_part()
    context = {"target": {"part_id": "P1-Combined"}}
    sheet = build_sheet(score, context)
    melody = next(slot for slot in sheet.slots if slot.kind == "melody_source")
    other = melody.options[1]
    pinned = build_sheet(score, context, pinned={melody.slot_id: other.option_id})
    assert melody.slot_id not in {slot.slot_id for slot in pinned.slots}
    assert pinned.fixed[melody.slot_id] == other
    assert {slot.slot_id for slot in pinned.slots} - {slot.slot_id for slot in sheet.slots} == set()


# --- Requests ----------------------------------------------------------------

def test_two_staff_requests_stay_on_one_staff_with_dense_keys_and_strict_schema():
    score = _two_staff_part()
    context = {"target": {"part_id": "P1-Combined", "verse_number": 1}, "other_instruction": "  "}
    sheet = build_sheet(score, context)
    requests = chooser.build_requests(sheet, chooser.build_request_context(score, context, sheet))
    scopes = [_scope(sheet, request) for request in requests]
    assert all(len(scope) == 1 for scope in scopes)
    assert {next(iter(scope)) for scope in scopes} == {(0, "1"), (0, "2")}
    slots = {slot.slot_id: slot for slot in sheet.slots}
    asked = [slot_id for request in requests for slot_id in request.key_to_slot.values()]
    assert sorted(asked) == sorted(slots)
    for request in requests:
        keys = list(request.key_to_slot)
        assert keys == [f"s{index}" for index in range(1, len(keys) + 1)]
        assert list(request.lane_keys) == [f"L{index}" for index in range(1, len(request.lane_keys) + 1)]
        lane_order = [lane.lane_id for lane in sheet.lanes]
        assert list(request.lane_keys.values()) == sorted(request.lane_keys.values(), key=lane_order.index)
        assert [request.key_to_slot[key] for key in keys] == [
            slot.slot_id for slot in sheet.slots if slot.slot_id in request.key_to_slot.values()
        ]
        assert all(lane.lane_id not in request.dynamic_text for lane in sheet.lanes)
        schema = request.response_schema
        assert schema["required"] == keys == schema["propertyOrdering"]
        for key, slot_id in request.key_to_slot.items():
            assert schema["properties"][key] == {
                "type": "string", "enum": [option.option_id for option in slots[slot_id].options],
            }
        payload = json.loads(request.dynamic_text)
        assert payload["request"] == {"target_part": "P1-Combined", "verse": "1", "user_instruction": None}
        assert all(isinstance(lane["height_midi"], int) for lane in payload["lanes"])


def test_voice_assignment_fields_are_sent_with_lane_keys(tmp_path):
    score = _chord_beside_a_line(tmp_path)
    sheet = build_sheet(score, _context(score))
    request, = chooser.build_requests(sheet, chooser.build_request_context(score, _context(score), sheet))
    assert all(lane.lane_id not in request.dynamic_text for lane in sheet.lanes)
    payload = json.loads(request.dynamic_text)
    assignment = next(slot for slot in payload["slots"] if slot["kind"] == "voice_assignment")
    assert set(assignment["options"][0]["fields"]["melody_source_by_lane"]) == set(request.lane_keys)
    mode = next(slot for slot in payload["slots"] if slot["kind"] == "lyric_line")
    assert all("fields" not in option for option in mode["options"])


def _evidence_request(tmp_path, *, context_extra=None, limit=400000, reading=True):
    source = tmp_path / "evidence.xml"
    source.write_text(_xml(), encoding="utf-8")
    score = parse_score(source, verse_number=1)
    score["score_summary"]["title"] = "Shared score"
    score["score_summary"]["composer"] = "Composer"
    context = {"target": {"part_id": score["parts"][0]["part_id"]}, **(context_extra or {})}
    sheet = build_sheet(score, context)
    lane = sheet.lanes[0]
    sheet.slots = [_slot(lane.lane_id, 1)]
    if reading:
        sheet.slots.append(_slot(lane.lane_id, 3, "lyric_reading"))
    request, = chooser.build_requests(
        sheet, chooser.build_request_context(score, context, sheet, evidence_max_chars=limit),
    )
    return score, request


def _xml_bars(xml):
    root = ET.fromstring(xml)
    parts = [item for item in root if item.tag == "part"]
    return {part.get("id"): [int(measure.get("number")) for measure in part if measure.tag == "measure"]
            for part in parts}


def test_request_shares_trimmed_xml_and_existing_score_metadata(tmp_path):
    score, request = _evidence_request(tmp_path)
    payload = json.loads(request.dynamic_text)
    assert _xml_bars(payload["score_xml"]) == {"P1": [1, 2, 3]}
    assert _xml_bars(payload["score_xml_other_parts"]) == {"P2": [2, 3]}
    assert request.evidence_chars == len(payload["score_xml"]) + len(payload["score_xml_other_parts"])
    assert request.evidence_unavailable is None
    assert payload["score"] == {
        "title": "Shared score", "composer": "Composer", "selected_verse": "1", "available_verses": ["1"],
    }
    assert payload["tempos"] == score["tempos"]
    assert payload["lyric_sample"] == ["one", "two", "three"]
    assert payload["lanes"][0]["part_name"] == "Choir"
    assert payload["lanes"][0]["xml_part"] == "P1"
    assert payload["lanes"][0]["xml_voice"] == "1"
    assert len(payload["slots"]) == 2
    assert all("score_xml" not in slot for slot in payload["slots"])


def test_request_language_uses_planning_context_and_suppresses_sample(tmp_path):
    _, request = _evidence_request(tmp_path, context_extra={
        "request": {"language": "French"},
        "future_synthesis_request": {"language": "English"},
    })
    payload = json.loads(request.dynamic_text)
    assert payload["request"]["language"] == "English"
    assert "lyric_sample" not in payload


def test_other_parts_are_omitted_without_lyric_reading(tmp_path):
    _, request = _evidence_request(tmp_path, reading=False)
    payload = json.loads(request.dynamic_text)
    assert _xml_bars(payload["score_xml"]) == {"P1": [1, 2]}
    assert "score_xml_other_parts" not in payload


def test_request_rejects_over_limit_or_unavailable_xml(tmp_path):
    with pytest.raises(EvidenceUnavailable, match="too_large"):
        _evidence_request(tmp_path, limit=1)
    score, _ = _evidence_request(tmp_path)
    score["source_musicxml_path"] = str(tmp_path / "missing.xml")
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    sheet.slots = [_slot(sheet.lanes[0].lane_id, 1)]
    with pytest.raises(EvidenceUnavailable, match="unreadable_path"):
        chooser.build_requests(sheet, chooser.build_request_context(score, context, sheet))

    source = tmp_path / "evidence.xml"
    source.write_text(_xml().replace('<measure number="2">', '<measure number="1">', 1),
                      encoding="utf-8")
    score["source_musicxml_path"] = str(source)
    with pytest.raises(EvidenceUnavailable, match="duplicate_measure_number"):
        chooser.build_requests(sheet, chooser.build_request_context(score, context, sheet))


def test_request_drops_only_other_parts_when_combined_xml_exceeds_limit(tmp_path):
    _, full = _evidence_request(tmp_path)
    full_payload = json.loads(full.dynamic_text)
    primary_size = len(full_payload["score_xml"])
    _, limited = _evidence_request(tmp_path, limit=primary_size)
    payload = json.loads(limited.dynamic_text)
    assert payload["score_xml"] == full_payload["score_xml"]
    assert "score_xml_other_parts" not in payload
    assert limited.evidence_chars == primary_size
    assert limited.evidence_unavailable == "other_parts_too_large"


def _lane(lane_id):
    return Lane(lane_id, "P", 0, "1", "v1", 0, "append_new_derived_lane")


def _slot(lane_id, measure, kind="mode"):
    return Slot(f"{lane_id}|{measure}-{measure}|{kind}", kind, (lane_id,), measure, measure, (
        Option("derive", "derive", {"mode": "derive"}, {}, ()),
        Option("rest", "rest", {"mode": "rest"}, {}, ()),
    ), {})


def _synthetic_sheet(slots_per_segment):
    lane = _lane("P|1|v1|r0")
    signature = MeasureSignature(1, ((lane.lane_id, False),), None)
    slots = [_slot(lane.lane_id, measure, kind) for measure, count in enumerate(slots_per_segment, start=1)
             for kind in ("mode", "melody_source", "lyric_line")[:count]]
    return Sheet(target_part_index=0, lanes=[lane], slots=slots, segments=[])


def test_requests_cut_only_at_segment_boundaries():
    sheet = _synthetic_sheet([1, 1, 1])
    requests = chooser.build_requests(sheet, {}, max_slots_per_request=2)
    assert [len(request.key_to_slot) for request in requests] == [2, 1]
    big = _synthetic_sheet([1, 3, 1])
    requests = chooser.build_requests(big, {}, max_slots_per_request=2)
    assert [sorted({int(slot_id.split("|")[-2].split("-")[0]) for slot_id in request.key_to_slot.values()})
            for request in requests] == [[1], [2], [3]]
    payload = json.loads(requests[1].dynamic_text)
    assert payload["slots"][0]["bars"] == "2-2"
    assert "evidence" not in payload["slots"][0] and "flags" not in payload["slots"][0]["options"][0]


# --- Replies -----------------------------------------------------------------

def test_parse_reply_reports_missing_extra_and_unknown():
    request, = chooser.build_requests(_synthetic_sheet([1, 1]), {})
    first, second = request.key_to_slot.values()
    assert chooser.parse_reply(request, '{"s1": "derive", "s2": "rest"}') == (
        {first: "derive", second: "rest"}, [],
    )
    assert chooser.parse_reply(request, '{"s1": "derive"}')[1] == ["s2: missing"]
    assert chooser.parse_reply(request, '{"s1": "derive", "s2": "x"}')[1] == ["s2: unknown option 'x'"]
    assert chooser.parse_reply(request, '{"s1": "derive", "s2": "rest", "s3": "rest"}')[1] == [
        "s3: unexpected key",
    ]
    assert chooser.parse_reply(request, "not json")[1]
    assert chooser.parse_reply(request, "[]")[1]


# --- choose -------------------------------------------------------------------

def test_invalid_reply_is_retried_once_then_raises(tmp_path):
    score = _chord_score(tmp_path)
    calls = []

    async def once_invalid(request):
        calls.append(request)
        return "{}" if len(calls) == 1 else _first_answer(request)

    sheet, choices, stats = asyncio.run(chooser.choose(score, _context(score), call=once_invalid))
    assert stats.retries == 1 and stats.requests == 1
    assert "Your previous answer was invalid: s1: missing" in calls[1].dynamic_text
    assert choices == assembler.first_option_choices(sheet)

    async def always_invalid(request):
        return "{}"

    with pytest.raises(chooser.ChooserError):
        asyncio.run(chooser.choose(score, _context(score), call=always_invalid))


def test_staves_are_asked_concurrently():
    score = _two_staff_part()
    started = 0

    async def run():
        both = asyncio.Event()

        async def call(request):
            nonlocal started
            started += 1
            if started >= 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2)
            return _first_answer(request)

        return await chooser.choose(score, {"target": {"part_id": "P1-Combined"}}, call=call)

    _, _, stats = asyncio.run(run())
    assert stats.requests >= 2 and started == stats.requests


def _gap_then_full(slot_lanes):
    """Freeform answers: the first leaves a gap, the retry covers the bar."""
    def answer(request, *, gap):
        reply = {}
        for key, slot_id in request.key_to_slot.items():
            if key in request.freeform_bars:
                start, end = request.freeform_bars[key]
                lanes = request.response_schema["properties"][key]["items"]["properties"]["lane"]["enum"]
                sections = [] if gap else [{"start_measure": start, "end_measure": end, "mode": "rest"}]
                reply[key] = [{"lane": lane, "sections": sections} for lane in lanes]
            else:
                reply[key] = request.response_schema["properties"][key]["enum"][0]
        return json.dumps(reply)
    return answer


def test_freeform_slot_is_one_array_key_and_a_gap_is_retried_once(tmp_path):
    score = _too_many_chord_claims(tmp_path)
    sheet = build_sheet(score, _context(score))
    freeform = next(slot for slot in sheet.slots if slot.kind == "freeform")
    request, = chooser.build_requests(sheet, chooser.build_request_context(score, _context(score), sheet))
    key = next(key for key, slot_id in request.key_to_slot.items() if slot_id == freeform.slot_id)
    schema = request.response_schema["properties"][key]
    assert schema["type"] == "array" and schema["minItems"] == len(freeform.lane_ids)
    section = schema["items"]["properties"]["sections"]["items"]
    assert {"start_measure", "end_measure", "mode", "melody_source"} <= set(section["properties"])
    assert "additionalProperties" not in json.dumps(schema)
    payload = json.loads(request.dynamic_text)
    assert payload["slots"][0]["evidence"]["per_measure_facts"]
    answer = _gap_then_full(freeform.lane_ids)
    calls = []

    async def call(request):
        calls.append(request)
        return answer(request, gap=len(calls) == 1)

    sheet, choices, stats = asyncio.run(chooser.choose(score, _context(score), call=call))
    assert stats.retries == 1 and len(calls) == 2
    assert "must cover bars" in calls[1].dynamic_text
    assert set(choices[freeform.slot_id]) == set(freeform.lane_ids)
    plan = chooser.assemble_chosen(sheet, choices, score=score)
    assert all(target["sections"] == [{"start_measure": 1, "end_measure": 1, "mode": "rest"}]
               for target in plan["targets"])


def _silent_second_voice(tmp_path, bars=8):
    """Voice 1 sings bars 1..bars; voice 2 is silent there and joins in the bar after."""
    measures = [_note("G", voice="1", lyric=f"w{index}") + '<backup><duration>4</duration></backup>'
                + _rest(voice="2", duration=4) for index in range(bars)]
    measures.append(_note("G", voice="1", lyric="end") + '<backup><duration>4</duration></backup>'
                    + _note("C", voice="2", lyric="low"))
    return _single_staff_score(tmp_path, measures)


def test_cuts_split_a_segment_and_none_changes_nothing(tmp_path):
    score = _silent_second_voice(tmp_path)
    context = _context(score)
    sheet = build_sheet(score, context)
    assert [(segment.start_measure, segment.end_measure) for segment in sheet.segments] == [(1, 8), (9, 9)]
    cut = build_sheet(score, context, cuts={(0, "1"): {5}})
    assert [(segment.start_measure, segment.end_measure) for segment in cut.segments] == [(1, 4), (5, 8), (9, 9)]
    assert build_sheet(score, context, cuts=None) == sheet


def _first_choices(sheet):
    return assembler.first_option_choices(sheet)


def _silent_lane(sheet):
    return next(lane for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")


def _rest_when_asked(asked):
    async def call(request):
        asked.append(request)
        payload = json.loads(request.dynamic_text)
        reply = {}
        for entry in payload["slots"]:
            enum = request.response_schema["properties"][entry["key"]]["enum"]
            reply[entry["key"]] = "rest" if entry["kind"] == "mode" and "rest" in enum else enum[0]
        return json.dumps(reply)
    return call


def test_repair_asks_only_the_failing_bars_with_findings_previous_and_neighbours(tmp_path):
    score = _silent_second_voice(tmp_path)
    context = _context(score)
    sheet = build_sheet(score, context)
    choices = _first_choices(sheet)
    lane = _silent_lane(sheet)
    finding = chooser.SlotFinding("partial_lyric_coverage", "P1", "Words missing.", 5, 6)
    asked = []
    new_sheet, new_choices, stats = asyncio.run(chooser.choose_repair(
        score, context, sheet=sheet, choices=choices, failing={lane.lane_id: [(5, 6)]},
        findings_by_lane={lane.lane_id: [finding]}, pinned=None, call=_rest_when_asked(asked),
    ))
    request, = asked
    assert list(request.key_to_slot.values()) == [f"{lane.lane_id}|5-6|mode"]
    payload = json.loads(request.dynamic_text)
    assert _xml_bars(payload["score_xml"]) == {"P1": [4, 5, 6, 7]}
    entry = payload["slots"][0]
    assert entry["previous"] == "derive"
    assert entry["findings"] == [{"rule": "partial_lyric_coverage", "severity": "P1",
                                  "message": "Words missing.", "bars": "5-6"}]
    # Neighbours give every choice next door, fixed ones included.
    assert entry["neighbours"]["before"]["mode"] == entry["neighbours"]["after"]["mode"] == "derive"
    flagged = next(option for option in entry["options"] if option["id"] == "derive")
    assert flagged["flags"] == ["failed_last_attempt: partial_lyric_coverage"]
    assert stats.slots_asked == 1
    assert new_choices[f"{lane.lane_id}|1-4|mode"] == "derive"
    assert new_choices[f"{lane.lane_id}|7-8|mode"] == "derive"
    # A non-default structure answer is pinned by the second pass, so it is fixed now.
    assert new_sheet.fixed[f"{lane.lane_id}|5-6|mode"].option_id == "rest"
    before = assembler.expand(next(target for target in assembler.assemble_plan(
        sheet, choices, score=score)["targets"] if target["source"]["voice_part_id"] == "voice part 2")["sections"])
    after = assembler.expand(next(target for target in chooser.assemble_chosen(
        new_sheet, new_choices, score=score)["targets"] if target["source"]["voice_part_id"] == "voice part 2")["sections"])
    assert {measure for measure in before if before[measure] != after[measure]} == {5, 6}


def test_repair_drops_an_option_that_failed_with_a_p0(tmp_path):
    score = _silent_second_voice(tmp_path)
    context = _context(score)
    sheet = build_sheet(score, context)
    lane = _silent_lane(sheet)
    asked = []
    asyncio.run(chooser.choose_repair(
        score, context, sheet=sheet, choices=_first_choices(sheet), failing={lane.lane_id: [(5, 6)]},
        findings_by_lane={lane.lane_id: [chooser.SlotFinding("unsung_notes", "P0", "Broken.", 5, 6)]},
        pinned=None, call=_rest_when_asked(asked),
    ))
    request, = asked
    assert request.response_schema["properties"]["s1"]["enum"] == ["rest"]


def _mode_section(plan, voice_part, measure):
    target = next(target for target in plan["targets"] if target["source"]["voice_part_id"] == voice_part)
    return assembler.expand(target["sections"])[measure]["mode"]


def test_second_repair_keeps_the_first_repairs_cuts_and_answer(tmp_path):
    score = _silent_second_voice(tmp_path)
    context = _context(score)
    sheet = build_sheet(score, context)
    lane = _silent_lane(sheet)

    async def repair(sheet, choices, span):
        asked = []
        result = await chooser.choose_repair(
            score, context, sheet=sheet, choices=choices, failing={lane.lane_id: [span]},
            findings_by_lane={}, pinned=None, call=_rest_when_asked(asked),
        )
        return (*result, asked)

    first_sheet, first_choices, _, _ = asyncio.run(repair(sheet, _first_choices(sheet), (5, 6)))
    second_sheet, second_choices, _, asked = asyncio.run(repair(first_sheet, first_choices, (1, 2)))
    assert [(segment.start_measure, segment.end_measure) for segment in second_sheet.segments] == [
        (1, 2), (3, 4), (5, 6), (7, 8), (9, 9)]
    assert list(asked[0].key_to_slot.values()) == [f"{lane.lane_id}|1-2|mode"]
    plan = chooser.assemble_chosen(second_sheet, second_choices, score=score)
    modes = [_mode_section(plan, "voice part 2", measure) for measure in range(1, 9)]
    assert modes == ["rest", "rest", "derive", "derive", "rest", "rest", "derive", "derive"]


def test_repair_inside_a_freeform_slot_clips_the_carried_sections(tmp_path):
    bar = (
        _note("C", voice="1", duration=2)
        + _note("E", voice="1", chord=True, duration=2, lyric=None)
        + _rest(voice="1", duration=2)
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="2", duration=2)
        + _note("G", voice="2", duration=2)
        + _note("B", voice="2", chord=True, duration=2, lyric=None)
    )
    score = _single_staff_score(tmp_path, [bar] * 8)
    context = _context(score)
    sheet = build_sheet(score, context)
    freeform, = [slot for slot in sheet.slots if slot.kind == "freeform"]
    assert (freeform.start_measure, freeform.end_measure) == (1, 8)
    head = {"start_measure": 1, "end_measure": 4, "mode": "rest"}
    tail = {"start_measure": 5, "end_measure": 8, "mode": "rest"}
    choices = {freeform.slot_id: {lane_id: [dict(head), dict(tail)] for lane_id in freeform.lane_ids}}
    asked = []

    async def call(request):
        asked.append(request)
        return _gap_then_full(freeform.lane_ids)(request, gap=False)

    new_sheet, new_choices, stats = asyncio.run(chooser.choose_repair(
        score, context, sheet=sheet, choices=choices, failing={freeform.lane_ids[0]: [(5, 6)]},
        findings_by_lane={}, pinned=None, call=call,
    ))
    prefix = freeform.slot_id.rsplit("|", 2)[0]
    assert stats.slots_asked == 1 and list(asked[0].key_to_slot.values()) == [f"{prefix}|5-6|freeform"]
    assert new_choices[f"{prefix}|1-4|freeform"] == {lane_id: [head] for lane_id in freeform.lane_ids}
    assert new_choices[f"{prefix}|7-8|freeform"] == {
        lane_id: [{**tail, "start_measure": 7}] for lane_id in freeform.lane_ids}
    plan = chooser.assemble_chosen(new_sheet, new_choices, score=score)
    assert all(target["sections"] == [{"start_measure": 1, "end_measure": 8, "mode": "rest"}]
               for target in plan["targets"])


def test_repair_over_fixed_slots_asks_nothing(tmp_path):
    score = parse_score(SATB, verse_number=1)
    context = {"target": {"part_id": "P1-Staff1"}}
    sheet = build_sheet(score, context)
    lane = sheet.lanes[0]

    async def call(request):
        raise AssertionError("no call expected")

    new_sheet, choices, stats = asyncio.run(chooser.choose_repair(
        score, context, sheet=sheet, choices=_first_choices(sheet), failing={lane.lane_id: [(1, 1)]},
        findings_by_lane={}, pinned=None, call=call,
    ))
    assert stats.slots_asked == 0 and stats.requests == 0
    assert choices == _first_choices(new_sheet)


def test_non_default_voice_assignment_asks_only_the_changed_slots(tmp_path):
    score = _chord_beside_a_line(tmp_path)
    first_sheet = build_sheet(score, _context(score))
    assignment = next(slot for slot in first_sheet.slots if slot.kind == "voice_assignment")
    other = next(option for option in assignment.options
                 if option.fields["melody_source_by_lane"] != assignment.options[0].fields["melody_source_by_lane"])
    asked: list[chooser.ChooserRequest] = []

    async def call(request):
        asked.append(request)
        answer = json.loads(_first_answer(request))
        for key, slot_id in request.key_to_slot.items():
            if slot_id == assignment.slot_id:
                answer[key] = other.option_id
        return json.dumps(answer)

    sheet, choices, stats = asyncio.run(chooser.choose(score, _context(score), call=call))
    assert len(asked) == 2 and stats.requests == 2
    assert all("score_xml" in json.loads(request.dynamic_text) for request in asked)
    expected = build_sheet(score, _context(score), pinned={assignment.slot_id: other.option_id})
    assert sheet == expected
    first_menus = {slot.slot_id: [option.option_id for option in slot.options] for slot in first_sheet.slots}
    changed = [slot.slot_id for slot in expected.slots
               if first_menus.get(slot.slot_id) != [option.option_id for option in slot.options]]
    assert changed and list(asked[1].key_to_slot.values()) == changed
    assert [slot_id.rsplit("|", 1)[-1] for slot_id in changed] == ["mode"]
    assert stats.second_pass_slots == len(changed)
    assert sheet.fixed[assignment.slot_id] == other
    assert set(choices) == {slot.slot_id for slot in sheet.slots}
    plan = assembler.assemble_plan(sheet, choices, score=score)
    assert plan["targets"]


def test_first_options_everywhere_means_no_second_pass_and_the_same_plan(tmp_path):
    for name, score, context in _fixtures(tmp_path):
        sheet, choices, stats = asyncio.run(chooser.choose(score, context, call=_first_option_call))
        expected_sheet = build_sheet(score, context)
        assert sheet == expected_sheet, name
        assert stats.second_pass_slots == 0 and stats.retries == 0, name
        assert stats.slots_asked == len(expected_sheet.slots), name
        assert assembler.assemble_plan(sheet, choices, score=score) == assembler.assemble_plan(
            expected_sheet, assembler.first_option_choices(expected_sheet), score=score,
        ), name


def test_gap_evidence_sent_to_chooser_contains_only_found_neighbour_bars(tmp_path):
    score = _gap_score(tmp_path)
    context = _context(score)
    sheet = build_sheet(score, context)
    slot = Slot(
        slot_id="synthetic-gap", kind="mode", lane_ids=(sheet.lanes[0].lane_id,),
        start_measure=3, end_measure=3,
        options=(Option("derive", "derive", {}, {}, ()),), evidence={},
    )
    request_context = chooser.build_request_context(score, context, sheet)
    for start, end, expected in ((3, 3, [2, 3]), (3, 5, [2, 3, 5, 6])):
        question = replace(slot, start_measure=start, end_measure=end)
        request = chooser._request(sheet, request_context, [question])
        xml = json.loads(request.dynamic_text)["score_xml"]
        assert [int(measure.get("number")) for measure in ET.fromstring(xml).iter()
                if measure.tag == "measure"] == expected
        assert asyncio.run(_first_option_call(request))
