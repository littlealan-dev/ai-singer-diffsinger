"""Synthetic, license-safe checks for offline Sing All mixing."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import mido
import numpy as np
import pytest
import soundfile as sf

from src.backend import sing_all_mix as mix


RATE = 16000


def _tone(seconds: float, level: float, frequency: float = 220) -> np.ndarray:
    time = np.arange(round(seconds * RATE), dtype=np.float32) / RATE
    return (level * np.sin(2 * np.pi * frequency * time)).astype(np.float32)


def test_vocals_reach_reference_and_silence_is_dropped():
    quiet = _tone(5, 0.025)
    loud = _tone(5, 0.2, 330)
    audio, stats = mix.balanced_mix(
        [mix.Stem("Quiet", quiet), mix.Stem("Loud", loud),
         mix.Stem("Silent", np.zeros_like(quiet))],
        None, mix.FULL_INSTRUMENTS_DB, RATE,
    )
    first, second, silent = stats["vocals"]
    assert abs((first["lufs"] + first["gain_db"]) -
               (second["lufs"] + second["gain_db"])) < 0.5
    assert silent["dropped"] and silent["gain_db"] is None
    assert audio.shape == (5 * RATE, 2) and audio.dtype == np.float32
    assert abs(stats["mix"]["lufs"] - mix.TARGET_LUFS) < 0.5
    assert stats["mix"]["peak_db"] <= mix.PEAK_LIMIT_DB + 0.01

    near_silent = _tone(5, 0.001)
    _, tiny = mix.balanced_mix(
        [mix.Stem("Tiny", near_silent)], None, mix.FULL_INSTRUMENTS_DB, RATE
    )
    assert tiny["vocals"][0]["gain_db"] <= mix.MAX_VOCAL_GAIN_DB


def test_instruments_are_measured_during_sung_passages():
    vocal = np.r_[np.zeros(4 * RATE, dtype=np.float32), _tone(5, 0.1)]
    instruments = np.r_[_tone(4, 0.75), _tone(5, 0.04)]
    _, stats = mix.balanced_mix(
        [mix.Stem("Verse", vocal)], instruments,
        mix.FULL_INSTRUMENTS_DB, RATE,
    )
    during_verses = mix.lufs(_tone(5, 0.04), RATE)
    full_song = mix.lufs(instruments, RATE)
    assert abs(stats["instruments"]["lufs"] - during_verses) < 0.5
    assert full_song - stats["instruments"]["lufs"] > 5
    assert stats["instruments"]["gain_db"] <= mix.MAX_INSTRUMENT_GAIN_DB


def test_excessive_peak_accepts_quieter_mix():
    audio = _tone(5, 0.01)
    audio[RATE] = 0.99
    rendered, stats = mix.balanced_mix(
        [mix.Stem("Transient", audio)], None, mix.FULL_INSTRUMENTS_DB, RATE
    )
    assert stats["mix"]["limiting_db"] <= mix.MAX_LIMITING_DB + 0.01
    assert stats["mix"]["lufs"] < mix.TARGET_LUFS - 0.5
    assert np.max(np.abs(rendered)) <= 10 ** (mix.PEAK_LIMIT_DB / 20) + 1e-5


def test_peak_limit_override_reaches_the_limiter(monkeypatch):
    monkeypatch.setattr(mix, "PEAK_LIMIT_DB", -3.0)
    audio = _tone(5, 0.01)
    audio[RATE] = 0.99
    rendered, stats = mix.balanced_mix(
        [mix.Stem("Made Up", audio)], None,
        mix.FULL_INSTRUMENTS_DB, RATE,
    )
    assert np.max(np.abs(rendered)) <= 10 ** (-3 / 20) + 1e-5
    assert stats["mix"]["peak_db"] <= -3 + 0.01


def test_mix_plan_counts_modes_and_names():
    lyrics = [mix.TakeRef(f"P{i}", f"Part {i}", "lyrics") for i in range(4)]
    both = lyrics + [mix.TakeRef(t.part_id, t.part_name, "solfege") for t in lyrics]
    assert len(mix.plan_mixes(lyrics, "full", True)) == 1
    assert len(mix.plan_mixes(lyrics, "all", True)) == 5
    assert len(mix.plan_mixes(both, "full", True)) == 2
    plan = mix.plan_mixes(both, "all", True)
    assert len(plan) == 10
    assert plan[0].file_name == "Full mix (lyrics).mp3"
    assert plan[1].file_name == "Part 0 (lyrics).mp3"
    assert plan[5].file_name == "Full mix (solfège).mp3"
    assert plan[-1].file_name == "Part 3 (solfège).mp3"
    assert mix.plan_mixes(both, "all", False) == mix.plan_mixes(both, "full", False)
    assert mix.plan_mixes(both, "none", True) == []


def test_trial_command_writes_mp3_and_prints_stats(tmp_path):
    sf.write(tmp_path / "made-up.wav", _tone(5, 0.08), RATE)
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.sing_all_mix_trial", str(tmp_path),
            "--take", "P1", "Part One", "lyrics", "made-up.wav",
        ],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout.strip())
    assert report["vocals"][0]["name"] == "Part One (lyrics)"
    assert report["mix"]["lufs"] is not None
    assert (tmp_path / "mixes" / "Full mix (lyrics).mp3").is_file()


def _fluid_available() -> bool:
    if importlib.util.find_spec("fluidsynth") is None:
        return False
    try:
        import fluidsynth  # noqa: F401
    except ImportError:
        return False
    return Path("ui/public/soundfonts/FluidR3_GM.sf2").is_file()


def _two_track_midi(tmp_path):
    midi_path = tmp_path / "made-up.mid"
    midi = mido.MidiFile(ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500_000)]))
    for note in (60, 64):
        midi.tracks.append(mido.MidiTrack([
            mido.Message("program_change", channel=0, program=76, time=0),
            mido.Message("note_on", channel=0, note=note, velocity=90, time=0),
            mido.Message("note_off", channel=0, note=note, velocity=0, time=960),
        ]))
    midi.save(midi_path)
    return midi_path


def test_instrument_render_rejects_wrong_track_sound_count(tmp_path):
    midi_path = _two_track_midi(tmp_path)
    with pytest.raises(ValueError, match="Expected 2 sounds"):
        mix.render_instruments(midi_path, tmp_path / "unused.sf2", RATE, [mix.TrackSound(0, 0)])


@pytest.mark.skipif(not _fluid_available(), reason="FluidSynth library or soundfont unavailable")
def test_instrument_render_has_tail_mutes_and_overrides_programs(tmp_path):
    midi_path = _two_track_midi(tmp_path)
    soundfont = Path("ui/public/soundfonts/FluidR3_GM.sf2")
    piano = mix.render_instruments(
        midi_path, soundfont, RATE, [mix.TrackSound(0, 0), None]
    )
    guitar = mix.render_instruments(
        midi_path, soundfont, RATE, [mix.TrackSound(0, 24), None]
    )
    muted = mix.render_instruments(midi_path, soundfont, RATE, [None, None])
    expected = RATE + round(mix.RELEASE_TAIL_SECONDS * RATE)
    assert piano.shape == guitar.shape == muted.shape == (expected, 2)
    assert np.max(np.abs(piano)) > 0.01
    assert np.max(np.abs(muted)) == 0
    assert not np.allclose(piano, guitar)
