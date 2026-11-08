"""Offline decision-sheet skeleton tests with synthetic scores only."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.api import parse_score
from src.api import preprocess_assembler as assembler
from src.api import preprocess_sheet as sheet_module
from src.api.preprocess_sheet import (
    SLOT_KINDS, Lane, _rank_assignment_options, _shared_notes_in_scope,
    build_sheet, build_sheet_skeleton,
)
from src.musicxml.solfege import GENERATED_LYRIC_NAME
from src.api.voice_parts import (
    _analyze_part_voice_parts, _staff_scope_measure_max_simultaneous_notes,
    preprocess_voice_parts,
)


ROOT = Path(__file__).resolve().parents[1]
SATB = ROOT / "tests/fixtures/split/satb_two_bars.xml"


def _assert_contiguous_segments(sheet, score):
    part = score["parts"][sheet.target_part_index]
    notes = [note for note in part["notes"] if not note.get("is_rest")]
    start = min(int(note["measure_number"]) for note in notes)
    end = max(int(note["measure_number"]) for note in notes)
    for scope in {lane.scope_key for lane in sheet.lanes}:
        segments = [segment for segment in sheet.segments if segment.scope_key == scope]
        assert segments
        assert segments[0].start_measure == start
        assert segments[-1].end_measure == end
        assert all(
            left.end_measure + 1 == right.start_measure
            for left, right in zip(segments, segments[1:])
        )
        assert all(segment.start_measure <= segment.end_measure for segment in segments)


def _parse_inline(tmp_path, xml: str):
    path = tmp_path / "synthetic.xml"
    path.write_text(xml, encoding="utf-8")
    return parse_score(path, verse_number=1)


def _reading_score(*notes):
    """Small parsed-score-shaped fixture with two voices on one staff."""
    return {"parts": [{"part_id": "P1", "notes": list(notes)}]}


def _reading_note(voice, beat, lyric=None, *, syllabic=None, tie_type=None):
    note = {"voice": str(voice), "staff": "1", "measure_number": 1,
            "offset_beats": float(beat), "duration_beats": 1.0, "pitch_midi": 70 - int(voice),
            "is_rest": False, "lyric": lyric}
    if syllabic is not None:
        note["syllabic"] = syllabic
    if tie_type is not None:
        note["tie_type"] = tie_type
    return note


def test_staff_bar_kinds_clash_takes_precedence_over_independent():
    make = _reading_note
    one_a, one_b = make(1, 0, "la"), make(2, 0)
    independent_a, independent_b = make(1, 0, "la"), make(2, 2, "da")
    clash_a, clash_b, clash_c = make(1, 0, "la"), make(2, 0, "da"), make(2, 2, "do")
    for note in (independent_a, independent_b):
        note["measure_number"] = 2
    for note in (clash_a, clash_b, clash_c):
        note["measure_number"] = 3
    score = _reading_score(one_a, one_b, independent_a, independent_b,
                           clash_a, clash_b, clash_c)
    lines = sheet_module.staff_lyric_lines(score)
    assert sheet_module.staff_bar_kinds(score, lines, 0, "1") == {
        1: "one_text", 2: "independent", 3: "clash",
    }


def test_lyric_bar_cases_shared_missing_independent_and_conflicting():
    make = _reading_note
    scenarios = [
        ([make(1, 0, "one"), make(1, 2), make(2, 0), make(2, 2, "two")],
         "shared", ()),
        ([make(1, 0), make(1, 2), make(2, 1, "one"), make(2, 3, "two")],
         "no_word_for_lane", ()),
        ([make(1, 0, "one"), make(1, 2), make(2, 1, "two")],
         "ambiguous", ("independent_rhythm",)),
        ([make(1, 0, "one"), make(2, 0, "other")], "single", ()),
    ]
    for notes, expected, reasons in scenarios:
        score = _reading_score(*notes)
        lines = sheet_module.staff_lyric_lines(score)
        case = sheet_module.lyric_bar_cases(score, lines, 0, "1", "voice part 1", None)[1]
        assert (case.case, case.reasons) == (expected, reasons)


def test_lyric_bar_cases_broken_split_word_and_tied_hold():
    make = _reading_note
    score = _reading_score(make(1, 0, "start", syllabic="begin"), make(1, 2),
                           make(2, 0), make(2, 2, "other", syllabic="begin"))
    lines = sheet_module.staff_lyric_lines(score)
    case = sheet_module.lyric_bar_cases(score, lines, 0, "1", "voice part 1", None)[1]
    assert case.case == "ambiguous" and case.reasons == ("broken_split_word",)

    tied = make(1, 2, tie_type="stop")
    score = _reading_score(make(1, 0, "first"), tied, make(2, 0), make(2, 2, "second"))
    lines = sheet_module.staff_lyric_lines(score)
    assert sheet_module.read_lane_lyrics(lines[(0, "1")], [tied]) == ["+"]
    assert sheet_module.lyric_bar_cases(score, lines, 0, "1", "voice part 1", None)[1].case == "shared"

    tied["measure_number"] = 2
    tied["offset_beats"] = 4.0
    next_word = make(2, 4, "second")
    next_word["measure_number"] = 2
    score = _reading_score(make(1, 0, "first"), tied, next_word)
    lines = sheet_module.staff_lyric_lines(score)
    assert sheet_module.lyric_bar_cases(score, lines, 0, "1", "voice part 1", None)[2].case == "no_word_for_lane"


def test_tied_note_with_own_word_is_a_new_lyric_note():
    make = _reading_note
    tied_word = make(1, 2, "own", tie_type="stop")
    tied_blank = make(1, 3, tie_type="continue")
    score = _reading_score(make(1, 0, "first"), tied_word, tied_blank,
                           make(2, 0), make(2, 3, "other"))
    lines = sheet_module.staff_lyric_lines(score)
    line = lines[(0, "1")]
    assert sheet_module.read_lane_lyrics(line, [tied_word, tied_blank]) == ["own", "+"]
    cases = sheet_module.lyric_bar_cases(score, lines, 0, "1", "voice part 1", None)
    assert cases[1].case == "shared"


def _two_staff_score(tmp_path):
    # One MusicXML part, four simultaneous voices over two staff scopes.
    notes = []
    for voice, step, octave, staff in (
        ("1", "G", 5, 1), ("2", "E", 5, 1),
        ("3", "B", 3, 2), ("4", "G", 3, 2),
    ):
        if notes:
            notes.append("<backup><duration>4</duration></backup>")
        notes.append(
            f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            f"<duration>4</duration><voice>{voice}</voice><type>whole</type>"
            f"<staff>{staff}</staff>"
            + ("<lyric><text>word</text></lyric>" if voice == "1" else "")
            + "</note>"
        )
    xml = (
        '<score-partwise version="3.1"><part-list><score-part id="P1">'
        '<part-name>Choir</part-name></score-part></part-list><part id="P1">'
        '<measure number="1"><attributes><divisions>1</divisions><staves>2</staves>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef number="1"><sign>G</sign><line>2</line></clef>'
        '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>'
        + "".join(notes) + "</measure></part></score-partwise>"
    )
    return _parse_inline(tmp_path, xml)


def _chord_score(tmp_path):
    xml = """<score-partwise version="3.1">
<part-list><score-part id="P1"><part-name>Solo</part-name></score-part></part-list>
<part id="P1">
  <measure number="1"><attributes><divisions>1</divisions>
    <time><beats>4</beats><beat-type>4</beat-type></time>
    <clef><sign>G</sign><line>2</line></clef></attributes>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration>
      <voice>1</voice><type>whole</type><lyric><text>word</text></lyric></note>
    <note><chord/><pitch><step>E</step><octave>5</octave></pitch><duration>4</duration>
      <voice>1</voice><type>whole</type></note>
  </measure>
  <measure number="2">
    <note><pitch><step>D</step><octave>5</octave></pitch><duration>4</duration>
      <voice>1</voice><type>whole</type><lyric><text>next</text></lyric></note>
  </measure>
</part></score-partwise>"""
    return _parse_inline(tmp_path, xml)


def _single_staff_score(tmp_path, measures, *, part_name="Solo"):
    xml = (
        '<score-partwise version="3.1"><part-list><score-part id="P1">'
        f'<part-name>{part_name}</part-name></score-part></part-list><part id="P1">'
        + "".join(
            f'<measure number="{number}">'
            + ('<attributes><divisions>1</divisions><time><beats>4</beats>'
               '<beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line>'
               '</clef></attributes>' if number == 1 else '')
            + notes + '</measure>'
            for number, notes in enumerate(measures, start=1)
        )
        + '</part></score-partwise>'
    )
    return _parse_inline(tmp_path, xml)


def _note(step, *, voice=None, chord=False, duration=4, lyric="word"):
    return (
        '<note>' + ('<chord/>' if chord else '')
        + f'<pitch><step>{step}</step><octave>5</octave></pitch>'
        + f'<duration>{duration}</duration>'
        + (f'<voice>{voice}</voice>' if voice is not None else '')
        + f'<type>{"half" if duration == 2 else "whole"}</type>'
        + (f'<lyric><text>{lyric}</text></lyric>' if lyric is not None else '')
        + '</note>'
    )


def _rest(*, voice, duration=2):
    return (
        '<note><rest/>' + f'<duration>{duration}</duration><voice>{voice}</voice>'
        + f'<type>{"half" if duration == 2 else "whole"}</type></note>'
    )


def _no_word_two_part_score(tmp_path):
    target = (
        '<part id="P1"><measure number="1"><attributes><divisions>1</divisions>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>'
        + _note("G", voice="1", duration=2, lyric=None) + _rest(voice="1")
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="2") + _note("E", voice="2", duration=2, lyric=None)
        + '</measure><measure number="2">'
        + _rest(voice="2", duration=4) + '<backup><duration>4</duration></backup>'
        + _note("A", voice="1", lyric=None) + '</measure></part>'
    )
    guide = (
        '<part id="P2"><measure number="1"><attributes><divisions>1</divisions>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>'
        + _note("D", lyric=None) + '</measure><measure number="2">'
        + _note("F", lyric=None) + '</measure></part>'
    )
    xml = (
        '<score-partwise version="3.1"><part-list>'
        '<score-part id="P1"><part-name>Choir</part-name></score-part>'
        '<score-part id="P2"><part-name>Guide</part-name></score-part>'
        '<score-part id="P3"><part-name>Donor</part-name></score-part>'
        '</part-list>' + target + guide
        + '<part id="P3"><measure number="3">'
        + _note("B", lyric="word") + '</measure></part>'
        + '</score-partwise>'
    )
    return _parse_inline(tmp_path, xml)


def _unnumbered_chord_with_opposite_voice_heights(tmp_path):
    first = _note("D", lyric="sing") + _note("F", chord=True, lyric=None)
    second = (_note("C", voice="1", lyric="low")
              + '<backup><duration>4</duration></backup>'
              + _note("G", voice="2", lyric="high"))
    return _single_staff_score(tmp_path, [first, second])


def _two_chord_voices_with_silent_lanes(tmp_path):
    first = (_note("C", voice="1") + _note("E", voice="1", chord=True, lyric=None)
             + '<backup><duration>4</duration></backup>'
             + _note("G", voice="2") + _note("B", voice="2", chord=True, lyric=None))
    second = (_note("D", voice="3") + '<backup><duration>4</duration></backup>'
              + _note("A", voice="4"))
    return _single_staff_score(tmp_path, [first, second])


def _four_note_chord_with_native_lanes(tmp_path):
    first = (
        _note("C", voice="1", duration=2)
        + "".join(_note(step, voice="1", chord=True, duration=2, lyric=None)
                  for step in "EGB")
        + _rest(voice="1", duration=2)
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="2", duration=2) + _note("D", voice="2", duration=2)
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="3", duration=2) + _note("F", voice="3", duration=2)
    )
    second = (_note("A", voice="3") + '<backup><duration>4</duration></backup>'
              + _note("B", voice="4"))
    return _single_staff_score(tmp_path, [first, second])


def _too_many_chord_claims(tmp_path):
    first = (
        _note("C", voice="1", duration=2)
        + _note("E", voice="1", chord=True, duration=2, lyric=None)
        + _rest(voice="1", duration=2)
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="2", duration=2)
        + _note("G", voice="2", duration=2)
        + _note("B", voice="2", chord=True, duration=2, lyric=None)
    )
    return _single_staff_score(tmp_path, [first])


def test_target_resolution_and_intent_flag():
    score = parse_score(SATB, verse_number=1)
    original = deepcopy(score)

    by_id = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1", "verse_number": " 2 "}})
    assert by_id.target_part_index == 0
    assert by_id.verse_number == "2"
    assert not by_id.issues
    assert len(by_id.lanes) == 2
    assert [(lane.source_voice_part_id, lane.default_rank) for lane in by_id.lanes] == [
        ("voice part 1", 0), ("voice part 2", 1)
    ]
    assert [(segment.start_measure, segment.end_measure) for segment in by_id.segments] == [
        (1, 1), (2, 2)
    ]
    assert [segment.signature.annotation for segment in by_id.segments] == ["Moderato", None]
    _assert_contiguous_segments(by_id, score)

    single = deepcopy(score)
    single["parts"] = single["parts"][:1]
    by_name = build_sheet_skeleton(single, {"target": {"part_id": "cHoIr"}, "request": {"verse_number": 3}})
    assert by_name.target_part_index == 0
    assert by_name.verse_number == "3"

    unresolved = build_sheet_skeleton(score, {"target": {"part_id": "Unknown"}})
    ambiguous = build_sheet_skeleton(score, {"target": {"part_id": "choir"}})
    assert unresolved.target_part_index is None and unresolved.lanes == []
    assert ambiguous.target_part_index is None and ambiguous.lanes == []
    assert unresolved.issues == ambiguous.issues == ["target_unresolved"]

    with_intent = build_sheet_skeleton(score, {
        "target": {"part_id": "P1-Staff1"}, "other_instruction": "keep the soprano"
    })
    assert with_intent.issues == ["intent_requires_llm"]
    assert with_intent.lanes == by_id.lanes
    assert score == original


def test_two_staff_scopes_have_complete_lane_density(tmp_path):
    score = _two_staff_score(tmp_path)
    original = deepcopy(score)
    # parse_score exposes each MusicXML staff as a separate part.
    assert len(score["parts"]) == 2
    for part_index, part in enumerate(score["parts"]):
        sheet = build_sheet_skeleton(score, {"target": {"part_id": part["part_id"]}})
        assert sheet.target_part_index == part_index
        density = _staff_scope_measure_max_simultaneous_notes(score, part_index=part_index, staff_scope="1")
        assert len(sheet.lanes) == max(density.values()) == 2
        _assert_contiguous_segments(sheet, score)
    assert score == original

    # The sheet also keeps scopes separate if parser facts arrive in one part.
    combined = deepcopy(score)
    one_part = deepcopy(combined["parts"][0])
    one_part["part_id"] = "P1-Combined"
    one_part["notes"] = [
        {**note, "staff": str(staff)}
        for staff, part in enumerate(combined["parts"], start=1)
        for note in part["notes"]
    ]
    combined["parts"] = [one_part]
    sheet = build_sheet_skeleton(combined, {"target": {"part_id": "P1-Combined"}})
    assert {lane.scope_key for lane in sheet.lanes} == {"1", "2"}
    assert [sum(lane.scope_key == scope for lane in sheet.lanes) for scope in ("1", "2")] == [2, 2]
    _assert_contiguous_segments(sheet, combined)


def test_same_voice_chord_adds_second_lane_and_density_boundary(tmp_path):
    score = _chord_score(tmp_path)
    original = deepcopy(score)
    sheet = build_sheet_skeleton(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(sheet.lanes) == 2
    assert [lane.source_voice_part_id for lane in sheet.lanes] == ["voice part 1"] * 2
    assert [lane.default_rank for lane in sheet.lanes] == [0, 1]
    assert [segment.signature.density for segment in sheet.segments] == [2, 1]
    assert [(segment.start_measure, segment.end_measure) for segment in sheet.segments] == [(1, 1), (2, 2)]
    assert sheet.segments[0].signature.lanes[1][1]
    assert not sheet.segments[1].signature.lanes[1][1]
    _assert_contiguous_segments(sheet, score)
    assert score == original


def test_turn_taking_real_voices_exclude_default_lane_and_assemble(tmp_path):
    score = _single_staff_score(tmp_path, [
        _note("C"),
        _note("G", voice="1", duration=2) + _rest(voice="1")
        + '<backup><duration>4</duration></backup>'
        + _rest(voice="2") + _note("E", voice="2", duration=2),
    ])
    voice_parts = _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]
    assert {str(vp["source_voice_id"]) for vp in voice_parts} == {"1", "2", "_default"}
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not sheet.issues
    assert [lane.source_voice_part_id for lane in sheet.lanes] == [
        "voice part 1", "voice part 2",
    ]
    assert [lane.default_rank for lane in sheet.lanes] == [0, 1]
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)["targets"]) == 2


def test_real_voices_plus_three_note_chord_get_one_ranked_extra(tmp_path):
    score = _single_staff_score(tmp_path, [
        _note("C", voice="1", duration=2)
        + _note("E", voice="1", chord=True, duration=2)
        + _note("G", voice="1", chord=True, duration=2)
        + _rest(voice="1") + '<backup><duration>4</duration></backup>'
        + _rest(voice="2") + _note("D", voice="2", duration=2),
    ])
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not sheet.issues
    assert [lane.source_voice_part_id for lane in sheet.lanes] == [
        "voice part 1", "voice part 2", "voice part 1",
    ]
    assert [lane.default_rank for lane in sheet.lanes] == [0, 1, 2]
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)["targets"]) == 3


def test_default_only_scope_keeps_default_lane_and_assembles(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C"), _note("D")])
    voice_parts = _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]
    assert [str(vp["source_voice_id"]) for vp in voice_parts] == ["_default"]
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not sheet.issues
    assert len(sheet.lanes) == 1
    assert sheet.lanes[0].source_voice_part_id == "voice part 1"
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)["targets"]) == 1


def test_target_part_id_resolves_double_escaped_ampersand(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C")], part_name="Soprano &amp;amp; Alto")
    parsed_id = score["parts"][0]["part_id"]
    assert parsed_id == "Soprano &amp; Alto"
    sheet = build_sheet_skeleton(score, {"target": {"part_id": "Soprano & Alto"}})
    assert sheet.target_part_index == 0
    assert not sheet.issues
    assert sheet.lanes
    assert all(lane.part_id == parsed_id and lane.lane_id.startswith(parsed_id + "|")
               for lane in sheet.lanes)


def test_ambiguous_unescaped_part_ids_leave_target_unresolved(tmp_path):
    xml = (
        '<score-partwise version="3.1"><part-list>'
        '<score-part id="P1"><part-name>Soprano &amp;amp; Alto</part-name></score-part>'
        '<score-part id="P2"><part-name>Soprano &amp; Alto</part-name></score-part>'
        '</part-list>'
        + ''.join(
            f'<part id="P{number}"><measure number="1"><attributes><divisions>1</divisions>'
            '<time><beats>4</beats><beat-type>4</beat-type></time>'
            '<clef><sign>G</sign><line>2</line></clef></attributes>'
            + _note("C") + '</measure></part>'
            for number in (1, 2)
        )
        + '</score-partwise>'
    )
    score = _parse_inline(tmp_path, xml)
    assert [part["part_id"] for part in score["parts"]] == [
        "Soprano &amp; Alto", "Soprano & Alto",
    ]
    sheet = build_sheet_skeleton(score, {"target": {"part_id": "Soprano &amp;amp; Alto"}})
    assert sheet.target_part_index is None
    assert sheet.lanes == []
    assert sheet.issues == ["target_unresolved"]
    exact = build_sheet_skeleton(score, {"target": {"part_id": "Soprano & Alto"}})
    assert exact.target_part_index == 1


def test_lyric_kind_change_does_not_start_new_segment():
    score = parse_score(SATB, verse_number=1)
    for note in score["parts"][0]["notes"]:
        if note.get("measure_number") == 2 and note.get("voice") == "1":
            note["lyric"] = "+"
    original = deepcopy(score)
    sheet = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1"}})
    unchanged = build_sheet_skeleton(parse_score(SATB, verse_number=1), {"target": {"part_id": "P1-Staff1"}})
    # Only density, native notes and annotations cut segments (bar 1 has a tempo mark).
    assert sheet.segments == unchanged.segments
    assert all(len(fact) == 2 for segment in sheet.segments for fact in segment.signature.lanes)
    _assert_contiguous_segments(sheet, score)
    assert score == original


def test_real_parsed_annotations_do_not_leak_between_staffs():
    score = parse_score(SATB, verse_number=1)
    original = deepcopy(score)
    staff1 = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1"}})
    staff2 = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff2"}})
    assert [segment.signature.annotation for segment in staff1.segments] == ["Moderato", None]
    assert [(segment.start_measure, segment.end_measure) for segment in staff1.segments] == [
        (1, 1), (2, 2)
    ]
    assert len(staff2.segments) == 1
    assert (staff2.segments[0].start_measure, staff2.segments[0].end_measure) == (1, 2)
    assert staff2.segments[0].signature.annotation is None
    _assert_contiguous_segments(staff1, score)
    _assert_contiguous_segments(staff2, score)
    assert score == original


def test_non_numeric_raw_annotation_measure_is_skipped():
    score = parse_score(SATB, verse_number=1)
    score["voice_part_signals"]["measure_annotations"][0]["measures"].append({
        "measure_number": "12a",
        "direction_entries": [{"staff": "1", "text": "div."}],
    })
    original = deepcopy(score)
    sheet = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1"}})
    assert [segment.signature.annotation for segment in sheet.segments] == ["Moderato", None]
    assert score == original


def test_existing_visible_lane_uses_update_mode(tmp_path):
    score = _chord_score(tmp_path)
    original_part_id = score["parts"][0]["part_id"]
    derived = deepcopy(score["parts"][0])
    derived["part_id"] = "P_DERIVED_TEST"
    derived["part_name"] = "Solo (Derived)"
    score["parts"].append(derived)
    score["voice_part_transforms"] = {
        "synthetic": {
            "appended_part_ref": {"part_index": 1},
            "derived_lane": {
                "source": {"part_index": 0, "voice_part_id": "voice part 1"},
                "slot": 2,
                "derived_lane_id": "dl:test:2",
            },
        }
    }
    original = deepcopy(score)
    sheet = build_sheet_skeleton(score, {"target": {"part_id": original_part_id}})
    assert [lane.output_mode for lane in sheet.lanes] == [
        "append_new_derived_lane", "update_existing_derived_lane"
    ]
    assert [lane.existing_derived_lane_id for lane in sheet.lanes] == [None, "dl:test:2"]
    assert score == original


def _all_options(sheet):
    return [option for slot in sheet.slots for option in slot.options] + list(sheet.fixed.values())


def _lane_segment_options(sheet, lane, segment, kind):
    slot_id = f"{lane.lane_id}|{segment.start_measure}-{segment.end_measure}|{kind}"
    if slot_id in sheet.fixed:
        return (sheet.fixed[slot_id],)
    return next((slot.options for slot in sheet.slots if slot.slot_id == slot_id), ())


def test_native_lyricless_voice_uses_eligible_voice_on_same_staff(tmp_path):
    # Bar 2 prints voice 1's rest: a bar written only in voice 2 is a shared line.
    score = _single_staff_score(tmp_path, [
        _note("G", voice="1", lyric="word")
        + '<backup><duration>4</duration></backup>'
        + _note("E", voice="2", lyric=None),
        _rest(voice="1", duration=4)
        + '<backup><duration>4</duration></backup>'
        + _note("C", voice="2", lyric=None),
    ])
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    lane = next(lane for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    segment = next(segment for segment in sheet.segments if segment.start_measure == 2)
    assert next(native for lane_id, native in segment.signature.lanes
                if lane_id == lane.lane_id)
    # The staff's line is read by code: no lyric menu for the lyric-less voice.
    assert not _lyric_slot_kinds(sheet, lane)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    target = plan["targets"][sheet.lanes.index(lane)]
    assert {section["lyric_source"]["voice_part_id"] for section in target["sections"]} == {"voice part 2"}
    assert all(section["lyric_source_line"] == {"staff": None, "voice_part_id": None}
               for section in target["sections"])
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert result.get("action") != "invalid_section_source"
    assert score == original
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert json.dumps(asdict(sheet), sort_keys=True)


def test_score_without_eligible_lyrics_stops_before_splitting(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", lyric=None)])
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    assert sheet.issues == ["score_has_no_lyrics"]
    assert sheet.target_part_index == 0
    assert sheet.lanes == sheet.segments == sheet.slots == []
    assert sheet.fixed == {}
    assert score == original
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert json.dumps(asdict(sheet), sort_keys=True)

    generated = _single_staff_score(tmp_path, [_note("D", lyric="do")])
    for note in generated["parts"][0]["notes"]:
        note["lyric_name"] = GENERATED_LYRIC_NAME
    generated_original = deepcopy(generated)
    generated_sheet = build_sheet(generated, context)
    assert generated_sheet.issues == ["score_has_no_lyrics"]
    assert generated_sheet.lanes == generated_sheet.segments == generated_sheet.slots == []
    assert generated == generated_original


def test_no_word_lyrics_on_native_notes_keep_own_source(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C", lyric=None), _note("D", lyric="word")])
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    assert not sheet.issues
    assert not any(slot.kind == "freeform" for slot in sheet.slots)
    lane = sheet.lanes[0]
    assert not _lyric_slot_kinds(sheet, lane)
    assert json.dumps(asdict(sheet), sort_keys=True)
    assert asdict(sheet) == asdict(build_sheet(score, context))
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert result.get("action") != "invalid_section_source"
    assert not [finding for finding in result.get("lint_findings", [])
                if finding.get("rule") == "melody_source_requires_lyric_source"
                and finding.get("rule_severity") == "P0"]
    assert score == original


def test_no_word_lyrics_on_idle_ranked_lane_keep_own_source(tmp_path):
    score = _single_staff_score(tmp_path, [
        _note("C", voice="1", lyric="word")
        + _note("E", voice="1", chord=True, lyric=None),
        _note("D", voice="1", lyric=None),
    ])
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not any(slot.kind == "freeform" for slot in sheet.slots)
    lane = sheet.lanes[1]
    segment = next(segment for segment in sheet.segments if segment.start_measure == 2)
    assert not next(native for lane_id, native in segment.signature.lanes
                    if lane_id == lane.lane_id)
    assert not _lyric_slot_kinds(sheet, lane)
    assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)


def test_no_word_lyrics_on_copied_voice_use_eligible_donor(tmp_path):
    score = _no_word_two_part_score(tmp_path)
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    assert not sheet.issues
    assert not any(slot.kind == "freeform" and
                   slot.evidence.get("reason") == "derive_without_lyric_source"
                   for slot in sheet.slots)
    lane = next(lane for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    segment = next(segment for segment in sheet.segments if segment.start_measure == 2)
    assert not next(native for lane_id, native in segment.signature.lanes
                    if lane_id == lane.lane_id)
    melody = _lane_segment_options(sheet, lane, segment, "melody_source")
    lyric = _lane_segment_options(sheet, lane, segment, "lyric_source")
    assert len(melody) >= 2
    assert len(lyric) == 1
    assert lyric[0].fields["lyric_source"] == {"part_index": 2, "voice_part_id": "voice part 1"}
    assert lyric[0].evidence == {"reason": "no_lyrics_in_source_voice"}
    assert all(option.flags == () for option in lyric)
    assert json.dumps(asdict(sheet), sort_keys=True)
    assert asdict(sheet) == asdict(build_sheet(score, context))
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert result.get("action") != "invalid_section_source"
    assert not [finding for finding in result.get("lint_findings", [])
                if finding.get("rule") == "melody_source_requires_lyric_source"
                and finding.get("rule_severity") == "P0"]
    assert score == original


def test_no_word_lyric_source_can_be_absent_from_candidates(tmp_path):
    score = _no_word_two_part_score(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not sheet.issues
    assert not any(slot.kind == "freeform" for slot in sheet.slots)
    lane = next(lane for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    segment = sheet.segments[0]
    assert all(source["part_index"] != 2 for source in sheet_module._source_candidates(
        score, segment, sheet_module._derived_part_indices(score)
    ))
    assert _lane_segment_options(sheet, lane, segment, "lyric_source")[0].fields == {
        "lyric_source": {"part_index": 2, "voice_part_id": "voice part 1"},
    }


def test_sheet_excludes_rest_on_native_notes_and_is_pure_json():
    score = parse_score(SATB, verse_number=1)
    original = deepcopy(score)
    context = {"target": {"part_id": "P1-Staff1"}}
    sheet = build_sheet(score, context)
    assert set(slot.kind for slot in sheet.slots).issubset(SLOT_KINDS)
    native_by_lane_segment = {
        (lane_id, segment.start_measure, segment.end_measure)
        for segment in sheet.segments for lane_id, native in segment.signature.lanes if native
    }
    for slot in sheet.slots:
        if slot.kind == "mode" and (
            slot.lane_ids[0], slot.start_measure, slot.end_measure
        ) in native_by_lane_segment:
            assert all(option.fields["mode"] != "rest" for option in slot.options)
    for slot_id, option in sheet.fixed.items():
        if slot_id.endswith("|mode"):
            assert option.fields["mode"] != "rest"
    assert json.dumps(asdict(sheet), sort_keys=True)
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert score == original


def test_chord_voice_assignment_and_fixed_single_options(tmp_path):
    score = _chord_score(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    rank_slots = [slot for slot in sheet.slots if slot.kind == "voice_assignment"]
    assert len(rank_slots) == 1
    assert len(rank_slots[0].lane_ids) == 2
    mappings = [option.fields["rank_index_by_lane"] for option in rank_slots[0].options]
    assert list(mappings[0].values()) == [0, 1]
    assert all(len(set(mapping.values())) == 2 for mapping in mappings)
    assert len({tuple(mapping.values()) for mapping in mappings}) == len(mappings) == 2
    nonzero = [
        Lane("a", "P1", 0, "1", "voice part 2", 1, "append_new_derived_lane"),
        Lane("b", "P1", 0, "1", "voice part 2", 2, "append_new_derived_lane"),
    ]
    assert list(_rank_assignment_options([nonzero])[0].fields["rank_index_by_lane"].values()) == [0, 1]
    # Bar 2's lone note is sung by both lanes, so no fallback is asked.
    assert not any(slot.kind == "rank_fallback" for slot in sheet.slots)
    assert sheet.fixed
    assert all(not slot.options or len(slot.options) > 1 for slot in sheet.slots)
    # The one-staff line is read by code, so no lane gets a lyric menu.
    assert not any(_lyric_slot_kinds(sheet, lane) for lane in sheet.lanes)


def test_unnumbered_chord_uses_pitch_order_not_voice_number(tmp_path):
    score = _unnumbered_chord_with_opposite_voice_heights(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    # Pitch order is the only reading here, so code answers it instead of asking.
    assert not [slot for slot in sheet.slots if slot.kind == "voice_assignment"]
    assignment_id, = [slot_id for slot_id in sheet.fixed if slot_id.endswith("|voice_assignment")]
    assert assignment_id.endswith("|1-1|voice_assignment")
    assert not [slot for slot in sheet.slots if slot.kind == "melody_source"
                and slot.start_measure == 1]
    first = sheet.fixed[assignment_id]
    ranks = first.fields["rank_index_by_lane"]
    raw_voice = {vp["voice_part_id"]: vp["source_voice_id"]
                 for vp in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]}
    high_lane = next(lane for lane in sheet.lanes
                     if raw_voice[lane.source_voice_part_id] == "2")
    low_lane = next(lane for lane in sheet.lanes
                    if raw_voice[lane.source_voice_part_id] == "1")
    assert ranks[high_lane.lane_id] == 0
    assert ranks[low_lane.lane_id] == 1
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    sections = [next(section for section in target["sections"]
                     if section["start_measure"] <= 1 <= section["end_measure"])
                for target in plan["targets"]]
    assert all(section["decision_type"] == "SPLIT_CHORDS_SELECT_NOTES"
               and section["method"] == "ranked" for section in sections)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert result["status"] == "ready"
    pitch_by_lane = {}
    for lane, target in zip(sheet.lanes, result["targets"]):
        part = result["score"]["parts"][target["appended_part_ref"]["part_index"]]
        pitch_by_lane[lane.lane_id] = {
            note["pitch_midi"] for note in part["notes"]
            if note["measure_number"] == 1 and not note["is_rest"]
        }
    assert pitch_by_lane[high_lane.lane_id] == {77.0}
    assert pitch_by_lane[low_lane.lane_id] == {74.0}


def test_two_chord_sources_get_disjoint_covering_lanes(tmp_path):
    score = _two_chord_voices_with_silent_lanes(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    slots = [slot for slot in sheet.slots if slot.kind == "voice_assignment"]
    assert len(slots) == 1 and len(slots[0].lane_ids) == 4
    first = slots[0].options[0]
    raw_voice = {vp["voice_part_id"]: vp["source_voice_id"]
                 for vp in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]}
    sources = [raw_voice[source["voice_part_id"]]
               for source in first.fields["melody_source_by_lane"].values()]
    assert sources.count("1") == sources.count("2") == 2
    assert sorted(first.fields["rank_index_by_lane"].values()) == [0, 0, 1, 1]
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings") or []
                if finding.get("rule") in {
                    "complete_split_source_underclaimed", "complete_split_duplicate_rank",
                }]


def test_leftover_silent_lane_joins_chord_with_distinct_rank_and_fallback(tmp_path):
    second = '<backup><duration>4</duration></backup>'.join(
        _note(step, voice=voice) for step, voice in (("G", "2"), ("B", "3"), ("D", "4"))
    )
    score = _single_staff_score(tmp_path, [
        _note("C", voice="1") + _note("E", voice="1", chord=True, lyric=None), second,
    ])
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    # A repair of these bars sees every option.
    menus = build_sheet(score, context, reopen={lane.lane_id: [(1, 2)] for lane in sheet.lanes})
    slot = next(slot for slot in menus.slots if slot.kind == "voice_assignment")
    assert len(sheet.lanes) == 3 and len(slot.options) == 6
    assert slot.evidence["need"] == {"voice part 4": 2}
    assert len(set(source["voice_part_id"] for source in
                   slot.options[0].fields["melody_source_by_lane"].values())) == 1
    assert sorted(slot.options[0].fields["rank_index_by_lane"].values()) == [0, 1, 2]
    assert sum(slot.kind == "rank_fallback" for slot in menus.slots) == 3
    # The first build keeps pitch order itself, and asks the fallback only for the
    # lane whose rank the two-note chord cannot fill.
    assert not [slot for slot in sheet.slots if slot.kind == "voice_assignment"]
    assignment = next(option for slot_id, option in sheet.fixed.items()
                      if slot_id.endswith("|voice_assignment"))
    assert assignment.option_id == slot.options[0].option_id
    fallback, = [slot for slot in sheet.slots if slot.kind == "rank_fallback"]
    assert assignment.fields["rank_index_by_lane"][fallback.lane_ids[0]] == 2
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(target["sections"][0]["rank_fallback"] == "greedy" for target in plan["targets"])
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings") or []
                if finding.get("rule_severity") == "P0"]
    assert score == original
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert json.dumps(asdict(sheet), sort_keys=True)


_BACKUP = '<backup><duration>4</duration></backup>'


def _voices(high, low):
    """A bar of voice 1 above voice 2: it gives the staff its two lanes and their heights."""
    return _note(high, voice="1") + _BACKUP + _note(low, voice="2", lyric=None)


def _three_voices(high, middle, low):
    """A bar of voices 1 to 3, top down: it gives the staff three lanes and their heights."""
    return (_note(high, voice="1") + _BACKUP + _note(middle, voice="2", lyric=None)
            + _BACKUP + _note(low, voice="3", lyric=None))


def _chord(top, low, duration=2):
    return (_note(top, voice="1", duration=duration)
            + _note(low, voice="1", chord=True, duration=duration, lyric=None))


def _triad(top, middle, low, duration=2):
    return _chord(top, middle, duration) + _note(low, voice="1", chord=True, duration=duration, lyric=None)


def _thinning_score(tmp_path, first_bar, *later_bars):
    """Bar 2 holds a chord, then one note for two beats; bar 3 a chord again."""
    return _single_staff_score(tmp_path, [
        first_bar, _chord("E", "C") + _note("D", voice="1", duration=2), *later_bars,
    ])


def _asked(sheet, kind):
    return [(slot.start_measure, slot.end_measure, slot.lane_ids) for slot in sheet.slots
            if slot.kind == kind]


def test_voice_assignment_is_asked_only_when_the_voices_cross_beside_it(tmp_path):
    def sheet_for(name, first_bar):
        (tmp_path / name).mkdir()
        score = _thinning_score(tmp_path / name, first_bar, _chord("E", "C", 4),
                                _voices("B", "D"), _voices("A", "E"))
        return build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})

    clear = sheet_for("clear", _voices("G", "C"))
    assert not _asked(clear, "voice_assignment")
    assert [option.option_id for slot_id, option in clear.fixed.items()
            if slot_id.endswith("|2-3|voice_assignment")] == ["va:0"]
    # Voice 2 sings above voice 1 in the bar before the chords: they may cross there too.
    crossed = sheet_for("crossed", _voices("C", "G"))
    assert [(start, end) for start, end, _ in _asked(crossed, "voice_assignment")] == [(2, 3)]


_SOLO = "<direction><direction-type><words>Solo</words></direction-type></direction>"


@pytest.mark.parametrize(("bar_two", "bar_three", "asked"), [
    (_triad("G", "E", "C") + _chord("E", "C"), _triad("G", "E", "C", 4), []),               # a brief thinning
    (_SOLO + _triad("G", "E", "C") + _chord("E", "C"), _triad("G", "E", "C", 4), [(2, 2)]),  # a marking
    (_triad("G", "E", "C") + _chord("E", "C") + _BACKUP + _rest(voice="3", duration=4),
     _triad("G", "E", "C", 4), [(2, 2)]),                                                    # a written rest
    (_triad("G", "E", "C") + _chord("E", "C"), _chord("F", "D") + _triad("G", "E", "C"), [(2, 3)]),  # a whole bar
    (_SOLO + _triad("G", "E", "C") + _note("D", voice="1", duration=2),
     _triad("G", "E", "C", 4), []),                                                          # a lone note
])
def test_rank_fallback_is_asked_only_when_the_score_may_stop_the_lane(tmp_path, bar_two, bar_three, asked):
    score = _single_staff_score(tmp_path, [_three_voices("G", "D", "C"), bar_two, bar_three])
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not _asked(sheet, "voice_assignment")
    lowest = max(sheet.lanes, key=lambda lane: lane.default_rank)
    # Only the third lane can find a chord without its note; every lane sings a lone note.
    assert _asked(sheet, "rank_fallback") == [(start, end, (lowest.lane_id,)) for start, end in asked]


def test_four_note_chord_moves_only_needed_native_lanes(tmp_path):
    score = _four_note_chord_with_native_lanes(tmp_path)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    menus = build_sheet(score, context, reopen={lane.lane_id: [(1, 1)] for lane in sheet.lanes})
    slot = next(slot for slot in menus.slots if slot.kind == "voice_assignment")
    assert len(sheet.lanes) == 4
    assert len(slot.options) == 24
    assert all("P1 drops_native_notes" in option.flags for option in slot.options)
    # Every option carries the same flag, so it tells them apart from nothing: code keeps pitch order.
    assert not [slot for slot in sheet.slots if slot.kind == "voice_assignment"]
    raw_voice = {vp["voice_part_id"]: vp["source_voice_id"]
                 for vp in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]}
    assert all(set(raw_voice[source["voice_part_id"]] for source in
                   option.fields["melody_source_by_lane"].values()) == {"1"}
               for option in slot.options)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings") or []
                if finding.get("rule_severity") == "P0"]


def test_chord_claims_exceed_available_lanes(tmp_path):
    score = _too_many_chord_claims(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(sheet.lanes) == 2
    freeform, = [slot for slot in sheet.slots if slot.kind == "freeform"]
    assert freeform.evidence["reason"] == "chord_claims_exceed_lanes"
    assert [slot.kind for slot in sheet.slots if slot.kind != "freeform"] == ["lane_layout"]


def test_first_option_never_simple_copies_a_chord_source(tmp_path):
    scores = [
        parse_score(SATB, verse_number=1), _chord_score(tmp_path),
        _two_staff_score(tmp_path), _unnumbered_chord_with_opposite_voice_heights(tmp_path),
        _two_chord_voices_with_silent_lanes(tmp_path), _four_note_chord_with_native_lanes(tmp_path),
    ]
    for score in scores:
        sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
        plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
        for target in plan["targets"]:
            for section in target["sections"]:
                if section["decision_type"] not in {"COPY_UNISON_SECTION", "EXTRACT_FROM_VOICE"}:
                    continue
                source = section["melody_source"]
                part_index = next(index for index, part in enumerate(score["parts"])
                                  if part["part_id"] == source["part_id"])
                part = score["parts"][part_index]
                voice = next(vp["source_voice_id"]
                             for vp in _analyze_part_voice_parts(part, part_index)["voice_parts"]
                             if vp["voice_part_id"] == source["voice_part_id"])
                counts = {}
                for note in part["notes"]:
                    if (note.get("is_rest") or note.get("voice") != voice
                            or not section["start_measure"] <= note["measure_number"] <= section["end_measure"]):
                        continue
                    key = (note["measure_number"], round(note["offset_beats"], 6))
                    counts[key] = counts.get(key, 0) + 1
                assert max(counts.values(), default=0) < 2


def _with_wordless_part(score):
    """Append a staff with no words, so melody options read from a staff without a line."""
    wordless = deepcopy(score["parts"][0])
    wordless["part_id"] = "P_WORDLESS"
    for note in wordless["notes"]:
        note["lyric"] = None
        note["lyric_is_extended"] = False
    score["parts"].append(wordless)
    return score


def _silence_voice_in_bar(score, part_index, voice, measure):
    """Drop one voice's notes in one bar, so its lane copies another voice there."""
    score["parts"][part_index]["notes"] = [
        note for note in score["parts"][part_index]["notes"]
        if not (note.get("voice") == voice and note.get("measure_number") == measure)
    ]
    return score


def _lyric_slot_kinds(sheet, lane):
    kinds = {slot.kind for slot in sheet.slots if lane.lane_id in slot.lane_ids}
    kinds.update(slot_id.rsplit("|", 1)[-1] for slot_id in sheet.fixed
                 if slot_id.startswith(lane.lane_id + "|"))
    return kinds & {"lyric_source", "lyric_strategy", "lyric_line"}


def test_lyric_donor_order_flags_and_no_extension_policy():
    score = _with_wordless_part(parse_score(SATB, verse_number=1))
    # The local soprano has eight words; the distant tenor has just one.
    tenor = next(note for note in score["parts"][1]["notes"] if note.get("voice") == "5")
    tenor["lyric"] = "one"
    # A lane with its own notes sings them; the silent alto bar copies a voice.
    _silence_voice_in_bar(score, 0, "2", 1)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    alto_id = next(lane.lane_id for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    donors = [slot for slot in sheet.slots if slot.kind == "lyric_source" and alto_id in slot.lane_ids]
    assert donors
    assert donors[0].evidence == {"used_when": "melody from a staff without a lyric line"}
    assert donors[0].options[0].fields["lyric_source"]["part_index"] == 0
    cross = next(option for option in donors[0].options
                 if option.fields["lyric_source"]["part_index"] == 1)
    assert "P1 cross_staff_lyric_source_with_stronger_local_alternative" in cross.flags
    assert "lyric_policy" not in SLOT_KINDS

    for note in score["parts"][0]["notes"]:
        if note.get("voice") == "1" and note.get("measure_number") == 2:
            note["lyric"] = "+"
    next(note for note in score["parts"][1]["notes"]
         if note.get("voice") == "5" and note.get("measure_number") == 2)["lyric"] = "next"
    extension = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    assert not any(slot.kind == "lyric_policy" for slot in extension.slots)
    assert not any(slot_id.endswith("|lyric_policy") for slot_id in extension.fixed)


def test_same_part_other_staff_uses_weak_source_flag():
    score = parse_score(SATB, verse_number=1)
    combined = deepcopy(score["parts"][0])
    combined["part_id"] = "P1-Combined"
    combined["notes"] = [
        {**note, "staff": str(staff)}
        for staff, part in enumerate(score["parts"], start=1)
        for note in part["notes"]
    ]
    next(note for note in combined["notes"] if note.get("voice") == "5")["lyric"] = "one"
    # A third staff with no words keeps the lyric source menu in play.
    combined["notes"] += [
        {**note, "staff": "3", "voice": "9", "lyric": None, "lyric_is_extended": False}
        for note in score["parts"][0]["notes"] if note.get("voice") == "1"
    ]
    score["parts"] = [combined]
    _silence_voice_in_bar(score, 0, "2", 1)
    voice_part_by_voice = {vp["source_voice_id"]: vp["voice_part_id"]
                           for vp in _analyze_part_voice_parts(combined, 0)["voice_parts"]}
    sheet = build_sheet(score, {"target": {"part_id": "P1-Combined"}})
    alto_id = next(lane.lane_id for lane in sheet.lanes
                   if lane.source_voice_part_id == voice_part_by_voice["2"])
    donors = [option for slot in sheet.slots
              if slot.kind == "lyric_source" and alto_id in slot.lane_ids
              for option in slot.options]
    weak_other_scope = next(option for option in donors
                            if option.fields["lyric_source"]["voice_part_id"] == voice_part_by_voice["5"])
    assert "P1 weak_lyric_source_with_better_alternative" in weak_other_scope.flags
    assert not any("cross_staff" in flag for flag in weak_other_scope.flags)


def test_derived_and_generated_solfege_donors_are_excluded():
    score = parse_score(SATB, verse_number=1)
    derived = deepcopy(score["parts"][0])
    derived["part_id"] = "P_DERIVED_TEST"
    score["parts"].append(derived)
    score["voice_part_transforms"] = {
        "synthetic": {"appended_part_ref": {"part_index": 2},
                      "derived_lane": {"source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                       "slot": 1}}
    }
    for note in score["parts"][0]["notes"]:
        if note.get("voice") == "1":
            note["lyric_name"] = GENERATED_LYRIC_NAME
    donor = next(note for note in score["parts"][1]["notes"] if note.get("voice") == "5")
    donor["lyric"] = "word"
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    for option in _all_options(sheet):
        for field in ("melody_source", "lyric_source"):
            if field in option.fields:
                assert option.fields[field]["part_index"] != 2
    for lane in sheet.lanes:
        lane_options = [
            option for slot in sheet.slots if lane.lane_id in slot.lane_ids
            for option in slot.options
        ] + [
            option for slot_id, option in sheet.fixed.items()
            if slot_id.startswith(lane.lane_id + "|")
        ]
        for option in lane_options:
            if option.fields.get("lyric_source") == {
                "part_index": 0, "voice_part_id": "voice part 1",
            }:
                pytest.fail("generated-only lyrics must not be a fallback donor")
    derived_target = build_sheet(score, {"target": {"part_id": "P_DERIVED_TEST"}})
    for lane in derived_target.lanes:
        lane_options = [
            option for slot in derived_target.slots if lane.lane_id in slot.lane_ids
            for option in slot.options
        ] + [
            option for slot_id, option in derived_target.fixed.items()
            if slot_id.startswith(lane.lane_id + "|")
        ]
        for option in lane_options:
            source = option.fields.get("lyric_source")
            if source is None:
                continue
            assert source["part_index"] != 2
            assert source != {"part_index": 0, "voice_part_id": "voice part 1"}


def test_every_derive_section_gets_a_lyric_source():
    score = parse_score(SATB, verse_number=1)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    for target in plan["targets"]:
        for section in target["sections"]:
            if section["mode"] == "derive":
                assert section["lyric_source"]["voice_part_id"]
                assert section["lyric_policy"] == "replace_all"


def test_every_derive_lane_segment_has_a_lyric_donor_or_freeform():
    score = _silence_voice_in_bar(_with_wordless_part(parse_score(SATB, verse_number=1)), 0, "2", 2)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet), score=score)
    assert all(section["lyric_source"]["voice_part_id"]
               for target in plan["targets"] for section in target["sections"]
               if section["mode"] == "derive")
    for segment in sheet.segments:
        for lane_id, _ in segment.signature.lanes:
            relevant = [slot for slot in sheet.slots
                        if lane_id in slot.lane_ids and
                        (slot.start_measure, slot.end_measure) ==
                        (segment.start_measure, segment.end_measure)]
            if any(slot.kind == "freeform" for slot in relevant):
                continue
            mode_id = f"{lane_id}|{segment.start_measure}-{segment.end_measure}|mode"
            mode_options = next((slot.options for slot in relevant if slot.kind == "mode"),
                                (sheet.fixed[mode_id],) if mode_id in sheet.fixed else ())
            melody_id = f"{lane_id}|{segment.start_measure}-{segment.end_measure}|melody_source"
            if melody_id in sheet.fixed:
                # Its own notes, on a staff with a lyric line: code reads the words.
                continue
            if any(option.fields["mode"] == "derive" for option in mode_options):
                lyric_id = f"{lane_id}|{segment.start_measure}-{segment.end_measure}|lyric_source"
                assert any(slot.kind == "lyric_source" and slot.options for slot in relevant) or (
                    lyric_id in sheet.fixed
                )


def test_unmodelled_singing_segment_has_one_freeform_slot(monkeypatch, tmp_path):
    score = _chord_score(tmp_path)
    monkeypatch.setattr("src.api.preprocess_sheet._source_candidates", lambda *_: [])
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(sheet.slots) == len(sheet.segments)
    assert all(slot.kind == "freeform" and not slot.options for slot in sheet.slots)
    assert not sheet.fixed
    assert all(slot.evidence["per_measure_facts"] for slot in sheet.slots)


def test_shared_note_policy_requires_same_measure_and_note():
    score = parse_score(SATB, verse_number=1)
    lane = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1"}}).lanes[0]
    first = next(note for note in score["parts"][0]["notes"] if note.get("voice") == "1")
    another_measure = {**first, "voice": "2", "measure_number": 2}
    score["parts"][0]["notes"] = [first, another_measure]
    assert not _shared_notes_in_scope(score, lane)
    another_measure["measure_number"] = 1
    assert _shared_notes_in_scope(score, lane)


# --- Lyric reading rule (SIG-162) -------------------------------------------

_TYPES = {1: "eighth", 2: "quarter", 4: "half", 8: "whole"}


def _lyric_note(step, duration, lyric=None, syllabic="single", tie=None, *, voice, octave=5):
    """One note in divisions of 2 per quarter; tie is "start" or "stop"."""
    return (
        f'<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>'
        f'<duration>{duration}</duration>'
        + (f'<tie type="{tie}"/>' if tie else '')
        + f'<voice>{voice}</voice><type>{_TYPES[duration]}</type>'
        + (f'<notations><tied type="{tie}"/></notations>' if tie else '')
        + (f'<lyric number="1"><syllabic>{syllabic}</syllabic><text>{lyric}</text></lyric>'
           if lyric is not None else '')
        + '</note>'
    )


def lyric_line_xml(bars):
    """One staff; each bar is a list of voices, each voice a list of _lyric_note kwargs tuples."""
    measures = []
    for number, voices in enumerate(bars, start=1):
        body = []
        for index, notes in enumerate(voices):
            if index:
                body.append('<backup><duration>8</duration></backup>')
            body.extend(_lyric_note(*args, voice=str(index + 1), octave=5 - index) for args in notes)
        measures.append(
            f'<measure number="{number}">'
            + ('<attributes><divisions>2</divisions><time><beats>4</beats><beat-type>4</beat-type>'
               '</time><clef><sign>G</sign><line>2</line></clef></attributes>' if number == 1 else '')
            + "".join(body) + '</measure>'
        )
    return (
        '<score-partwise version="3.1"><part-list><score-part id="P1"><part-name>Choir</part-name>'
        '</score-part></part-list><part id="P1">' + "".join(measures) + '</part></score-partwise>'
    )


# Bar 1: both voices sing "Glo". Bar 2: the words are typed on voice 2 only, and
# voice 1 has extra notes on the half beats (beats 1, 1½, 2, 2½, 3, 4).
SHARED_LINE = [
    [[("G", 8, "Glo")], [("E", 8, "Glo")]],
    [[("G", 1), ("A", 1), ("G", 1), ("A", 1), ("G", 2), ("G", 2)],
     [("E", 2, "in"), ("E", 2, "ex", "begin"), ("E", 2, "cel", "middle"), ("E", 2, "sis", "end")]],
]


def _sung(result, lane_index):
    part = result["score"]["parts"][result["targets"][lane_index]["appended_part_ref"]["part_index"]]
    return [note.get("lyric") for note in part["notes"] if not note.get("is_rest")]


def _plan_and_run(score, sheet, choices=None):
    plan = assembler.assemble_plan(sheet, choices or assembler.first_option_choices(sheet), score=score)
    return plan, preprocess_voice_parts(deepcopy(score), request={"plan": plan})


def test_staff_lyric_lines_and_reading_follow_the_page(tmp_path):
    score = _parse_inline(tmp_path, lyric_line_xml(SHARED_LINE))
    lines = sheet_module.staff_lyric_lines(score)
    line = lines[(0, "1")]
    assert not line.multiple
    assert line.words[(2, 4.0)] == [("voice part 2", "in")]
    assert line.words[(1, 0.0)] == [("voice part 1", "Glo"), ("voice part 2", "Glo")]
    soprano = [note for note in score["parts"][0]["notes"] if note.get("voice") == "1"]
    assert sheet_module.read_lane_lyrics(line, soprano) == ["Glo", "in", "+", "ex", "+", "cel", "sis"]
    assert sheet_module.read_lane_lyrics(line, [{"measure_number": 0, "offset_beats": -1.0}]) == [None]


def test_shared_line_with_extra_notes_is_read_by_code(tmp_path):
    score = _parse_inline(tmp_path, lyric_line_xml(SHARED_LINE))
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    assert not any(_lyric_slot_kinds(sheet, lane) for lane in sheet.lanes)
    plan, result = _plan_and_run(score, sheet)
    assert result["status"] in {"ready", "ready_with_warnings"}
    assert _sung(result, 0) == ["Glo", "in", "+", "ex", "+", "cel", "sis"]
    assert _sung(result, 1) == ["Glo", "in", "ex", "cel", "sis"]
    bar_two = next(section for section in plan["targets"][0]["sections"]
                   if section["start_measure"] <= 2 <= section["end_measure"])
    assert bar_two["lyric_source"]["voice_part_id"] == "voice part 1"
    assert (bar_two["lyric_policy"], bar_two["lyric_strategy"]) == ("replace_all", "staff_line")
    assert bar_two["lyric_source_line"] == {"staff": None, "voice_part_id": None}
    assert score == original
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert json.dumps(asdict(sheet), sort_keys=True)


def test_words_typed_on_different_voices_reach_both_lanes(tmp_path):
    score = _parse_inline(tmp_path, lyric_line_xml([
        [[("G", 4, "one"), ("G", 4, "two")], [("E", 4), ("E", 4)]],
        [[("G", 4), ("G", 4)], [("E", 4, "three"), ("E", 4, "four")]],
    ]))
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not any(_lyric_slot_kinds(sheet, lane) for lane in sheet.lanes)
    _, result = _plan_and_run(score, sheet)
    assert result["status"] in {"ready", "ready_with_warnings"}
    assert _sung(result, 0) == _sung(result, 1) == ["one", "two", "three", "four"]


def test_held_note_keeps_its_syllable_in_one_section(tmp_path):
    score = _parse_inline(tmp_path, lyric_line_xml([
        [[("G", 4, "la"), ("A", 4, "Ah", "single", "start")]],
        [[("A", 8, None, "single", "stop")]],
        [[("G", 8, "end")]],
    ]))
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(sheet.segments) == 1
    plan, result = _plan_and_run(score, sheet)
    assert [(section["start_measure"], section["end_measure"])
            for section in plan["targets"][0]["sections"]] == [(1, 3)]
    assert result["status"] in {"ready", "ready_with_warnings"}
    assert "section_lyric_quality_failed" not in json.dumps(result.get("targets"))
    assert _sung(result, 0) == ["la", "Ah", "+", "end"]


def test_two_lines_on_one_staff_ask_which_line(tmp_path):
    score = _parse_inline(tmp_path, lyric_line_xml([
        [[("G", 4, "ex", "begin"), ("G", 4, "cel", "end")],
         [("E", 4, "uno"), ("E", 4, "dos")]],
    ]))
    assert sheet_module.staff_lyric_lines(score)[(0, "1")].multiple
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    line_slots = {slot.lane_ids[0]: slot for slot in sheet.slots if slot.kind == "lyric_line"}
    assert set(line_slots) == {lane.lane_id for lane in sheet.lanes}
    assert "lyric_line" in SLOT_KINDS
    soprano, alto = sheet.lanes
    assert [option.label for option in line_slots[soprano.lane_id].options] == ["ex-cel", "uno dos"]
    assert [option.label for option in line_slots[alto.lane_id].options] == ["uno dos", "ex-cel"]
    assert line_slots[soprano.lane_id].options[0].fields == {
        "lyric_line_voice": {"part_index": 0, "voice_part_id": "voice part 1"},
    }
    assert line_slots[soprano.lane_id].options[0].evidence == {"word_count": 2}
    assert not _lyric_slot_kinds(sheet, soprano) - {"lyric_line"}

    _, result = _plan_and_run(score, sheet)
    assert _sung(result, 0) == ["ex", "cel"]
    assert _sung(result, 1) == ["uno", "dos"]
    choices = assembler.first_option_choices(sheet)
    slot = line_slots[soprano.lane_id]
    choices[slot.slot_id] = slot.options[1].option_id
    _, result = _plan_and_run(score, sheet, choices)
    assert _sung(result, 0) == ["uno", "dos"]


def test_melody_from_a_staff_without_words_keeps_the_lyric_source_menu(tmp_path):
    xml = (
        '<score-partwise version="3.1"><part-list>'
        '<score-part id="P1"><part-name>Piano</part-name></score-part>'
        '<score-part id="P2"><part-name>Voice</part-name></score-part></part-list>'
        + "".join(
            f'<part id="{part_id}"><measure number="1"><attributes><divisions>2</divisions>'
            '<time><beats>4</beats><beat-type>4</beat-type></time>'
            '<clef><sign>G</sign><line>2</line></clef></attributes>'
            + _lyric_note("G", 4, lyric, voice="1") + _lyric_note("A", 4, lyric, voice="1")
            + '</measure></part>'
            for part_id, lyric in (("P1", None), ("P2", "la"))
        )
        + '</score-partwise>'
    )
    score = _parse_inline(tmp_path, xml)
    sheet = build_sheet(score, {"target": {"part_id": "Piano"}})
    lane = sheet.lanes[0]
    # One donor, so the menu is fixed; the lane's melody options include its own wordless staff.
    donors = [option for slot_id, option in sheet.fixed.items() if slot_id.endswith("|lyric_source")]
    assert [option.fields["lyric_source"] for option in donors] == [
        {"part_index": 1, "voice_part_id": "voice part 1"},
    ]
    assert not any(slot.kind == "lyric_policy" for slot in sheet.slots)
    plan, _ = _plan_and_run(score, sheet)
    section = plan["targets"][0]["sections"][0]
    assert section["lyric_policy"] == "replace_all"
    assert section["lyric_source"] == {"part_id": "Voice", "voice_part_id": "voice part 1"}
    assert _lyric_slot_kinds(sheet, lane) & {"lyric_source"}
