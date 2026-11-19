"""Synthetic click-track checks for the test-only MIDI sync report."""

from __future__ import annotations

import mido
import numpy as np
import pytest
import soundfile as sf

from scripts.sing_all_sync_check import measure_offsets


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


@pytest.mark.parametrize("shift_ms", [0, 40])
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
