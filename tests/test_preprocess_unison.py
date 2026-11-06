"""Who sings a single note in a split: the lane default, overridden bar by bar."""

from __future__ import annotations

from copy import deepcopy

from src.api.preprocess_assembler import assemble_plan, first_option_choices
from src.api.preprocess_sheet import build_sheet
from src.api.voice_parts import preprocess_voice_parts
from tests.test_preprocess_sheet import (
    _BACKUP, _SOLO, _chord, _note, _rest, _single_staff_score, _voices,
)


def _score(tmp_path, bar_two):
    # Bar 2: a chord, then (in bar_two) a marked single G. Bar 3: a chord, then an unmarked single E.
    return _single_staff_score(tmp_path, [
        _voices("G", "C"),
        bar_two,
        _chord("F", "D") + _note("E", voice="1", duration=2),
        _chord("E", "C", 4),
    ])


_SOLO_BAR = _chord("E", "C") + _SOLO + _note("G", voice="1", duration=2)


def _sheet(score, **kwargs):
    return build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}}, **kwargs)


def _lower(sheet):
    return max(sheet.lanes, key=lambda lane: lane.default_rank)


def _sung(score, sheet, answer):
    """Each lane's bar 2 and bar 3 events, with every unison slot answered `answer`."""
    choices = first_option_choices(sheet)
    for slot in sheet.slots:
        if slot.kind == "unison":
            choices[slot.slot_id] = answer
    plan = assemble_plan(sheet, choices, score=score)
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready", result
    lanes = []
    for target in result["targets"]:
        part = result["score"]["parts"][target["appended_part_ref"]["part_index"]]
        lanes.append({bar: [None if note.get("is_rest") else int(note["pitch_midi"])
                            for note in part["notes"] if note["measure_number"] == bar]
                      for bar in (2, 3)})
    return plan, lanes


def test_unison_is_asked_only_for_a_lower_lane_under_a_direction(tmp_path):
    score = _score(tmp_path, _SOLO_BAR)
    sheet = _sheet(score)
    unison = [slot for slot in sheet.slots if slot.kind == "unison"]
    # Bar 3's single E has no marking, and the top lane always sings a single note.
    assert [(slot.start_measure, slot.lane_ids) for slot in unison] == [(2, (_lower(sheet).lane_id,))]
    assert [option.option_id for option in unison[0].options] == ["every_lane", "top_lane_only"]
    assert unison[0].evidence == {"single_note_bars": [2]}


def test_unison_answer_decides_who_sings_the_single_note(tmp_path):
    score = _score(tmp_path, _SOLO_BAR)
    sheet = _sheet(score)
    default_plan = assemble_plan(sheet, first_option_choices(sheet), score=score)

    plan, (top, lower) = _sung(score, sheet, "every_lane")
    assert plan == default_plan  # every_lane writes nothing: the lane default applies
    assert lower == {2: [72, 79], 3: [74, 76]}

    plan, (top, lower) = _sung(score, sheet, "top_lane_only")
    assert lower == {2: [72], 3: [74, 76]}
    assert top == {2: [76, 79], 3: [77, 76]}
    sections = [section for target in plan["targets"] for section in target["sections"]
                if "split_shared_note_policy" in section]
    assert [(section["start_measure"], section["end_measure"], section["split_shared_note_policy"])
            for section in sections] == [(2, 2, "assign_primary_only")]


def test_unison_override_reaches_the_gaps_around_the_lanes_own_notes(tmp_path):
    # The lower lane's own voice writes a rest on beats 1-2, so the executor fills only beats 3-4.
    score = _score(tmp_path, _SOLO_BAR + _BACKUP + _rest(voice="2"))
    sheet = _sheet(score)
    assert [slot.start_measure for slot in sheet.slots if slot.kind == "unison"] == [2]
    assert _sung(score, sheet, "every_lane")[1][1][2] == [None, 79]
    assert _sung(score, sheet, "top_lane_only")[1][1][2] == [None]


def test_unison_is_not_asked_where_the_lanes_own_voice_writes_something(tmp_path):
    score = _score(tmp_path, _SOLO_BAR + _BACKUP + _rest(voice="2") + _rest(voice="2"))
    sheet = _sheet(score)
    assert not [slot for slot in sheet.slots if slot.kind == "unison"]
    assert _sung(score, sheet, "every_lane")[1][1][2] == [None, None]


def test_unison_is_not_asked_in_a_separate_layout(tmp_path):
    score = _score(tmp_path, _SOLO_BAR + _BACKUP + _rest(voice="2"))
    layout, = [slot for slot in _sheet(score).slots if slot.kind == "lane_layout"]
    separate = _sheet(score, pinned={layout.slot_id: "separate"})
    assert not [slot for slot in separate.slots if slot.kind == "unison"]
