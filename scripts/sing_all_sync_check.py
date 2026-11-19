"""Test-only comparison of sung onsets and instrument MIDI on the MIDI clock.

This module is intentionally outside src/ and is never used by a package job.
"""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from statistics import median
from typing import Any

import mido
import numpy as np

from src.api.score import expanded_score_for_synthesis
from src.backend.audio_mix import _read_audio


def _midi_clock(midi: mido.MidiFile):
    events: list[tuple[int, int]] = []
    onsets: list[int] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                events.append((tick, message.tempo))
            elif message.type == "note_on" and message.velocity:
                onsets.append(tick)
    events.sort(key=lambda event: event[0])
    ticks = [0]
    seconds = [0.0]
    tempos = [500_000]
    for tick, tempo in events:
        if tick == ticks[-1]:
            tempos[-1] = tempo
        else:
            seconds.append(seconds[-1] + mido.tick2second(
                tick - ticks[-1], midi.ticks_per_beat, tempos[-1]
            ))
            ticks.append(tick)
            tempos.append(tempo)

    def at_beat(beat: float) -> float:
        target = round(beat * midi.ticks_per_beat)
        index = bisect_right(ticks, target) - 1
        return seconds[index] + mido.tick2second(
            target - ticks[index], midi.ticks_per_beat, tempos[index]
        )

    return at_beat, sorted(set(onsets))


def _heard_note_onsets(
    audio: np.ndarray, sample_rate: int, expected: list[float],
) -> list[float]:
    """Locate the steepest energy rise near each written note boundary.

    A sung vowel can continue across two notes without a quiet gap, so a
    whole-clip silence or ratio detector misses that second note's attack.
    The score narrows the search to 60 ms around each expected MIDI onset.
    """
    mono = np.mean(audio, axis=1) if audio.ndim == 2 else audio
    frame = max(1, round(sample_rate * 0.01))
    count = len(mono) // frame
    if not count:
        return []
    frames = mono[:count * frame].reshape((count, frame))
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
    rise = np.diff(rms, prepend=0.0)
    minimum_rise = max(0.005, float(np.max(rms, initial=0)) * 0.02)
    heard: list[float] = []
    for onset in expected:
        center = round(onset * sample_rate / frame)
        radius = max(1, round(0.06 * sample_rate / frame))
        start = max(0, center - radius)
        stop = min(count, center + radius + 1)
        if start >= stop:
            continue
        index = start + int(np.argmax(rise[start:stop]))
        if rise[index] >= minimum_rise:
            heard.append(index * frame / sample_rate)
    return heard


def _note_beats(part: dict[str, Any]) -> list[float]:
    return sorted({
        float(note["offset_beats"])
        for note in part.get("notes", [])
        if isinstance(note, dict)
        and not note.get("is_rest")
        and note.get("tie_type") not in {"stop", "continue"}
        and isinstance(note.get("offset_beats"), (int, float))
    })


def _paired_offset(
    expected: list[float], heard: list[float], tolerance: float,
) -> tuple[float | None, int]:
    available = list(heard)
    offsets: list[float] = []
    for onset in expected:
        if not available:
            break
        nearest = min(range(len(available)), key=lambda index: abs(available[index] - onset))
        delta = available[nearest] - onset
        if abs(delta) <= tolerance:
            offsets.append(delta * 1000)
            available.pop(nearest)
    return (float(median(offsets)) if offsets else None, len(offsets))


def measure_offsets(
    take_wav: Path, midi_path: Path, score: dict[str, Any],
    part: str, repeats: bool,
) -> dict[str, float | int | None]:
    """Measure the take and the MIDI's instrument notes on one tempo map."""
    midi = mido.MidiFile(str(midi_path))
    clock, midi_ticks = _midi_clock(midi)
    sung_score = expanded_score_for_synthesis(score) if repeats else score
    parts = [item for item in sung_score.get("parts", []) if isinstance(item, dict)]
    vocal_part = next(
        (item for item in parts if part in {item.get("part_id"), item.get("raw_part_id")}),
        None,
    )
    if vocal_part is None:
        raise ValueError(f"Part not found in score: {part}")
    audio, sample_rate = _read_audio(take_wav)
    expected_vocal = [clock(beat) for beat in _note_beats(vocal_part)]
    heard_vocal = _heard_note_onsets(audio, sample_rate, expected_vocal)
    vocal_ms, vocal_paired = _paired_offset(expected_vocal, heard_vocal, 0.150)

    instrument_beats = sorted({
        beat
        for item in parts
        if item is not vocal_part and not any(
            isinstance(note, dict) and (note.get("lyric") or "").strip()
            for note in item.get("notes", [])
        )
        for beat in _note_beats(item)
    })
    expected_instruments = [clock(beat) for beat in instrument_beats]
    written_instruments = [
        clock(tick / midi.ticks_per_beat) for tick in midi_ticks
    ]
    instruments_ms, instruments_paired = _paired_offset(
        expected_instruments, written_instruments, 0.050
    )
    return {
        "vocal_ms": vocal_ms,
        "vocal_paired": vocal_paired,
        "instruments_ms": instruments_ms,
        "instruments_paired": instruments_paired,
    }
