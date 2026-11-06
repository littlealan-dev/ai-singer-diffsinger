"""One-line bars on a divided staff: code takes the staff's line; a lower lane's unison answers decide."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.api.preprocess_assembler import assemble_plan, first_option_choices
from src.api.preprocess_sheet import build_sheet
from src.api.voice_parts import preprocess_voice_parts
from tests.test_preprocess_sheet import (
    _BACKUP, _SOLO, _chord, _note, _parse_inline, _rest, _single_staff_score, _voices,
)


def _score(tmp_path, bar_two=None, bar_three=None):
    # Bars 1 and 3 divide; bars 2, 4 ("Solo") and 5 write one line for the whole staff.
    return _single_staff_score(tmp_path, [
        _voices("G", "C"),
        bar_two or _note("E"),
        bar_three or _voices("A", "F"),
        _SOLO + _note("D"),
        _note("E"),
    ])


def _sheet(score, **kwargs):
    return build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}}, **kwargs)


def _lanes(sheet):
    return sorted(sheet.lanes, key=lambda lane: lane.default_rank)


def _sung(score, sheet, **answers):
    """Each lane's sung pitches per bar, with the slots of each kind answered as given."""
    choices = first_option_choices(sheet)
    for slot in sheet.slots:
        if slot.kind in answers:
            choices[slot.slot_id] = answers[slot.kind]
    plan = assemble_plan(sheet, choices, score=score)
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    assert result["status"] == "ready", result
    lanes = []
    for target in result["targets"]:
        part = result["score"]["parts"][target["appended_part_ref"]["part_index"]]
        lanes.append({bar: [int(note["pitch_midi"]) for note in part["notes"]
                            if note["measure_number"] == bar and not note.get("is_rest")]
                      for bar in range(1, 6)})
    return plan, lanes


def test_one_line_bars_ask_the_lower_lane_once_and_its_directed_bars(tmp_path):
    score = _score(tmp_path)
    sheet = _sheet(score)
    top, lower = _lanes(sheet)
    asked = [(slot.kind, slot.start_measure, slot.lane_ids) for slot in sheet.slots
             if slot.kind in {"mode", "melody_source", "unison", "unison_default"}]
    # No lane is asked "sing or rest?" or "which line?"; the top lane is asked nothing.
    assert asked == [("unison_default", 1, (lower.lane_id,)), ("unison", 4, (lower.lane_id,))]
    default = next(slot for slot in sheet.slots if slot.kind == "unison_default")
    assert [option.option_id for option in default.options] == ["every_lane", "top_lane_only"]
    assert default.evidence == {"one_line_bars": 3, "divided_bars": 2, "direction_bars": [4]}


@pytest.mark.parametrize(("lane_answer", "bar_answer", "lower"), [
    ("every_lane", "every_lane", {1: [72], 2: [76], 3: [77], 4: [74], 5: [76]}),
    ("top_lane_only", "top_lane_only", {1: [72], 2: [], 3: [77], 4: [], 5: []}),
    ("top_lane_only", "every_lane", {1: [72], 2: [], 3: [77], 4: [74], 5: []}),
    ("every_lane", "top_lane_only", {1: [72], 2: [76], 3: [77], 4: [], 5: [76]}),
])
def test_the_lane_answer_decides_and_a_directed_bar_overrides_it(tmp_path, lane_answer, bar_answer, lower):
    score = _score(tmp_path)
    sheet = _sheet(score)
    plan, (top_sung, lower_sung) = _sung(score, sheet, unison_default=lane_answer, unison=bar_answer)
    assert top_sung == {1: [79], 2: [76], 3: [81], 4: [74], 5: [76]}  # the top lane always sings
    assert lower_sung == lower
    policies = [target.get("split_shared_note_policy") for target in plan["targets"]]
    assert policies[1] == ("assign_primary_only" if lane_answer == "top_lane_only" else None)


def test_every_lane_writes_the_first_option_plan(tmp_path):
    score = _score(tmp_path)
    sheet = _sheet(score)
    plan, _ = _sung(score, sheet, unison_default="every_lane", unison="every_lane")
    assert plan == assemble_plan(sheet, first_option_choices(sheet), score=score)


@pytest.mark.parametrize("lane_answer", ["every_lane", "top_lane_only"])
def test_a_note_written_in_both_voices_stays_in_both_lanes(tmp_path, lane_answer):
    score = _score(tmp_path, bar_two=_note("E", voice="1") + _BACKUP + _note("E", voice="2", lyric=None))
    sheet = _sheet(score)
    _, (top_sung, lower_sung) = _sung(score, sheet, unison_default=lane_answer)
    assert top_sung[2] == lower_sung[2] == [76]


def test_a_one_line_bar_with_chords_is_not_a_unison(tmp_path):
    score = _score(tmp_path, bar_two=_note("E", duration=4) + _note("C", chord=True, lyric=None))
    sheet = _sheet(score)
    assert not [slot for slot in sheet.slots if slot.kind in {"mode", "melody_source"} and slot.start_measure == 2]
    assert [slot.start_measure for slot in sheet.slots if slot.kind == "unison"] == [4]


def test_one_line_bars_in_a_separate_layout_are_unchanged(tmp_path):
    # Voice 1's own chord in bar 3 makes "separate voices" a possible reading.
    score = _score(tmp_path, bar_three=_chord("A", "F", 4) + _BACKUP + _rest(voice="2", duration=4))
    layout, = [slot for slot in _sheet(score).slots if slot.kind == "lane_layout"]
    separate = _sheet(score, pinned={layout.slot_id: "separate"})
    assert not [slot for slot in separate.slots if slot.kind in {"unison", "unison_default"}]
    assert not [slot_id for slot_id in separate.fixed if slot_id.endswith("|one_line")]


def _with_second_part(tmp_path, measures):
    """The target staff, plus a second part singing one note in every bar."""
    def part(part_id, bars):
        return f'<part id="{part_id}">' + "".join(
            f'<measure number="{number}">'
            + ('<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type>'
               '</time><clef><sign>G</sign><line>2</line></clef></attributes>' if number == 1 else '')
            + notes + '</measure>'
            for number, notes in enumerate(bars, start=1)) + '</part>'
    xml = ('<score-partwise version="3.1"><part-list>'
           '<score-part id="P1"><part-name>Solo</part-name></score-part>'
           '<score-part id="P2"><part-name>Other</part-name></score-part></part-list>'
           + part("P1", measures) + part("P2", [_note("B")] * len(measures)) + '</score-partwise>')
    return _parse_inline(tmp_path, xml)


def test_a_repair_reopens_the_line_choice_in_failing_bars(tmp_path):
    score = _with_second_part(tmp_path, [_voices("G", "C"), _note("E"), _voices("A", "F")])
    lower = _lanes(_sheet(score))[1]

    def line_question(sheet):
        return [len(slot.options) for slot in sheet.slots
                if slot.kind == "melody_source" and slot.lane_ids == (lower.lane_id,) and slot.start_measure == 2]

    assert line_question(_sheet(score)) == []  # code takes the staff's own line
    assert line_question(_sheet(score, reopen={lower.lane_id: [(2, 2)]})) == [2]


def _split_score(tmp_path):
    # One voice throughout: chords in bars 1 and 3, single notes in bars 2, 4 ("Solo") and 5.
    return _single_staff_score(tmp_path, [
        _chord("G", "C", 4),
        _note("E", voice="1"),
        _chord("A", "F", 4),
        _SOLO + _note("D", voice="1"),
        _note("E", voice="1"),
    ])


def test_single_notes_in_the_split_voice_ask_the_lower_lane_once(tmp_path):
    score = _split_score(tmp_path)
    sheet = _sheet(score)
    top, lower = _lanes(sheet)
    assert top.source_voice_part_id == lower.source_voice_part_id  # both lanes split one voice
    asked = [(slot.kind, slot.start_measure, slot.lane_ids) for slot in sheet.slots
             if slot.kind in {"mode", "melody_source", "unison", "unison_default"}]
    assert asked == [("unison_default", 1, (lower.lane_id,)), ("unison", 4, (lower.lane_id,))]


@pytest.mark.parametrize(("lane_answer", "bar_answer", "lower"), [
    ("every_lane", "every_lane", {1: [72], 2: [76], 3: [77], 4: [74], 5: [76]}),
    ("top_lane_only", "top_lane_only", {1: [72], 2: [], 3: [77], 4: [], 5: []}),
    ("top_lane_only", "every_lane", {1: [72], 2: [], 3: [77], 4: [74], 5: []}),
    ("every_lane", "top_lane_only", {1: [72], 2: [76], 3: [77], 4: [], 5: [76]}),
])
def test_in_the_split_voice_the_lane_answer_decides_through_the_lane_field(tmp_path, lane_answer, bar_answer, lower):
    score = _split_score(tmp_path)
    sheet = _sheet(score)
    plan, (top_sung, lower_sung) = _sung(score, sheet, unison_default=lane_answer, unison=bar_answer)
    assert top_sung == {1: [79], 2: [76], 3: [81], 4: [74], 5: [76]}
    assert lower_sung == lower  # and the pre-run check passed (_sung asserts the status)
    lower_target = plan["targets"][1]
    # The lane sings its own voice by rank everywhere; resting over its own notes is refused.
    assert {section["mode"] for section in lower_target["sections"]} == {"derive"}
    assert lower_target.get("split_shared_note_policy") == (
        "assign_primary_only" if lane_answer == "top_lane_only" else None)


def test_split_voice_every_lane_writes_the_first_option_plan(tmp_path):
    score = _split_score(tmp_path)
    sheet = _sheet(score)
    plan, _ = _sung(score, sheet, unison_default="every_lane", unison="every_lane")
    assert plan == assemble_plan(sheet, first_option_choices(sheet), score=score)


def test_a_bar_where_the_split_voice_is_silent_still_asks_whether_to_sing(tmp_path):
    score = _with_second_part(tmp_path, [_chord("G", "C", 4), _rest(voice="1", duration=4), _note("E", voice="1")])
    sheet = _sheet(score)
    lower = _lanes(sheet)[1]
    modes = {slot.start_measure: len(slot.options) for slot in sheet.slots
             if slot.kind == "mode" and slot.lane_ids == (lower.lane_id,)}
    assert modes == {2: 2}  # silent bar 2: sing or rest; single-note bar 3: not asked
