"""A late first tempo applies from beat zero in quote, voice, and MIDI."""

import logging
from pathlib import Path
from xml.etree import ElementTree

import pytest
from music21 import midi, tempo

from src.api import parse_score
from src.api.score import score_duration_seconds
from src.api.synthesize import TimeAxis, _compute_note_timing, _estimated_score_duration_seconds
from src.musicxml.performance_midi import build_instrumental_performance_midis
from src.musicxml.part_reference import load_musicxml_score
from src.musicxml.tempo_map import (
    canonical_tempo_events,
    normalize_score_tempo_map,
    score_tempo_events,
)


FIXTURE = Path(__file__).parent / "fixtures" / "late_first_tempo.musicxml"
BPMLESS_FIXTURE = Path(__file__).parent / "fixtures" / "bpmless_tempo_mark.musicxml"
FRAME_MS = 512 / 44100 * 1000


def _written_ends(score: dict) -> list[float]:
    return [
        entry["end_seconds"]
        for entry in score["score_summary"]["performance_measure_map"]["written"]
    ]


def _modified_bpmless_fixture(tmp_path: Path, *, text: str = "c.",
                               numbered_first: bool = True,
                               numbered_last: bool = False) -> Path:
    tree = ElementTree.parse(BPMLESS_FIXTURE)
    voice = tree.getroot().find("./part[@id='P1']")
    assert voice is not None
    first = voice.find("./measure[@number='1']")
    third = voice.find("./measure[@number='3']")
    fourth = voice.find("./measure[@number='4']")
    assert first is not None and third is not None and fourth is not None
    per_minute = third.find("./direction/direction-type/metronome/per-minute")
    assert per_minute is not None
    per_minute.text = text
    if not numbered_first:
        first.remove(first.find("direction"))
    if numbered_last:
        fourth.insert(0, ElementTree.fromstring(
            '<direction><direction-type><metronome><beat-unit>quarter</beat-unit>'
            '<per-minute>90</per-minute></metronome></direction-type>'
            '<sound tempo="90"/></direction>'
        ))
    source = tmp_path / "bpmless-variant.musicxml"
    tree.write(source, encoding="utf-8", xml_declaration=True)
    return source


def _single_note_score(end_beat: float, tempos: list[dict]) -> dict:
    return {
        "parts": [{"notes": [{"offset_beats": 0.0, "duration_beats": end_beat}]}],
        "tempos": tempos,
    }


def _fixture_score() -> dict:
    score = parse_score(FIXTURE)
    assert score["tempos"] == [
        {"offset_beats": 0.0, "bpm": 60.0},
        {"offset_beats": 8.0, "bpm": 60.0},
    ]
    return score


def _inline_two_part_score(tmp_path: Path, bpm: int | None) -> Path:
    direction = (
        f"<direction><direction-type><metronome><beat-unit>quarter</beat-unit>"
        f"<per-minute>{bpm}</per-minute></metronome></direction-type>"
        f"<sound tempo=\"{bpm}\"/></direction>"
        if bpm is not None else ""
    )
    source = tmp_path / "inline-score.musicxml"
    source.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<score-partwise version="4.0"><part-list>'
        '<score-part id="P1"><part-name>Voice</part-name></score-part>'
        '<score-part id="P2"><part-name>Piano</part-name></score-part>'
        '</part-list>'
        '<part id="P1"><measure number="1"><attributes><divisions>1</divisions>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>'
        + direction +
        '<note><pitch><step>C</step><octave>4</octave></pitch>'
        '<duration>4</duration><type>whole</type></note></measure></part>'
        '<part id="P2"><measure number="1"><attributes><divisions>1</divisions>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>'
        '<note><pitch><step>E</step><octave>4</octave></pitch>'
        '<duration>4</duration><type>whole</type></note></measure></part>'
        '</score-partwise>',
        encoding="utf-8",
    )
    return source


def test_canonical_tempo_events() -> None:
    assert canonical_tempo_events([(188, 52)]) == [(0.0, 52.0)]
    assert canonical_tempo_events([(-1, 60)]) == [(0.0, 60.0)]
    assert canonical_tempo_events([]) == [(0.0, 120.0)]
    assert canonical_tempo_events([(4, 60), (0, 90)]) == [(0.0, 90.0), (4.0, 60.0)]
    assert canonical_tempo_events([(8, 0), (8, float("nan"))]) == [(0.0, 120.0)]


def test_parsed_fixture_summary_uses_first_mark_from_beat_zero() -> None:
    score = _fixture_score()
    assert score["score_summary"]["duration_seconds"] == pytest.approx(16.0, abs=0.01)
    assert score_duration_seconds(score) == pytest.approx(16.0, abs=0.01)


def test_fixture_note_frames_keep_sung_span() -> None:
    score = _fixture_score()
    notes = score["parts"][0]["notes"]
    starts, ends, *_ = _compute_note_timing(notes, score["tempos"], FRAME_MS)
    assert starts == [0, 345, 689, 1034]
    assert ends == [345, 689, 1034, 1378]
    assert ends[-1] - starts[0] == 1378


def test_fixture_duration_parity() -> None:
    score = _fixture_score()
    assert TimeAxis(score["tempos"]).get_ms_at_beat(0.0) == 0.0
    values = (
        score["score_summary"]["duration_seconds"],
        score_duration_seconds(score),
        TimeAxis(score["tempos"]).get_ms_at_beat(16.0) / 1000,
        _estimated_score_duration_seconds(score),
    )
    assert values == pytest.approx((16.0,) * 4, abs=0.01)


def test_fixture_measure_highlight_map_matches_playback() -> None:
    score = _fixture_score()
    expected = [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0), (12.0, 16.0)]
    measure_map = score["score_summary"]["performance_measure_map"]
    for order in ("written", "expanded"):
        entries = measure_map[order]
        assert len(entries) == len(expected)
        for entry, (start, end) in zip(entries, expected):
            assert entry["start_seconds"] == pytest.approx(start)
            assert entry["end_seconds"] == pytest.approx(end)


def test_measure_highlight_map_uses_mark_owned_by_another_part(tmp_path: Path) -> None:
    tree = ElementTree.parse(FIXTURE)
    soprano, piano = tree.getroot().findall("part")
    soprano_measure = soprano.find("./measure[@number='3']")
    piano_measure = piano.find("./measure[@number='3']")
    assert soprano_measure is not None and piano_measure is not None
    direction = soprano_measure.find("direction")
    assert direction is not None
    soprano_measure.remove(direction)
    piano_measure.insert(0, direction)
    source = tmp_path / "piano-owned-tempo.musicxml"
    tree.write(source, encoding="utf-8", xml_declaration=True)

    score = parse_score(source)
    assert score["tempos"] == [
        {"offset_beats": 0.0, "bpm": 60.0},
        {"offset_beats": 8.0, "bpm": 60.0},
    ]
    entries = score["score_summary"]["performance_measure_map"]["written"]
    assert [entry["end_seconds"] for entry in entries] == pytest.approx([4.0, 8.0, 12.0, 16.0])


def test_instrumental_midi_starts_with_first_mark(tmp_path: Path) -> None:
    written = tmp_path / "written.mid"
    result = build_instrumental_performance_midis(
        FIXTURE,
        original_output_path=written,
        expanded_output_path=tmp_path / "expanded.mid",
    )
    assert result["original_midi_available"] is True
    midi_file = midi.MidiFile()
    midi_file.open(str(written))
    midi_file.read()
    midi_file.close()
    absolute_tick = 0
    tempo_events = []
    for delta, event in zip(midi_file.tracks[0].events[::2], midi_file.tracks[0].events[1::2]):
        absolute_tick += delta.time
        if event.type == midi.MetaEvents.SET_TEMPO:
            tempo_events.append(
                (absolute_tick, round(60_000_000 / int.from_bytes(event.data, byteorder="big")))
            )
    assert tempo_events == [(0, 60), (80640, 60)]


def test_fixture_normalizes_each_part_tempo_map() -> None:
    loaded = load_musicxml_score(FIXTURE)
    for part in loaded.parts:
        marks = sorted(
            (round(mark.getOffsetInHierarchy(part), 6), mark.getQuarterBPM())
            for mark in part.recurse().getElementsByClass(tempo.MetronomeMark)
        )
        assert marks == [(0.0, 60.0), (8.0, 60.0)]


def test_no_mark_keeps_implicit_default(tmp_path: Path) -> None:
    source = _inline_two_part_score(tmp_path, None)
    loaded = load_musicxml_score(source)
    assert score_tempo_events(loaded) == []
    normalize_score_tempo_map(loaded)
    assert score_tempo_events(loaded) == []
    assert parse_score(source)["tempos"] == [{"offset_beats": 0.0, "bpm": 120.0}]


def test_beat_zero_mark_is_copied_to_other_part(tmp_path: Path) -> None:
    source = _inline_two_part_score(tmp_path, 88)
    loaded = load_musicxml_score(source)
    for part in loaded.parts:
        assert score_tempo_events(part) == [(0.0, 88.0)]
    assert parse_score(source)["tempos"] == [{"offset_beats": 0.0, "bpm": 88.0}]


def test_existing_tempo_behavior_guards() -> None:
    assert TimeAxis([]).get_ms_at_beat(2.0) == pytest.approx(1000.0)
    ordered = [{"offset_beats": 0.0, "bpm": 90.0}, {"offset_beats": 4.0, "bpm": 60.0}]
    assert TimeAxis(ordered).get_ms_at_beat(8.0) == pytest.approx(6666.667, abs=0.01)
    assert TimeAxis(list(reversed(ordered))).get_ms_at_beat(8.0) == pytest.approx(6666.667, abs=0.01)
    assert score_duration_seconds(_single_note_score(8.0, ordered)) == pytest.approx(6.667, abs=0.01)


def test_bpmless_fixture_keeps_cursor_map_and_sung_duration() -> None:
    score = parse_score(BPMLESS_FIXTURE)
    assert _written_ends(score) == pytest.approx([4.0, 8.0, 12.0, 16.0])
    assert score["tempos"] == [{"offset_beats": 0.0, "bpm": 60.0}]
    assert score["score_summary"]["duration_seconds"] == pytest.approx(16.0)


@pytest.mark.parametrize("text", ["c.", "ca. 60", "60-66", ""])
def test_bpmless_text_variants_keep_cursor_map(tmp_path: Path, text: str) -> None:
    source = _modified_bpmless_fixture(tmp_path, text=text)
    assert _written_ends(parse_score(source)) == pytest.approx([4.0, 8.0, 12.0, 16.0])


def test_only_bpmless_mark_uses_default_tempo(tmp_path: Path) -> None:
    source = _modified_bpmless_fixture(tmp_path, numbered_first=False)
    score = parse_score(source)
    assert score["tempos"] == [{"offset_beats": 0.0, "bpm": 120.0}]
    assert _written_ends(score) == pytest.approx([2.0, 4.0, 6.0, 8.0])
    assert _written_ends(score)[-1] == pytest.approx(score_duration_seconds(score))


def test_numbered_mark_after_bpmless_mark_keeps_timeline(tmp_path: Path) -> None:
    source = _modified_bpmless_fixture(tmp_path, numbered_last=True)
    score = parse_score(source)
    assert _written_ends(score) == pytest.approx([4.0, 8.0, 12.0, 14.667], abs=0.01)
    assert _written_ends(score)[-1] == pytest.approx(score_duration_seconds(score))


def test_bpmless_mark_removed_from_each_loaded_part() -> None:
    loaded = load_musicxml_score(BPMLESS_FIXTURE)
    for part in loaded.parts:
        marks = part.recurse().getElementsByClass(tempo.MetronomeMark)
        assert all(mark.getQuarterBPM() is not None for mark in marks)
        assert score_tempo_events(part) == [(0.0, 60.0)]


def test_bpmless_fixture_does_not_log_map_failure(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        parse_score(BPMLESS_FIXTURE)
    assert "performance_measure_map_failed" not in caplog.text


def test_bpmless_fixture_instrumental_midi_retains_single_tempo(tmp_path: Path) -> None:
    written = tmp_path / "written.mid"
    result = build_instrumental_performance_midis(
        BPMLESS_FIXTURE,
        original_output_path=written,
        expanded_output_path=tmp_path / "expanded.mid",
    )
    assert result["original_midi_available"] is True
    midi_file = midi.MidiFile()
    midi_file.open(str(written))
    midi_file.read()
    midi_file.close()
    absolute_tick = 0
    tempo_events = []
    for delta, event in zip(midi_file.tracks[0].events[::2], midi_file.tracks[0].events[1::2]):
        absolute_tick += delta.time
        if event.type == midi.MetaEvents.SET_TEMPO:
            tempo_events.append(
                (absolute_tick, round(60_000_000 / int.from_bytes(event.data, byteorder="big")))
            )
    assert tempo_events == [(0, 60)]
