"""Exact offline assembly of the public preprocess plan."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest

from src.api import parse_score
from src.api import preprocess_assembler as assembler
from src.api.preprocess_sheet import build_sheet, lyric_bar_cases, staff_lyric_lines
from src.api.voice_parts import (
    _propagate_lyrics, _resolve_staff_lyric_source_notes, parse_voice_part_plan,
    preprocess_voice_parts,
)
from src.mcp.tools import _PREPROCESS_PLAN_REQUEST_SCHEMA
from tests.test_preprocess_sheet import SATB, _chord_score, _parse_inline, _silence_voice_in_bar, _two_staff_score


def _shared_line_score(tmp_path):
    xml = '''<score-partwise version="3.1"><part-list><score-part id="P1">
<part-name>Choir</part-name></score-part></part-list><part id="P1"><measure number="1">
<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
<clef><sign>G</sign><line>2</line></clef></attributes>
<note><pitch><step>G</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice>
<type>half</type><lyric><text>first</text></lyric></note>
<note><pitch><step>A</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice>
<type>half</type></note><backup><duration>4</duration></backup>
<note><pitch><step>C</step><octave>5</octave></pitch><duration>2</duration><voice>2</voice>
<type>half</type></note>
<note><pitch><step>D</step><octave>5</octave></pitch><duration>2</duration><voice>2</voice>
<type>half</type><lyric><text>second</text></lyric></note>
</measure></part></score-partwise>'''
    return _parse_inline(tmp_path, xml)


def test_borrowed_staff_line_reads_both_voice_words_across_parts(tmp_path):
    score = _shared_line_score(tmp_path)
    recipient = deepcopy(score["parts"][0])
    recipient["part_id"] = "P2"
    for note in recipient["notes"]:
        note["lyric"] = None
        note["lyric_is_extended"] = False
    score["parts"].append(recipient)

    sheet = _sheet(score, "P2")
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(
        section["lyric_source_line"] == {"staff": "1", "voice_part_id": None}
        for target in plan["targets"] for section in target["sections"]
    )
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready"
    for target in result["targets"]:
        notes = result["score"]["parts"][target["appended_part_ref"]["part_index"]]["notes"]
        assert [note.get("lyric") for note in notes if note.get("lyric")] == ["first", "second"]


def test_borrowed_staff_line_deduplicates_chords_and_can_select_voice(tmp_path):
    score = _shared_line_score(tmp_path)
    first_word = next(note for note in score["parts"][0]["notes"]
                      if note.get("lyric") == "first")
    chord_tone = dict(first_word, pitch_midi=first_word["pitch_midi"] + 4)
    score["parts"][0]["notes"].append(chord_tone)
    arguments = dict(source_part_index=0, staff="1", verse_number=None,
                     copy_all_verses=False, measure_range=(1, 1))
    whole_line = _resolve_staff_lyric_source_notes(
        score, source_voice_part_id=None, **arguments
    )
    voice_line = _resolve_staff_lyric_source_notes(
        score, source_voice_part_id="voice part 1", **arguments
    )
    assert [note["lyric"] for note in whole_line] == ["first", "second"]
    assert [note["lyric"] for note in voice_line] == ["first"]


def test_shared_staff_line_plan_executes_without_quality_failure(tmp_path):
    score = _shared_line_score(tmp_path)
    strategy_schema = _PREPROCESS_PLAN_REQUEST_SCHEMA["properties"]["targets"]["items"]["properties"]["sections"]["items"]["properties"]["lyric_strategy"]
    assert "staff_line" not in strategy_schema["enum"]
    sheet = _sheet(score, score["parts"][0]["part_id"])
    assert not any(slot.kind == "lyric_reading" for slot in sheet.slots)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(section["lyric_strategy"] == "staff_line"
               for target in plan["targets"] for section in target["sections"])
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready"
    assert not result.get("section_quality_issues")


def test_different_words_on_same_beat_offer_line_not_reading(tmp_path):
    score = _shared_line_score(tmp_path)
    for note in score["parts"][0]["notes"]:
        if note.get("voice") == "2" and note["offset_beats"] == 0:
            note["lyric"] = "other"
    sheet = _sheet(score, score["parts"][0]["part_id"])
    assert any(slot.kind == "lyric_line" for slot in sheet.slots)
    assert not any(slot.kind == "lyric_reading" for slot in sheet.slots)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(section["lyric_strategy"] == "staff_line"
               for target in plan["targets"] for section in target["sections"])
    assert all(section["lyric_source_line"]["voice_part_id"] is not None
               for target in plan["targets"] for section in target["sections"])


def test_ambiguous_reading_choice_applies_only_to_ambiguous_bar(tmp_path):
    score = _shared_line_score(tmp_path)
    part = score["parts"][0]
    shared = deepcopy(part["notes"])
    for note in shared:
        note["measure_number"] = 2
        note["offset_beats"] += 4
    part["notes"] = [note for note in part["notes"]
                     if not (note.get("voice") == "2" and note["offset_beats"] == 0)]
    for note in part["notes"]:
        if note.get("voice") == "2":
            note["offset_beats"] = 1.0
    part["notes"].extend(shared)
    sheet = _sheet(score, part["part_id"])
    slot = next(slot for slot in sheet.slots if slot.kind == "lyric_reading")
    assert slot.evidence == {"ambiguous_bars": {"1": ["independent_rhythm"]}}
    assert [option.option_id for option in slot.options] == ["own_words_only", "share_line"]
    choices = assembler.first_option_choices(sheet)
    plan = assembler.assemble_plan(sheet, choices, score=score)
    own = next(target for target in plan["targets"]
               if target["source"]["voice_part_id"] == "voice part 1")
    assert all(action["lyric_strategy"] == "staff_line"
               for action in assembler.expand(own["sections"]).values())
    assert assembler.expand(own["sections"])[1]["lyric_source_line"]["voice_part_id"] == "voice part 1"
    assert assembler.expand(own["sections"])[2]["lyric_source_line"]["voice_part_id"] is None
    choices[slot.slot_id] = "share_line"
    plan = assembler.assemble_plan(sheet, choices, score=score)
    own = next(target for target in plan["targets"]
               if target["source"]["voice_part_id"] == "voice part 1")
    assert all(action["lyric_strategy"] == "staff_line"
               for action in assembler.expand(own["sections"]).values())


def test_staff_line_earlier_word_hold_tie_and_range():
    source = [{"staff": "1", "measure_number": 1, "offset_beats": 0.0,
               "duration_beats": 1.0, "pitch_midi": 60, "lyric": "first"},
              {"staff": "1", "measure_number": 2, "offset_beats": 2.0,
               "duration_beats": 1.0, "pitch_midi": 62, "lyric": "second"}]
    targets = [{"staff": "1", "measure_number": 1, "offset_beats": 0.0,
                "duration_beats": 1.0, "pitch_midi": 65},
               {"staff": "1", "measure_number": 2, "offset_beats": 1.0,
                "duration_beats": 1.0, "pitch_midi": 67},
               {"staff": "1", "measure_number": 2, "offset_beats": 2.0,
                "duration_beats": 1.0, "pitch_midi": 69, "tie_type": "stop"}]
    copied, _ = _propagate_lyrics(
        target_notes=targets, source_notes=source, strategy="staff_line", policy="replace_all",
        verse_number=None, copy_all_verses=False, measure_range=(2, 2),
    )
    assert copied[0].get("lyric") is None
    assert [note.get("lyric") for note in copied[1:]] == ["+", "+"]
    assert all(note.get("lyric_is_extended") for note in copied[1:])
    assert all(note.get("syllabic") is None for note in copied[1:])


def test_staff_line_tied_own_word_and_section_diagnostics():
    source = [
        {"staff": "1", "voice": "1", "measure_number": 1, "offset_beats": 0.0,
         "duration_beats": 1.0, "pitch_midi": 60, "lyric": "earlier"},
        {"staff": "1", "voice": "1", "measure_number": 2, "offset_beats": 4.0,
         "duration_beats": 1.0, "pitch_midi": 60, "lyric": "own", "tie_type": "stop"},
        {"staff": "1", "voice": "2", "measure_number": 2, "offset_beats": 5.0,
         "duration_beats": 1.0, "pitch_midi": 65, "lyric": "other"},
    ]
    targets = [dict(source[1]),
               {"staff": "1", "voice": "1", "measure_number": 2, "offset_beats": 5.0,
                "duration_beats": 1.0, "pitch_midi": 60, "tie_type": "continue"}]
    copied, diagnostics = _propagate_lyrics(
        target_notes=targets, source_notes=source, strategy="staff_line", policy="replace_all",
        verse_number=None, copy_all_verses=False, measure_range=(2, 2),
    )
    assert [note.get("lyric") for note in copied] == ["own", "+"]
    assert diagnostics["source_lyric_candidates_count"] == 2
    assert diagnostics["dropped_source_lyrics_count"] == 1
    assert [item["lyric"] for item in diagnostics["dropped_source_lyrics"]] == ["other"]


def test_tied_lane_bar_uses_staff_line_and_keeps_hold(tmp_path):
    _shared_line_score(tmp_path)
    xml = (tmp_path / "synthetic.xml").read_text(encoding="utf-8")
    xml = xml.replace('<type>half</type></note><backup>',
                      '<type>half</type><tie type="start"/></note><backup>')
    xml = xml.replace('</measure></part>', '''</measure><measure number="2">
<note><pitch><step>A</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice>
<type>whole</type><tie type="stop"/></note><backup><duration>4</duration></backup>
<note><rest/><duration>2</duration><voice>2</voice><type>half</type></note>
<note><pitch><step>C</step><octave>5</octave></pitch><duration>2</duration><voice>2</voice>
<type>half</type><lyric><text>third</text></lyric></note></measure></part>''')
    score = _parse_inline(tmp_path, xml)
    cases = lyric_bar_cases(score, staff_lyric_lines(score), 0, "1", "voice part 1", None)
    assert cases[2].case == "no_word_for_lane"
    sheet = _sheet(score, score["parts"][0]["part_id"])
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    lane = next(target for target in plan["targets"]
                if target["source"]["voice_part_id"] == "voice part 1")
    assert assembler.expand(lane["sections"])[2]["lyric_strategy"] == "staff_line"
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready"
    ref = result["targets"][0]["appended_part_ref"]["part_index"]
    tied = [note for note in result["score"]["parts"][ref]["notes"]
            if note["measure_number"] == 2]
    assert [note.get("lyric") for note in tied] == ["+"]


def _assert_schema(value, schema):
    """Check the public schema features used by a plan, without another dependency."""
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    assert any(matches[kind](value) for kind in kinds)
    if "enum" in schema:
        assert value in schema["enum"]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= value.keys()
        if schema.get("additionalProperties") is False:
            assert value.keys() <= properties.keys()
        for key, child in value.items():
            if key in properties:
                _assert_schema(child, properties[key])
    if isinstance(value, list):
        assert len(value) >= schema.get("minItems", 0)
        for child in value:
            _assert_schema(child, schema["items"])
    if isinstance(value, int) and not isinstance(value, bool):
        assert value >= schema.get("minimum", value)


def _sheet(score, part_id):
    return build_sheet(score, {"target": {"part_id": part_id}})


def test_run_length_merge_exact_randomized_and_boundaries():
    random = Random(138)
    actions = [
        {"mode": "rest", "decision_type": "INSERT_RESTS"},
        {"mode": "derive", "decision_type": "EXTRACT_FROM_VOICE", "rank_index": 0},
        {"mode": "derive", "decision_type": "EXTRACT_FROM_VOICE", "rank_index": 1},
    ]
    for _ in range(50):
        expected = {measure: deepcopy(random.choice(actions)) for measure in range(1, 41)}
        merged = assembler.run_length_merge(expected)
        assert assembler.expand(merged) == expected
        assert all(left["end_measure"] + 1 == right["start_measure"]
                   or {k: v for k, v in left.items() if k not in {"start_measure", "end_measure"}}
                   != {k: v for k, v in right.items() if k not in {"start_measure", "end_measure"}}
                   for left, right in zip(merged, merged[1:]))
    assert assembler.expand(assembler.run_length_merge({7: actions[0]})) == {7: actions[0]}
    gap = {1: actions[0], 3: actions[0]}
    assert len(assembler.run_length_merge(gap)) == 2
    assert assembler.expand(assembler.run_length_merge(gap)) == gap
    with pytest.raises(assembler.AssemblyError, match="Overlapping"):
        assembler.expand([
            {"start_measure": 1, "end_measure": 2, **actions[0]},
            {"start_measure": 2, "end_measure": 3, **actions[1]},
        ])


@pytest.mark.parametrize("fixture", ["satb", "chord"])
def test_first_option_plan_is_public_valid_deterministic_and_executable(tmp_path, fixture):
    if fixture == "satb":
        satb_copy = tmp_path / SATB.name
        satb_copy.write_bytes(SATB.read_bytes())
        score = parse_score(satb_copy, verse_number=1)
    else:
        score = _chord_score(tmp_path)
    part_id = score["parts"][0]["part_id"]
    original = deepcopy(score)
    sheet = _sheet(score, part_id)
    choices = assembler.first_option_choices(sheet)
    plan = assembler.assemble_plan(sheet, choices, score=score)
    assert plan == assembler.assemble_plan(sheet, choices, score=score)
    # The v2 section line is validated by parse_voice_part_plan, outside the v1 MCP schema.
    assert parse_voice_part_plan(plan, score=score)["ok"]
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings", [])
                if finding.get("rule_severity") == "P0"]
    assert result.get("status") == "ready"
    assert score == original
    for target in plan["targets"]:
        sections = target["sections"]
        assert sections[0]["start_measure"] == 1
        assert sections[-1]["end_measure"] == max(
            int(note["measure_number"]) for note in score["parts"][0]["notes"]
        )
        assert all(left["end_measure"] + 1 == right["start_measure"]
                   for left, right in zip(sections, sections[1:]))


def test_chord_split_always_ranked_even_when_density_falls(tmp_path):
    score = _chord_score(tmp_path)
    sheet = _sheet(score, score["parts"][0]["part_id"])
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    for target in plan["targets"]:
        for section in target["sections"]:
            assert section["decision_type"] == "SPLIT_CHORDS_SELECT_NOTES"
            assert section["method"] == "ranked"
            assert isinstance(section["rank_index"], int)
            assert section["rank_fallback"] in {"greedy", "skip"}
    assert [target["sections"][0]["rank_index"] for target in plan["targets"]] == [0, 1]


def test_own_density_one_source_extracts_trivially():
    score = parse_score(SATB, verse_number=1)
    sheet = _sheet(score, "P1-Staff1")
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(section["decision_type"] == "EXTRACT_FROM_VOICE"
               and section["method"] == "trivial"
               for target in plan["targets"] for section in target["sections"])


def test_read_lines_encode_as_staff_line_replace_all():
    score = parse_score(SATB, verse_number=1)
    sheet = _sheet(score, "P1-Staff1")
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    for target in plan["targets"]:
        for section in target["sections"]:
            assert section["lyric_policy"] == "replace_all"
            assert section["lyric_strategy"] == "staff_line"
            assert section["lyric_source_line"] == {"staff": None, "voice_part_id": None}
            assert section["lyric_source"] == target["source"]

    for note in score["parts"][0]["notes"]:
        if note.get("voice") == "1" and note.get("measure_number") == 2:
            note["lyric"] = "+"
    extension = _sheet(score, "P1-Staff1")
    assert not any(slot.kind == "lyric_policy" for slot in extension.slots)
    plan = assembler.assemble_plan(extension, assembler.first_option_choices(extension), score=score)
    # Bar 2 has no word and keeps the same whole-line reading.
    assert all(len(target["sections"]) == 1 for target in plan["targets"])


def test_rest_and_copy_unison_decision_rows(tmp_path):
    # The alto is silent in bar 2, so its lane there rests or copies another voice.
    satb = _silence_voice_in_bar(parse_score(SATB, verse_number=1), 0, "2", 2)
    sheet = _sheet(satb, "P1-Staff1")
    choices = assembler.first_option_choices(sheet)
    rest_slot = next(slot for slot in sheet.slots if slot.kind == "mode")
    choices[rest_slot.slot_id] = next(option.option_id for option in rest_slot.options
                                      if option.fields["mode"] == "rest")
    plan = assembler.assemble_plan(sheet, choices, score=satb)
    lane_index = [lane.lane_id for lane in sheet.lanes].index(rest_slot.lane_ids[0])
    rest_section = next(section for section in plan["targets"][lane_index]["sections"]
                        if section["start_measure"] <= rest_slot.start_measure <= section["end_measure"])
    assert rest_section["decision_type"] == "INSERT_RESTS"
    assert rest_section["mode"] == "rest"
    assert {"method", "melody_source", "lyric_source"}.isdisjoint(rest_section)
    assert parse_voice_part_plan(plan, score=satb)["ok"]

    sheet = _sheet(satb, "P1-Staff1")
    choices = assembler.first_option_choices(sheet)
    source_slot = next(slot for slot in sheet.slots
                       if slot.kind == "melody_source" and len(slot.options) > 1)
    own_voice = source_slot.lane_ids[0].split("|")[2]
    copy_option = next(option for option in source_slot.options
                       if option.fields["melody_source"]["voice_part_id"] != own_voice)
    choices[source_slot.slot_id] = copy_option.option_id
    plan = assembler.assemble_plan(sheet, choices, score=satb)
    chosen = copy_option.fields["melody_source"]
    expected_source = {
        "part_id": satb["parts"][chosen["part_index"]]["part_id"],
        "voice_part_id": chosen["voice_part_id"],
    }
    copy_sections = [section for target in plan["targets"] for section in target["sections"]
                     if section["decision_type"] == "COPY_UNISON_SECTION"]
    assert copy_sections
    assert all(section["method"] == "trivial"
               and section["melody_source"] == expected_source for section in copy_sections)
    # The v2 section line is validated by parse_voice_part_plan, outside the v1 MCP schema.
    assert parse_voice_part_plan(plan, score=satb)["ok"]


def test_invalid_choices_freeform_and_forced_roundtrip_failure(monkeypatch, tmp_path):
    score = _chord_score(tmp_path)
    sheet = _sheet(score, score["parts"][0]["part_id"])
    choices = assembler.first_option_choices(sheet)
    first_id = next(iter(choices))
    with pytest.raises(ValueError, match="Missing choices"):
        assembler.assemble_plan(sheet, {key: value for key, value in choices.items() if key != first_id}, score=score)
    with pytest.raises(ValueError, match="Unknown option ID"):
        assembler.assemble_plan(sheet, {**choices, first_id: "missing"}, score=score)
    with pytest.raises(ValueError, match="unknown slot IDs"):
        assembler.assemble_plan(sheet, {**choices, "not-a-slot": "x"}, score=score)

    monkeypatch.setattr("src.api.preprocess_sheet._source_candidates", lambda *_: [])
    freeform_sheet = _sheet(score, score["parts"][0]["part_id"])
    with pytest.raises(assembler.FreeformUnresolved) as error:
        assembler.assemble_plan(freeform_sheet, assembler.first_option_choices(freeform_sheet), score=score)
    assert error.value.slot_ids == sorted(slot.slot_id for slot in freeform_sheet.slots)
    supplied = {
        slot.slot_id: [{
            "start_measure": slot.start_measure, "end_measure": slot.end_measure,
            "mode": "derive", "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
            "method": "ranked", "rank_index": 0, "rank_fallback": "greedy",
            "melody_source": {"part_id": "Solo", "voice_part_id": "voice part 1"},
            "lyric_source": {"part_id": "Solo", "voice_part_id": "voice part 1"},
            "lyric_strategy": "strict_onset", "lyric_policy": "fill_missing_only",
        }]
        for slot in freeform_sheet.slots
    }
    freeform_plan = assembler.assemble_plan(
        freeform_sheet, assembler.first_option_choices(freeform_sheet),
        freeform_sections=supplied, score=score)
    _assert_schema(freeform_plan, _PREPROCESS_PLAN_REQUEST_SCHEMA)
    assert parse_voice_part_plan(freeform_plan, score=score)["ok"]

    monkeypatch.setattr(assembler, "expand", lambda sections: {})
    with pytest.raises(assembler.AssemblyError, match="Round-trip mismatch"):
        assembler.assemble_plan(sheet, choices, score=score)


def test_update_lane_requires_and_carries_existing_derived_id(tmp_path):
    score = _chord_score(tmp_path)
    derived = deepcopy(score["parts"][0])
    derived["part_id"] = "P_DERIVED_TEST"
    score["parts"].append(derived)
    score["voice_part_transforms"] = {
        "synthetic": {
            "appended_part_ref": {"part_index": 1},
            "derived_lane": {"source": {"part_index": 0, "voice_part_id": "voice part 1"},
                             "slot": 2, "derived_lane_id": "dl:test:2"},
        }
    }
    sheet = _sheet(score, score["parts"][0]["part_id"])
    choices = assembler.first_option_choices(sheet)
    plan = assembler.assemble_plan(sheet, choices, score=score)
    assert [target["output"] for target in plan["targets"]] == [
        {"mode": "append_new_derived_lane"},
        {"mode": "update_existing_derived_lane", "derived_lane_id": "dl:test:2"},
    ]
    assert parse_voice_part_plan(plan, score=score)["ok"]
    missing_id = replace(sheet, lanes=[sheet.lanes[0], replace(sheet.lanes[1], existing_derived_lane_id=None)])
    with pytest.raises(assembler.AssemblyError, match="lacks derived_lane_id"):
        assembler.assemble_plan(missing_id, choices, score=score)


def test_real_two_staff_with_one_lyric_donor_assembles_first_options(tmp_path):
    score = _two_staff_score(tmp_path)
    original = deepcopy(score)
    for part in score["parts"]:
        sheet = _sheet(score, part["part_id"])
        assert not any(slot.kind == "freeform" for slot in sheet.slots)
        plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
        # The v2 section line is validated by parse_voice_part_plan, outside the v1 MCP schema.
        assert parse_voice_part_plan(plan, score=score)["ok"]
        result = preprocess_voice_parts(score, request={"plan": plan})
        assert not [finding for finding in result.get("lint_findings", [])
                    if finding.get("rule_severity") == "P0"]
    assert score == original
