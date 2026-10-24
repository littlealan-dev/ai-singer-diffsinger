"""Round-trip coverage for timing in materialized derived MusicXML lanes."""

from pathlib import Path
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import pytest

from src.api import parse_score
from src.api.voice_parts import (
    _append_transformed_measures,
    _materialize_transformed_part,
    preprocess_voice_parts,
)


def _note(pitch: str, duration: int, *, lyric: str | None = None, tie: str | None = None) -> str:
    lyric_xml = f"<lyric><text>{lyric}</text></lyric>" if lyric else ""
    tie_xml = f'<tie type="{tie}"/>' if tie else ""
    tied_xml = f'<notations><tied type="{tie}"/></notations>' if tie else ""
    return (
        f"<note><pitch><step>{pitch}</step><octave>4</octave></pitch>"
        f"<duration>{duration}</duration><type>quarter</type>{tie_xml}"
        f"{tied_xml}{lyric_xml}</note>"
    )


def _rest(duration: int) -> str:
    return f"<note><rest/><duration>{duration}</duration><type>quarter</type></note>"


def _score_xml(measures: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<score-partwise version="3.1"><part-list><score-part id="P1">'
        '<part-name>Solo</part-name></score-part></part-list>'
        f'<part id="P1">{"".join(measures)}</part></score-partwise>'
    )


def _measure(number: int, body: str, *, beats: int = 4, implicit: bool = False) -> str:
    implicit_attr = ' implicit="yes"' if implicit else ""
    return (
        f'<measure number="{number}"{implicit_attr}>'
        f'<attributes><divisions>4</divisions><time><beats>{beats}</beats>'
        '<beat-type>4</beat-type></time></attributes>'
        f"{body}</measure>"
    )


def _note_signature(note: dict) -> tuple:
    return (
        note["measure_number"],
        note["offset_beats"],
        note["duration_beats"],
        note["pitch_midi"],
        note["lyric"],
    )


def _materialize(xml: str, *, keep_pitches: set[float] | None = None):
    with TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / "source.xml"
        source_path.write_text(xml, encoding="utf-8")
        source = parse_score(source_path)
        lane = [
            note
            for note in source["parts"][0]["notes"]
            if not note["is_rest"]
            and (keep_pitches is None or note["pitch_midi"] in keep_pitches)
        ]
        result = _materialize_transformed_part(
            source_musicxml_path=str(source_path),
            transformed_part={"part_id": "P1", "notes": lane},
            part_id="P_DERIVED_TEST",
            part_name="Solo Derived",
            transform_hash="0123456789abcdef",
        )
        assert result["status"] == "ready", result
        output_path = result["modified_musicxml_path"]
        reparsed = parse_score(output_path)
        derived = next(part for part in reparsed["parts"] if part["part_name"] == "Solo Derived")
        written = ET.parse(output_path).getroot().find("part[@id='P_DERIVED_TEST']")
        assert written is not None
        return lane, derived["notes"], written


def _assert_round_trip(lane: list[dict], reparsed: list[dict]) -> None:
    sung = [note for note in reparsed if not note["is_rest"]]
    assert [_note_signature(note) for note in sung] == [_note_signature(note) for note in lane]


def _rest_beats(measure: ET.Element) -> list[float]:
    return [
        int(note.findtext("duration")) / 4
        for note in measure.findall("note")
        if note.find("rest") is not None
    ]


def test_leading_gap_preserves_beat_four_notes():
    xml = _score_xml([
        _measure(1, _rest(12) + _note("C", 2, lyric="la") + _note("D", 2, lyric="li")),
    ])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert lane[0]["offset_beats"] == 3.0
    assert sum(_rest_beats(written.find("measure"))) == 3.0


def test_middle_gap_preserves_beat_three_note():
    xml = _score_xml([_measure(1, _note("C", 4) + _rest(4) + _note("D", 4) + _rest(4))])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert _rest_beats(written.find("measure")) == [1.0, 1.0]


def test_trailing_gap_fills_the_bar():
    xml = _score_xml([_measure(1, _note("C", 4) + _rest(12))])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert sum(_rest_beats(written.find("measure"))) == 3.0
    assert sum(int(note.findtext("duration")) for note in written.find("measure").findall("note")) == 16


def test_three_beat_gap_uses_exact_representable_rests():
    xml = _score_xml([_measure(1, _rest(12) + _note("C", 4))])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    measure = written.find("measure")
    assert sum(_rest_beats(measure)) == 3.0
    assert sum(int(note.findtext("duration")) for note in measure.findall("note")) == 16
    for note in measure.findall("note"):
        if note.find("rest") is None:
            continue
        kind = note.findtext("type")
        dots = len(note.findall("dot"))
        base = {"whole": 4, "half": 2, "quarter": 1, "eighth": 0.5, "16th": 0.25}[kind]
        assert int(note.findtext("duration")) / 4 == base * sum(0.5**n for n in range(dots + 1))


def test_three_four_meter_and_implicit_pickup():
    xml = _score_xml([
        _measure(0, _note("C", 4, lyric="up"), beats=3, implicit=True),
        _measure(1, _rest(8) + _note("D", 4, lyric="down"), beats=3),
        _measure(2, _note("E", 4) + _rest(8), beats=3),
    ])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert [note["offset_beats"] for note in lane] == [0.0, 3.0, 4.0]
    assert [_rest_beats(m) for m in written.findall("measure")] == [[], [2.0], [2.0]]


def test_unmarked_pickup_uses_source_measure_timeline():
    xml = _score_xml([
        _measure(0, _note("C", 4), implicit=False),
        _measure(1, _note("C", 4) + _note("D", 4) + _note("E", 4) + _note("F", 4)),
        _measure(2, _note("G", 4) + _rest(12)),
    ])
    lane, reparsed, _ = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert [note["offset_beats"] for note in lane] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_underfull_middle_bar_does_not_shift_following_notes():
    xml = _score_xml([
        _measure(1, _note("C", 4) + _note("D", 4) + _note("E", 4) + _note("F", 4)),
        _measure(2, _note("G", 4) + _note("A", 4) + _note("B", 4)),
        _measure(3, _note("C", 4) + _rest(12)),
    ])
    lane, reparsed, _ = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    assert lane[-1]["offset_beats"] == 7.0


def test_no_reference_does_not_write_missing_bars_as_leading_rests():
    part = ET.Element("part", {"id": "P_DERIVED_TEST"})
    _append_transformed_measures(
        part,
        transformed_notes=[{
            "measure_number": 3,
            "offset_beats": 8.0,
            "duration_beats": 1.0,
            "pitch_midi": 60.0,
        }],
        divisions=4,
        reference_part=None,
        q=lambda tag: tag,
    )
    measure = part.find("measure")
    assert measure is not None
    assert measure.get("number") == "3"
    assert measure.find("note/rest") is None


def test_ties_and_lyrics_survive_bar_line():
    xml = _score_xml([
        _measure(1, _rest(12) + _note("C", 4, lyric="hold", tie="start")),
        _measure(2, _note("C", 4, tie="stop") + _rest(12)),
    ])
    lane, reparsed, written = _materialize(xml)
    _assert_round_trip(lane, reparsed)
    sung = [note for note in reparsed if not note["is_rest"]]
    assert [note["tie_type"] for note in sung] == ["start", "stop"]
    assert [note["lyric"] for note in sung] == [note["lyric"] for note in lane]
    assert [tie.get("type") for tie in written.findall(".//tie")] == ["start", "stop"]


def test_ranked_chord_split_lower_lane_keeps_beat_four():
    chord_bar = (
        '<forward><duration>12</duration></forward>'
        + _note("G", 4, lyric="la")
        + '<note><chord/><pitch><step>C</step><octave>4</octave></pitch>'
        '<duration>4</duration><type>quarter</type></note>'
    )
    xml = _score_xml([_measure(1, chord_bar), _measure(2, chord_bar)])

    def target(rank: int) -> dict:
        source = {"part_index": 0, "voice_part_id": "voice part 1"}
        return {
            "source": source,
            "output": {"mode": "append_new_derived_lane"},
            "split_coverage": "complete",
            "sections": [{
                "start_measure": 1,
                "end_measure": 2,
                "mode": "derive",
                "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                "method": "ranked",
                "rank_index": rank,
                "rank_fallback": "greedy",
                "melody_source": source,
                "lyric_source": source,
                "lyric_strategy": "strict_onset",
                "lyric_policy": "replace_all",
            }],
        }

    with TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / "chords.xml"
        source_path.write_text(xml, encoding="utf-8")
        result = preprocess_voice_parts(
            parse_score(source_path), request={"plan": {"targets": [target(0), target(1)]}}
        )
        assert result["status"] == "ready", result
        output = parse_score(result["modified_musicxml_path"])
        lower = next(
            part for part in output["parts"]
            if part["part_name"] == result["appended_part_ref"]["part_name"]
        )
        transformed = result["score"]["parts"][result["appended_part_ref"]["part_index"]]
        _assert_round_trip(
            [note for note in transformed["notes"] if not note["is_rest"]], lower["notes"]
        )
        sung = [note for note in lower["notes"] if not note["is_rest"]]
        assert [(note["offset_beats"], note["pitch_midi"]) for note in sung] == [
            (3.0, 60.0), (7.0, 60.0)
        ]
