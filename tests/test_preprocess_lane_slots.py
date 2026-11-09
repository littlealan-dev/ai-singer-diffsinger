"""Skeleton slots remain the derived lane identity through plan execution."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.api.preprocess_assembler import assemble_plan, first_option_choices
from src.api.preprocess_sheet import build_sheet, build_sheet_skeleton
from src.api.voice_parts import (
    _normalize_output_lane_request,
    _resolve_plan_output_lanes,
    _staff_scope_key,
    preprocess_voice_parts,
)
from tests.test_preprocess_lane_layout import _turn_taking_chords
from tests.test_preprocess_sheet import _note, _single_staff_score


def _context(score):
    return {"target": {"part_id": score["parts"][0]["part_id"]}}


def _sheet_for_layout(score, layout):
    sheet = build_sheet(score, _context(score))
    layout_slots = [slot for slot in sheet.slots if slot.kind == "lane_layout"]
    if layout_slots:
        assert len(layout_slots) == 1
        sheet = build_sheet(score, _context(score), pinned={layout_slots[0].slot_id: layout})
    return sheet


@pytest.mark.parametrize("voices,chord_size", [(2, 2), (2, 1), (3, 2)])
@pytest.mark.parametrize("layout", ["shared", "separate"])
def test_output_part_keys_match_skeleton_slots(tmp_path, voices, chord_size, layout):
    score = _turn_taking_chords(
        tmp_path, voices=voices, chord_size=chord_size, bars_per_voice=2
    )
    sheet = _sheet_for_layout(score, layout)
    plan = assemble_plan(sheet, first_option_choices(sheet), score=score)
    assert [target["output"]["slot"] for target in plan["targets"]] == [
        lane.slot for lane in sheet.lanes
    ]

    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready"
    actual = []
    for target in result["targets"]:
        source = target["derived_lane"]["source"]
        output = result["score"]["parts"][target["appended_part_ref"]["part_index"]]
        staffs = {_staff_scope_key(note) for note in output["notes"] if not note.get("is_rest")}
        assert len(staffs) == 1
        actual.append((
            score["parts"][source["part_index"]]["part_id"], staffs.pop(),
            source["voice_part_id"], target["derived_lane"]["slot"],
        ))
    assert actual == [
        (lane.part_id, lane.scope_key, lane.source_voice_part_id, lane.slot)
        for lane in sheet.lanes
    ]


def test_reversing_plan_targets_preserves_each_skeleton_slot(tmp_path):
    score = _single_staff_score(
        tmp_path, [_note("C", voice="1", lyric="word")
                   + _note("E", voice="1", chord=True, lyric=None)]
    )
    sheet = build_sheet(score, _context(score))
    assert [lane.slot for lane in sheet.lanes] == [1, 2]
    plan = assemble_plan(sheet, first_option_choices(sheet), score=score)
    plan["targets"].reverse()

    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready"
    assert [target["derived_lane"]["slot"] for target in result["targets"]] == [2, 1]


def test_hidden_shared_line_slot_does_not_shift_sibling_voice(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", voice="1", lyric="word")])
    score["voice_part_transforms"] = {
        "hidden": {"derived_lane": {
            "derived_lane_id": "dl:p0:source:default:slot:1",
            "source": {"part_index": 0, "voice_part_id": "voice part _default"},
            "slot": 1,
        }}
    }
    assert _normalize_output_lane_request({
        "mode": "append_new_derived_lane", "slot": 1
    })["slot"] == 1
    plan = {"targets": [{
        "uses_derived_lane_contract": True,
        "source": {"part_index": 0, "voice_part_id": "voice part 1"},
        "output": {"mode": "append_new_derived_lane", "slot": 1},
    }]}
    resolved = _resolve_plan_output_lanes(score, plan)
    assert resolved["ok"]
    assert resolved["plan"]["targets"][0]["output"]["slot"] == 1


def _two_staff_one_voice_score(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", voice="1", lyric="word")])
    other_staff_note = deepcopy(score["parts"][0]["notes"][0])
    other_staff_note.update(staff="2", measure_number=2)
    score["parts"][0]["notes"].append(other_staff_note)
    return score


def test_one_voice_on_two_staves_keeps_global_slots_and_finds_existing(tmp_path):
    score = _two_staff_one_voice_score(tmp_path)
    skeleton = build_sheet_skeleton(score, _context(score))
    assert [(lane.scope_key, lane.source_voice_part_id, lane.slot) for lane in skeleton.lanes] == [
        ("1", "voice part 1", 1), ("2", "voice part 1", 2)
    ]
    for reverse in (False, True):
        lanes = list(reversed(skeleton.lanes)) if reverse else skeleton.lanes
        plan = {"targets": [{
            "uses_derived_lane_contract": True,
            "source": {"part_index": 0, "voice_part_id": lane.source_voice_part_id},
            "output": {"mode": "append_new_derived_lane", "slot": lane.slot},
        } for lane in lanes]}
        resolved = _resolve_plan_output_lanes(score, plan)
        assert resolved["ok"]
        assert [target["output"]["slot"] for target in resolved["plan"]["targets"]] == [
            lane.slot for lane in lanes
        ]

    existing = deepcopy(score)
    existing["parts"].extend([
        {"part_id": f"P_DERIVED_{slot}", "part_name": f"Lane {slot} (Derived)", "notes": []}
        for slot in (1, 2)
    ])
    existing["voice_part_transforms"] = {
        str(slot): {
            "appended_part_ref": {"part_index": slot},
            "derived_lane": {
                "derived_lane_id": f"dl:p0:source:voice-part-1:slot:{slot}",
                "source": {"part_index": 0, "voice_part_id": "voice part 1"},
                "slot": slot,
            },
        }
        for slot in (1, 2)
    }
    again = build_sheet_skeleton(existing, _context(existing))
    assert [lane.slot for lane in again.lanes] == [1, 2]
    assert [lane.output_mode for lane in again.lanes] == ["update_existing_derived_lane"] * 2
    assert [lane.existing_derived_lane_id for lane in again.lanes] == [
        f"dl:p0:source:voice-part-1:slot:{slot}" for slot in (1, 2)
    ]


def test_two_targets_cannot_claim_one_explicit_slot(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", voice="1", lyric="word")])
    target = {
        "uses_derived_lane_contract": True,
        "source": {"part_index": 0, "voice_part_id": "voice part 1"},
        "output": {"mode": "append_new_derived_lane", "slot": 1},
    }
    resolved = _resolve_plan_output_lanes(score, {"targets": [deepcopy(target), deepcopy(target)]})
    assert not resolved["ok"]
    assert resolved["error"]["action"] == "derived_lane_slot_taken"


def test_missing_slot_keeps_first_free_allocation(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", voice="1", lyric="word")])
    assert _normalize_output_lane_request({"mode": "append_new_derived_lane", "slot": 2})["slot"] == 2
    assert "slot" not in _normalize_output_lane_request({"mode": "append_new_derived_lane", "slot": 0})
    assert "slot" not in _normalize_output_lane_request({"mode": "append_new_derived_lane", "slot": True})
    plan = {"targets": [{
        "uses_derived_lane_contract": True,
        "source": {"part_index": 0, "voice_part_id": "voice part 1"},
        "output": {"mode": "append_new_derived_lane"},
    }, {
        "uses_derived_lane_contract": True,
        "source": {"part_index": 0, "voice_part_id": "voice part 1"},
        "output": {"mode": "append_new_derived_lane"},
    }]}
    resolved = _resolve_plan_output_lanes(score, plan)
    assert resolved["ok"]
    assert [target["output"]["slot"] for target in resolved["plan"]["targets"]] == [1, 2]
