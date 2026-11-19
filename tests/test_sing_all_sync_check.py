"""Synthetic click-track checks for the test-only MIDI sync report."""

from __future__ import annotations

from copy import deepcopy

import mido
import numpy as np
import pytest
import soundfile as sf
from pathlib import Path
from xml.etree import ElementTree

from scripts.sing_all_sync_check import _note_beats, measure_offsets


def _midi_and_score(tmp_path):
    midi_path = tmp_path / "made-up-clock.mid"
    midi = mido.MidiFile(ticks_per_beat=480)
    tempo = mido.MidiTrack()
    tempo.extend([
        mido.MetaMessage("set_tempo", tempo=500_000, time=0),
        mido.MetaMessage("set_tempo", tempo=750_000, time=3 * 480),
    ])
    midi.tracks.append(tempo)
    notes = mido.MidiTrack()
    previous = 0
    for beat in range(6):
        tick = beat * 480
        notes.append(mido.Message("note_on", note=60, velocity=80, time=tick - previous))
        notes.append(mido.Message("note_off", note=60, velocity=0, time=120))
        previous = tick + 120
    midi.tracks.append(notes)
    midi.save(midi_path)
    score = {
        "parts": [
            {"part_id": "Voice", "notes": [
                {"offset_beats": beat, "is_rest": False, "lyric": "la"}
                for beat in range(6)
            ]},
            {"part_id": "Piano", "notes": [
                {"offset_beats": beat, "is_rest": False, "lyric": None}
                for beat in range(6)
            ]},
        ],
    }
    return midi_path, score


@pytest.mark.parametrize("shift_ms", [0, 40, 100, 140])
def test_click_track_reports_shift_on_midi_tempo_clock(tmp_path, shift_ms):
    midi_path, score = _midi_and_score(tmp_path)
    rate = 16000
    beat_seconds = [0, 0.5, 1.0, 1.5, 2.25, 3.0]
    audio = np.zeros(4 * rate, dtype=np.float32)
    for onset in beat_seconds:
        start = round((onset + shift_ms / 1000) * rate)
        audio[start:start + round(0.025 * rate)] = 0.4
    take = tmp_path / "made-up-clicks.wav"
    sf.write(take, audio, rate)
    offsets = measure_offsets(take, midi_path, score, "Voice", repeats=False)
    assert offsets["vocal_paired"] >= 5
    assert abs(offsets["vocal_ms"] - shift_ms) <= 5
    assert offsets["instruments_paired"] == 6
    assert abs(offsets["instruments_ms"]) <= 5


@pytest.mark.parametrize(
    ("feature", "source", "repeats"),
    [
        ("repeat-written", "repeat_navigation_piano/forward_repeat.xml", False),
        ("repeat-played", "repeat_navigation_piano/forward_repeat.xml", True),
        ("pickup", "late_first_tempo.musicxml", False),
        ("tempo-change", "late_first_tempo.musicxml", False),
    ],
)
def test_voicebank_take_matches_instrument_midi_clock(
    tmp_path, feature, source, repeats,
):
    """Generate the take at test time; no licensed vocal audio is committed."""
    from src.api.score import expanded_score_for_synthesis, parse_score
    from src.api.synthesize import synthesize
    from src.musicxml.performance_midi import build_instrumental_performance_midis

    root = Path(__file__).parent.parent
    voicebank = root / "assets/voicebanks/Raine_Rena_2.01"
    if not (voicebank / "dsmain").is_dir():
        pytest.skip("Local voicebank is not installed.")
    source_path = root / "tests/fixtures" / source
    if feature == "pickup":
        tree = ElementTree.parse(source_path)
        for xml_part in tree.getroot().findall("part"):
            first = xml_part.find("measure")
            assert first is not None
            first.set("implicit", "yes")
            first.set("number", "0")
            first.find("note/duration").text = "1"
            first.find("note/type").text = "quarter"
        source_path = tmp_path / "made-up-pickup.musicxml"
        tree.write(source_path, encoding="unicode")
    elif feature == "tempo-change":
        tree = ElementTree.parse(source_path)
        voice = tree.getroot().find("part")
        assert voice is not None
        measures = voice.findall("measure")
        later_direction = deepcopy(measures[2].find("direction"))
        assert later_direction is not None
        later_direction.find("sound").set("tempo", "90")
        later_direction.find("direction-type/metronome/per-minute").text = "90"
        measures[3].insert(0, later_direction)
        source_path = tmp_path / "made-up-tempo-change.musicxml"
        tree.write(source_path, encoding="unicode")
    score = parse_score(str(source_path))
    original = tmp_path / "written.mid"
    expanded = tmp_path / "played.mid"
    result = build_instrumental_performance_midis(
        source_path, original_output_path=original, expanded_output_path=expanded,
        score_summary=score.get("score_summary"),
    )
    midi_path = expanded if repeats else original
    assert midi_path.is_file(), f"{feature}: instrument MIDI unavailable: {result['diagnostic']}"
    if feature == "tempo-change":
        tempos = {
            message.tempo for track in mido.MidiFile(midi_path).tracks
            for message in track if message.type == "set_tempo"
        }
        assert len(tempos) >= 2, f"{feature}: MIDI has no real tempo change: {tempos}"
    take = synthesize(score, voicebank, part_index=0, expand_repeats=repeats, device="cpu")
    take_path = tmp_path / "generated-at-test-time.wav"
    sf.write(take_path, take["waveform"], take["sample_rate"])
    part = score["parts"][0]["part_id"]
    offsets = measure_offsets(take_path, midi_path, score, part, repeats)
    sung = expanded_score_for_synthesis(score) if repeats else score
    vocal_count = len(_note_beats(sung["parts"][0]))
    instrument_count = len(_note_beats(sung["parts"][1]))
    assert offsets["vocal_paired"] >= 0.8 * vocal_count, f"{feature}: {offsets}"
    assert offsets["instruments_paired"] >= 0.8 * instrument_count, f"{feature}: {offsets}"
    assert offsets["vocal_ms"] is not None and abs(offsets["vocal_ms"]) <= 30, f"{feature}: {offsets}"
    assert offsets["instruments_ms"] is not None and abs(offsets["instruments_ms"]) <= 30, f"{feature}: {offsets}"
