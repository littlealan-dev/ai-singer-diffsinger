"""Offline decision-sheet skeleton tests with synthetic scores only."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.api import parse_score
from src.api.preprocess_sheet import build_sheet_skeleton
from src.api.voice_parts import _staff_scope_measure_max_simultaneous_notes


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
            f"<staff>{staff}</staff></note>"
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
    assert score == original
