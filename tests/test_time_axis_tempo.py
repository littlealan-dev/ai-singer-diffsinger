"""Regression coverage for scores whose first tempo mark follows beat zero."""

import importlib
from pathlib import Path

import pytest

from src.api import parse_score
from src.api.score import score_duration_seconds
from src.musicxml.parser import TempoEvent
from src.pipeline import TimeAxis as PipelineTimeAxis

synthesize = importlib.import_module("src.api.synthesize")
FIXTURE = Path(__file__).parent / "fixtures" / "late_first_tempo.musicxml"
FRAME_MS = 512 / 44100 * 1000


def _fixture_score():
    return parse_score(FIXTURE)


def test_synthesis_axis_uses_120_before_late_first_mark():
    axis = synthesize.TimeAxis([{"offset_beats": 188.0, "bpm": 52.0}])
    assert axis.get_ms_at_beat(0.0) == pytest.approx(0.0)
    assert axis.get_ms_at_beat(188.0) == pytest.approx(94000.0)
    assert axis.get_ms_at_beat(204.0) == pytest.approx(112461.538, abs=0.01)


def test_synthesis_axis_clamps_negative_tempo_offset():
    axis = synthesize.TimeAxis([{"offset_beats": -1.0, "bpm": 60.0}])
    assert axis.get_ms_at_beat(0.0) == pytest.approx(0.0)
    assert axis.get_ms_at_beat(1.0) == pytest.approx(1000.0)


def test_parsed_score_note_frames_follow_default_then_marked_tempo():
    score = _fixture_score()
    assert score["tempos"] == [{"offset_beats": 8.0, "bpm": 60.0}]
    notes = score["parts"][0]["notes"]
    start_frames, end_frames, _, _, _ = synthesize._compute_note_timing(
        notes, score["tempos"], FRAME_MS
    )
    assert start_frames == [0, 172, 345, 689]
    assert end_frames == [172, 345, 689, 1034]


def test_parsed_score_duration_agrees_with_sung_axis():
    score = _fixture_score()
    durations = (
        score_duration_seconds(score),
        score["score_summary"]["duration_seconds"],
        synthesize.TimeAxis(score["tempos"]).get_ms_at_beat(16.0) / 1000,
    )
    assert durations == pytest.approx((12.0, 12.0, 12.0), abs=0.01)


def test_pipeline_axis_uses_same_default_and_handles_no_marks():
    axis = PipelineTimeAxis([TempoEvent(offset_beats=188.0, bpm=52.0)])
    assert axis.get_ms_at_beat(0.0) == pytest.approx(0.0)
    assert axis.get_ms_at_beat(188.0) == pytest.approx(94000.0)
    assert axis.get_ms_at_beat(204.0) == pytest.approx(112461.538, abs=0.01)
    assert PipelineTimeAxis([]).get_ms_at_beat(2.0) == pytest.approx(1000.0)


def test_existing_tempo_and_duration_guards():
    assert synthesize.TimeAxis([]).get_ms_at_beat(2.0) == pytest.approx(1000.0)
    ordered = [{"offset_beats": 0.0, "bpm": 90.0}, {"offset_beats": 4.0, "bpm": 60.0}]
    for events in (ordered, list(reversed(ordered))):
        assert synthesize.TimeAxis(events).get_ms_at_beat(8.0) == pytest.approx(
            6666.667, abs=0.01
        )
    assert synthesize._estimated_score_duration_seconds(_fixture_score()) == pytest.approx(
        12.0
    )
