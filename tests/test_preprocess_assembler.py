"""Exact offline assembly of the public preprocess plan."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest

from src.api import parse_score
from src.api import preprocess_assembler as assembler
from src.api.preprocess_sheet import build_sheet
from src.api.voice_parts import parse_voice_part_plan, preprocess_voice_parts
from src.mcp.tools import _PREPROCESS_PLAN_REQUEST_SCHEMA
from tests.test_preprocess_sheet import SATB, _chord_score


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
    score = parse_score(SATB, verse_number=1) if fixture == "satb" else _chord_score(tmp_path)
    part_id = score["parts"][0]["part_id"]
    original = deepcopy(score)
    sheet = _sheet(score, part_id)
    choices = assembler.first_option_choices(sheet)
    plan = assembler.assemble_plan(sheet, choices)
    assert plan == assembler.assemble_plan(sheet, choices)
    _assert_schema(plan, _PREPROCESS_PLAN_REQUEST_SCHEMA)
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
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
    for target in plan["targets"]:
        for section in target["sections"]:
            assert section["decision_type"] == "SPLIT_CHORDS_SELECT_NOTES"
            assert section["method"] == "ranked"
            assert isinstance(section["rank_index"], int)
            assert section["rank_fallback"] in {"greedy", "skip"}
    assert [target["sections"][0]["rank_index"] for target in plan["targets"]] == [0, 1]


def test_rest_and_copy_unison_decision_rows(tmp_path):
    score = _chord_score(tmp_path)
    sheet = _sheet(score, score["parts"][0]["part_id"])
    choices = assembler.first_option_choices(sheet)
    rest_slot = next(slot for slot in sheet.slots if slot.kind == "mode")
    choices[rest_slot.slot_id] = next(option.option_id for option in rest_slot.options
                                      if option.fields["mode"] == "rest")
    plan = assembler.assemble_plan(sheet, choices)
    assert plan["targets"][1]["sections"][-1]["decision_type"] == "INSERT_RESTS"
    assert plan["targets"][1]["sections"][-1]["mode"] == "rest"
    assert parse_voice_part_plan(plan, score=score)["ok"]

    satb = parse_score(SATB, verse_number=1)
    sheet = _sheet(satb, "P1-Staff1")
    choices = assembler.first_option_choices(sheet)
    source_slot = next(slot for slot in sheet.slots
                       if slot.kind == "melody_source" and len(slot.options) > 1)
    own_voice = source_slot.lane_ids[0].split("|")[2]
    copy_option = next(option for option in source_slot.options
                       if option.fields["melody_source"]["voice_part_id"] != own_voice)
    choices[source_slot.slot_id] = copy_option.option_id
    plan = assembler.assemble_plan(sheet, choices)
    assert any(section["decision_type"] == "COPY_UNISON_SECTION"
               for target in plan["targets"] for section in target["sections"])
    _assert_schema(plan, _PREPROCESS_PLAN_REQUEST_SCHEMA)
    assert parse_voice_part_plan(plan, score=satb)["ok"]


def test_invalid_choices_freeform_and_forced_roundtrip_failure(monkeypatch, tmp_path):
    score = _chord_score(tmp_path)
    sheet = _sheet(score, score["parts"][0]["part_id"])
    choices = assembler.first_option_choices(sheet)
    first_id = next(iter(choices))
    with pytest.raises(ValueError, match="Missing choices"):
        assembler.assemble_plan(sheet, {key: value for key, value in choices.items() if key != first_id})
    with pytest.raises(ValueError, match="Unknown option ID"):
        assembler.assemble_plan(sheet, {**choices, first_id: "missing"})
    with pytest.raises(ValueError, match="unknown slot IDs"):
        assembler.assemble_plan(sheet, {**choices, "not-a-slot": "x"})

    monkeypatch.setattr("src.api.preprocess_sheet._source_candidates", lambda *_: [])
    freeform_sheet = _sheet(score, score["parts"][0]["part_id"])
    with pytest.raises(assembler.FreeformUnresolved) as error:
        assembler.assemble_plan(freeform_sheet, assembler.first_option_choices(freeform_sheet))
    assert error.value.slot_ids == sorted(slot.slot_id for slot in freeform_sheet.slots)

    monkeypatch.setattr(assembler, "expand", lambda sections: {})
    with pytest.raises(assembler.AssemblyError, match="Round-trip mismatch"):
        assembler.assemble_plan(sheet, choices)


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
    plan = assembler.assemble_plan(sheet, choices)
    assert [target["output"] for target in plan["targets"]] == [
        {"mode": "append_new_derived_lane"},
        {"mode": "update_existing_derived_lane", "derived_lane_id": "dl:test:2"},
    ]
    assert parse_voice_part_plan(plan, score=score)["ok"]
    missing_id = replace(sheet, lanes=[sheet.lanes[0], replace(sheet.lanes[1], existing_derived_lane_id=None)])
    with pytest.raises(assembler.AssemblyError, match="lacks derived_lane_id"):
        assembler.assemble_plan(missing_id, choices)
