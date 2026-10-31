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
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))["targets"]) == 2


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
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))["targets"]) == 3


def test_default_only_scope_keeps_default_lane_and_assembles(tmp_path):
    score = _single_staff_score(tmp_path, [_note("C"), _note("D")])
    voice_parts = _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]
    assert [str(vp["source_voice_id"]) for vp in voice_parts] == ["_default"]
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert not sheet.issues
    assert len(sheet.lanes) == 1
    assert sheet.lanes[0].source_voice_part_id == "voice part 1"
    assert len(assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))["targets"]) == 1


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


def test_lyric_kind_change_starts_new_segment():
    score = parse_score(SATB, verse_number=1)
    for note in score["parts"][0]["notes"]:
        if note.get("measure_number") == 2 and note.get("voice") == "1":
            note["lyric"] = "+"
    original = deepcopy(score)
    sheet = build_sheet_skeleton(score, {"target": {"part_id": "P1-Staff1"}})
    assert [segment.signature.lanes[0][2] for segment in sheet.segments] == ["words", "extension_only"]
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
    score = _single_staff_score(tmp_path, [
        _note("G", voice="1", lyric="word")
        + '<backup><duration>4</duration></backup>'
        + _note("E", voice="2", lyric=None),
        _note("C", voice="2", lyric=None),
    ])
    original = deepcopy(score)
    context = {"target": {"part_id": score["parts"][0]["part_id"]}}
    sheet = build_sheet(score, context)
    lane = next(lane for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    segment = next(segment for segment in sheet.segments if segment.start_measure == 2)
    assert next(native for lane_id, native, _ in segment.signature.lanes
                if lane_id == lane.lane_id)
    lyric = _lane_segment_options(sheet, lane, segment, "lyric_source")
    assert len(lyric) == 1
    assert lyric[0].fields["lyric_source"] == {"part_index": 0, "voice_part_id": "voice part 1"}
    assert lyric[0].evidence == {"reason": "no_lyrics_in_source_voice"}
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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
    lane, segment = sheet.lanes[0], sheet.segments[0]
    lyric = _lane_segment_options(sheet, lane, segment, "lyric_source")
    assert len(lyric) == 1
    assert lyric[0].option_id == f"{lane.part_id}|{lane.source_voice_part_id}"
    assert lyric[0].fields == {"lyric_source": {
        "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id,
    }}
    assert lyric[0].evidence == {"reason": "no_word_lyric_donor"}
    assert lyric[0].flags == ()
    assert json.dumps(asdict(sheet), sort_keys=True)
    assert asdict(sheet) == asdict(build_sheet(score, context))
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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
    assert not next(native for lane_id, native, _ in segment.signature.lanes
                    if lane_id == lane.lane_id)
    lyric = _lane_segment_options(sheet, lane, segment, "lyric_source")
    assert len(lyric) == 1
    assert lyric[0].fields["lyric_source"] == {
        "part_index": lane.part_index, "voice_part_id": lane.source_voice_part_id,
    }
    assert lyric[0].evidence == {"reason": "no_word_lyric_donor"}
    assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))


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
    assert not next(native for lane_id, native, _ in segment.signature.lanes
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
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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
        for segment in sheet.segments for lane_id, native, _ in segment.signature.lanes if native
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
    assert any(slot.kind == "rank_fallback" for slot in sheet.slots)
    assert sheet.fixed
    assert all(not slot.options or len(slot.options) > 1 for slot in sheet.slots)
    first_measure_lyric = [
        option for slot_id, option in sheet.fixed.items()
        if slot_id.endswith("|1-1|lyric_source")
    ]
    assert first_measure_lyric
    # Lint counts both notes of the chord, even though they share one onset.
    assert all(option.evidence["target_sung_note_count"] == 2
               and option.evidence["word_lyric_coverage_ratio"] == 1.0
               for option in first_measure_lyric)


def test_unnumbered_chord_uses_pitch_order_not_voice_number(tmp_path):
    score = _unnumbered_chord_with_opposite_voice_heights(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    slot = next(slot for slot in sheet.slots if slot.kind == "voice_assignment")
    assert slot.start_measure == slot.end_measure == 1
    assert not [slot for slot in sheet.slots if slot.kind == "melody_source"
                and slot.start_measure == 1]
    first = slot.options[0]
    ranks = first.fields["rank_index_by_lane"]
    raw_voice = {vp["voice_part_id"]: vp["source_voice_id"]
                 for vp in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]}
    high_lane = next(lane for lane in sheet.lanes
                     if raw_voice[lane.source_voice_part_id] == "2")
    low_lane = next(lane for lane in sheet.lanes
                    if raw_voice[lane.source_voice_part_id] == "1")
    assert ranks[high_lane.lane_id] == 0
    assert ranks[low_lane.lane_id] == 1
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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
    slot = next(slot for slot in sheet.slots if slot.kind == "voice_assignment")
    assert len(sheet.lanes) == 3 and len(slot.options) == 6
    assert slot.evidence["need"] == {"voice part 4": 2}
    assert len(set(source["voice_part_id"] for source in
                   slot.options[0].fields["melody_source_by_lane"].values())) == 1
    assert sorted(slot.options[0].fields["rank_index_by_lane"].values()) == [0, 1, 2]
    assert sum(slot.kind == "rank_fallback" for slot in sheet.slots) == 3
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
    assert all(target["sections"][0]["rank_fallback"] == "greedy" for target in plan["targets"])
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings") or []
                if finding.get("rule_severity") == "P0"]
    assert score == original
    assert asdict(sheet) == asdict(build_sheet(score, context))
    assert json.dumps(asdict(sheet), sort_keys=True)


def test_four_note_chord_moves_only_needed_native_lanes(tmp_path):
    score = _four_note_chord_with_native_lanes(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    slot = next(slot for slot in sheet.slots if slot.kind == "voice_assignment")
    assert len(sheet.lanes) == 4
    assert len(slot.options) == 24
    assert all("P1 drops_native_notes" in option.flags for option in slot.options)
    raw_voice = {vp["voice_part_id"]: vp["source_voice_id"]
                 for vp in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]}
    assert all(set(raw_voice[source["voice_part_id"]] for source in
                   option.fields["melody_source_by_lane"].values()) == {"1"}
               for option in slot.options)
    plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert not [finding for finding in result.get("lint_findings") or []
                if finding.get("rule_severity") == "P0"]


def test_chord_claims_exceed_available_lanes(tmp_path):
    score = _too_many_chord_claims(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    assert len(sheet.lanes) == 2
    assert len(sheet.slots) == 1
    assert sheet.slots[0].kind == "freeform"
    assert sheet.slots[0].evidence["reason"] == "chord_claims_exceed_lanes"


def test_first_option_never_simple_copies_a_chord_source(tmp_path):
    scores = [
        parse_score(SATB, verse_number=1), _chord_score(tmp_path),
        _two_staff_score(tmp_path), _unnumbered_chord_with_opposite_voice_heights(tmp_path),
        _two_chord_voices_with_silent_lanes(tmp_path), _four_note_chord_with_native_lanes(tmp_path),
    ]
    for score in scores:
        sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
        plan = assembler.assemble_plan(sheet, assembler.first_option_choices(sheet))
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


def test_lyric_donor_order_flags_and_extension_policy():
    score = parse_score(SATB, verse_number=1)
    # The local soprano has eight words; the distant tenor has just one.
    tenor = next(note for note in score["parts"][1]["notes"] if note.get("voice") == "5")
    tenor["lyric"] = "one"
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    alto_id = next(lane.lane_id for lane in sheet.lanes if lane.source_voice_part_id == "voice part 2")
    donors = [slot for slot in sheet.slots if slot.kind == "lyric_source" and alto_id in slot.lane_ids]
    assert donors
    assert donors[0].options[0].fields["lyric_source"]["part_index"] == 0
    cross = next(option for option in donors[0].options
                 if option.fields["lyric_source"]["part_index"] == 1)
    assert "P1 cross_staff_lyric_source_with_stronger_local_alternative" in cross.flags
    assert not any(slot.kind == "lyric_policy" for slot in sheet.slots)

    for note in score["parts"][0]["notes"]:
        if note.get("voice") == "1" and note.get("measure_number") == 2:
            note["lyric"] = "+"
    next(note for note in score["parts"][1]["notes"]
         if note.get("voice") == "5" and note.get("measure_number") == 2)["lyric"] = "next"
    extension = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    policies = [slot for slot in extension.slots if slot.kind == "lyric_policy"]
    assert policies
    assert [option.fields["lyric_policy"] for option in policies[0].options] == [
        "fill_missing_only", "replace_all"
    ]


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
    score["parts"] = [combined]
    sheet = build_sheet(score, {"target": {"part_id": "P1-Combined"}})
    alto_id = next(lane.lane_id for lane in sheet.lanes
                   if lane.source_voice_part_id == "voice part 2")
    donors = [option for slot in sheet.slots
              if slot.kind == "lyric_source" and alto_id in slot.lane_ids
              for option in slot.options]
    weak_other_scope = next(option for option in donors
                            if option.fields["lyric_source"]["voice_part_id"] == "voice part 3")
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


def test_every_derive_lane_segment_has_a_lyric_donor_or_freeform():
    score = parse_score(SATB, verse_number=1)
    sheet = build_sheet(score, {"target": {"part_id": "P1-Staff1"}})
    for segment in sheet.segments:
        for lane_id, _, _ in segment.signature.lanes:
            relevant = [slot for slot in sheet.slots
                        if lane_id in slot.lane_ids and
                        (slot.start_measure, slot.end_measure) ==
                        (segment.start_measure, segment.end_measure)]
            if any(slot.kind == "freeform" for slot in relevant):
                continue
            mode_id = f"{lane_id}|{segment.start_measure}-{segment.end_measure}|mode"
            mode_options = next((slot.options for slot in relevant if slot.kind == "mode"),
                                (sheet.fixed[mode_id],) if mode_id in sheet.fixed else ())
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
