"""A late first tempo applies from beat zero in quote, voice, and MIDI."""

from pathlib import Path
from xml.etree import ElementTree

import pytest
from music21 import midi

from src.api import parse_score
from src.api.score import score_duration_seconds
from src.api.synthesize import TimeAxis, _compute_note_timing, _estimated_score_duration_seconds
from src.musicxml.performance_midi import build_instrumental_performance_midis
from src.musicxml.tempo_map import canonical_tempo_events
from src.musicxml.parser import TempoEvent
from src.pipeline import TimeAxis as PipelineTimeAxis


FIXTURE = Path(__file__).parent / "fixtures" / "late_first_tempo.musicxml"
FRAME_MS = 512 / 44100 * 1000


def _single_note_score(end_beat: float, tempos: list[dict]) -> dict:
    return {
        "parts": [{"notes": [{"offset_beats": 0.0, "duration_beats": end_beat}]}],
        "tempos": tempos,
    }


def _fixture_score() -> dict:
    score = parse_score(FIXTURE)
    assert score["tempos"] == [{"offset_beats": 8.0, "bpm": 60.0}]
    return score


def test_canonical_tempo_events() -> None:
    assert canonical_tempo_events([(188, 52)]) == [(0.0, 52.0)]
    assert canonical_tempo_events([(-1, 60)]) == [(0.0, 60.0)]
    assert canonical_tempo_events([]) == [(0.0, 120.0)]
    assert canonical_tempo_events([(4, 60), (0, 90)]) == [(0.0, 90.0), (4.0, 60.0)]
    assert canonical_tempo_events([(8, 0), (8, float("nan"))]) == [(0.0, 120.0)]


def test_score_duration_uses_first_mark_from_beat_zero() -> None:
    score = _single_note_score(204.0, [{"offset_beats": 188.0, "bpm": 52.0}])
    assert score_duration_seconds(score) == pytest.approx(235.385, abs=0.01)


def test_parsed_fixture_summary_uses_first_mark_from_beat_zero() -> None:
    score = _fixture_score()
    assert score["score_summary"]["duration_seconds"] == pytest.approx(16.0, abs=0.01)
    assert score_duration_seconds(score) == pytest.approx(16.0, abs=0.01)


def test_synthesis_axis_starts_at_zero() -> None:
    axis = TimeAxis([{"offset_beats": 188.0, "bpm": 52.0}])
    assert axis.get_ms_at_beat(0.0) == pytest.approx(0.0)
    assert axis.get_ms_at_beat(188.0) == pytest.approx(216923.077, abs=0.01)
    assert axis.get_ms_at_beat(204.0) == pytest.approx(235384.615, abs=0.01)
    assert TimeAxis([{"offset_beats": -1.0, "bpm": 60.0}]).get_ms_at_beat(0.0) == 0.0


def test_fixture_note_frames_keep_sung_span() -> None:
    score = _fixture_score()
    notes = score["parts"][0]["notes"]
    starts, ends, *_ = _compute_note_timing(notes, score["tempos"], FRAME_MS)
    assert starts == [0, 345, 689, 1034]
    assert ends == [345, 689, 1034, 1378]
    assert ends[-1] - starts[0] == 1378


def test_fixture_duration_parity() -> None:
    score = _fixture_score()
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
    assert score["tempos"] == [{"offset_beats": 8.0, "bpm": 60.0}]
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
    assert tempo_events == [(0, 60)]


def test_legacy_axis_uses_first_mark_and_handles_no_marks() -> None:
    axis = PipelineTimeAxis([TempoEvent(offset_beats=188.0, bpm=52.0)])
    assert axis.get_ms_at_beat(0.0) == pytest.approx(0.0)
    assert axis.get_ms_at_beat(188.0) == pytest.approx(216923.077, abs=0.01)
    assert axis.get_ms_at_beat(204.0) == pytest.approx(235384.615, abs=0.01)
    assert PipelineTimeAxis([]).get_ms_at_beat(2.0) == pytest.approx(1000.0)


def test_existing_tempo_behavior_guards() -> None:
    assert TimeAxis([]).get_ms_at_beat(2.0) == pytest.approx(1000.0)
    ordered = [{"offset_beats": 0.0, "bpm": 90.0}, {"offset_beats": 4.0, "bpm": 60.0}]
    assert TimeAxis(ordered).get_ms_at_beat(8.0) == pytest.approx(6666.667, abs=0.01)
    assert TimeAxis(list(reversed(ordered))).get_ms_at_beat(8.0) == pytest.approx(6666.667, abs=0.01)
    assert score_duration_seconds(_single_note_score(8.0, ordered)) == pytest.approx(6.667, abs=0.01)
