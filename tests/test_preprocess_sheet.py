"""Offline decision-sheet skeleton tests with synthetic scores only."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.api import parse_score
from src.api.preprocess_sheet import (
    SLOT_KINDS, Lane, _rank_assignment_options, _shared_notes_in_scope,
    build_sheet, build_sheet_skeleton,
)
from src.musicxml.solfege import GENERATED_LYRIC_NAME
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


def _all_options(sheet):
    return [option for slot in sheet.slots for option in slot.options] + list(sheet.fixed.values())


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


def test_chord_rank_assignment_and_fixed_single_options(tmp_path):
    score = _chord_score(tmp_path)
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    rank_slots = [slot for slot in sheet.slots if slot.kind == "rank_assignment"]
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
    assert list(_rank_assignment_options([nonzero])[0].fields["rank_index_by_lane"].values()) == [1, 2]
    assert any(slot.kind == "rank_fallback" for slot in sheet.slots)
    assert sheet.fixed
    assert all(not slot.options or len(slot.options) > 1 for slot in sheet.slots)


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
                if field == "lyric_source":
                    assert option.fields[field] != {"part_index": 0, "voice_part_id": "voice part 1"}


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
