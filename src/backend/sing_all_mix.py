"""Offline Sing All mix planning and rendering, independent of job storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import ffmpeg
import mido
import numpy as np
import pyloudnorm as pyln


VOCAL_REF_LUFS = -23.0
FULL_INSTRUMENTS_DB = -6.0
PART_DOMINANT_INSTRUMENTS_DB = -12.0
TARGET_LUFS = -16.0
PEAK_LIMIT_DB = -1.0
MAX_LIMITING_DB = 3.0
MAX_VOCAL_GAIN_DB = 12.0
MAX_INSTRUMENT_GAIN_DB = 18.0
RELEASE_TAIL_SECONDS = 1.0


@dataclass(frozen=True)
class Stem:
    name: str
    audio: np.ndarray


@dataclass(frozen=True)
class TakeRef:
    part_id: str
    part_name: str
    mode: Literal["lyrics", "solfege"]


@dataclass(frozen=True)
class PlannedMix:
    kind: Literal["full", "part"]
    mode: Literal["lyrics", "solfege"]
    part_id: str | None
    file_name: str


def _stereo(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float32)
    if data.ndim == 1:
        return np.repeat(data[:, None], 2, axis=1)
    if data.ndim == 2 and data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    if data.ndim == 2 and data.shape[1] == 2:
        return data
    raise ValueError("Audio must be mono or stereo.")


def lufs(audio: np.ndarray, sample_rate: int) -> float | None:
    """Measure stereo BS.1770 integrated loudness; silence and short clips have none."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive.")
    data = _stereo(audio)
    if data.shape[0] < int(np.ceil(0.4 * sample_rate)):
        return None
    try:
        result = float(pyln.Meter(sample_rate).integrated_loudness(data))
    except ValueError:
        return None
    return result if np.isfinite(result) else None


def limit(
    audio: np.ndarray, sample_rate: int, peak_db: float = PEAK_LIMIT_DB
) -> np.ndarray:
    """Apply ffmpeg's lookahead limiter without its automatic makeup gain."""
    data = _stereo(audio)
    if not len(data):
        return data.copy()
    threshold = float(10 ** (peak_db / 20))
    if not 0.0625 <= threshold <= 1:
        raise ValueError("peak_db is outside ffmpeg alimiter's supported range.")
    raw, _ = (
        ffmpeg.input("pipe:", format="f32le", ac=2, ar=sample_rate)
        .filter("alimiter", limit=threshold, level=0, latency=1)
        .output("pipe:", format="f32le", acodec="pcm_f32le", ac=2, ar=sample_rate)
        .run(input=data.tobytes(), capture_stdout=True, capture_stderr=True)
    )
    rendered = np.frombuffer(raw, dtype=np.float32).reshape((-1, 2))
    if len(rendered) < len(data):
        rendered = np.pad(rendered, ((0, len(data) - len(rendered)), (0, 0)))
    return np.clip(rendered[:len(data)], -threshold, threshold).astype(np.float32)


def _peak_db(audio: np.ndarray) -> float:
    peak = float(np.max(np.abs(audio), initial=0))
    return float(20 * np.log10(peak)) if peak else float("-inf")


def balanced_mix(
    vocals: list[Stem], instruments: np.ndarray | None,
    instruments_db: float, sample_rate: int,
) -> tuple[np.ndarray, dict]:
    """Balance stems, bring the mix toward the target, and limit its peaks."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive.")
    vocal_data = [_stereo(stem.audio) for stem in vocals]
    instrument_data = _stereo(instruments) if instruments is not None else None
    length = max(
        [len(data) for data in vocal_data] +
        ([len(instrument_data)] if instrument_data is not None else [0])
    )
    vocal_bus = np.zeros((length, 2), dtype=np.float32)
    vocal_stats: list[dict] = []
    for stem, data in zip(vocals, vocal_data):
        measured = lufs(data, sample_rate)
        gain_db = None if measured is None else min(
            VOCAL_REF_LUFS - measured, MAX_VOCAL_GAIN_DB
        )
        vocal_stats.append({
            "name": stem.name, "lufs": measured, "gain_db": gain_db,
            "dropped": gain_db is None,
        })
        if gain_db is not None:
            vocal_bus[:len(data)] += data * np.float32(10 ** (gain_db / 20))

    mix = vocal_bus.copy()
    instrument_stats = None
    if instrument_data is not None:
        padded = np.zeros_like(mix)
        padded[:len(instrument_data)] = instrument_data
        # A sample-wise threshold selects just the sung passages. The loudness
        # meter integrates those passages rather than a loud instrumental intro.
        sung = np.max(np.abs(vocal_bus), axis=1) > 10 ** (-50 / 20)
        selected = padded[sung] if int(np.count_nonzero(sung)) >= 3 * sample_rate else padded
        measured = lufs(selected, sample_rate)
        vocal_level = lufs(vocal_bus, sample_rate)
        gain_db = None if measured is None else (
            min(vocal_level + instruments_db - measured, MAX_INSTRUMENT_GAIN_DB)
            if vocal_level is not None else 0.0
        )
        instrument_stats = {"lufs": measured, "gain_db": gain_db}
        if gain_db is not None:
            mix += padded * np.float32(10 ** (gain_db / 20))

    measured_mix = lufs(mix, sample_rate)
    limiting_db = 0.0
    if measured_mix is not None:
        gain_db = TARGET_LUFS - measured_mix
        overshoot = max(0.0, _peak_db(mix) + gain_db - PEAK_LIMIT_DB)
        if overshoot > MAX_LIMITING_DB:
            gain_db -= overshoot - MAX_LIMITING_DB
            limiting_db = MAX_LIMITING_DB
        else:
            limiting_db = overshoot
        mix *= np.float32(10 ** (gain_db / 20))
    result = limit(mix, sample_rate)
    return result, {
        "vocals": vocal_stats,
        "instruments": instrument_stats,
        "mix": {
            "lufs": lufs(result, sample_rate),
            "peak_db": _peak_db(result),
            "limiting_db": limiting_db,
        },
    }


def render_instruments(
    midi_path: Path, soundfont_path: Path, sample_rate: int,
    drop_tracks: set[int],
) -> np.ndarray:
    """Render MIDI track events directly through an in-process FluidSynth."""
    import fluidsynth  # Deliberately optional for the API and pure mix tests.

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive.")
    midi = mido.MidiFile(str(midi_path))
    events: list[tuple[int, int, mido.Message]] = []
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        for message in track:
            tick += message.time
            events.append((tick, track_index, message))
    events.sort(key=lambda event: (event[0], event[1]))

    synth = fluidsynth.Synth(samplerate=sample_rate)
    output: list[np.ndarray] = []
    try:
        sfid = synth.sfload(str(soundfont_path))
        if sfid < 0:
            raise ValueError(f"Could not load soundfont: {soundfont_path}")
        for channel in range(16):
            synth.program_select(channel, sfid, 128 if channel == 9 else 0, 0)

        def pull(frames: int) -> None:
            while frames > 0:
                count = min(frames, 8192)
                output.append(
                    synth.get_samples(count).reshape((-1, 2)).astype(np.float32) / 32768
                )
                frames -= count

        tempo = 500_000
        tick = 0
        elapsed = 0.0
        cursor = 0
        for event_tick, track_index, message in events:
            elapsed += mido.tick2second(event_tick - tick, midi.ticks_per_beat, tempo)
            tick = event_tick
            position = round(elapsed * sample_rate)
            pull(position - cursor)
            cursor = position
            if message.type == "set_tempo":
                tempo = message.tempo
                continue
            if track_index in drop_tracks or message.is_meta:
                continue
            channel = getattr(message, "channel", 0)
            if message.type == "program_change":
                synth.program_change(channel, message.program)
            elif message.type == "note_on":
                if message.velocity:
                    synth.noteon(channel, message.note, message.velocity)
                else:
                    synth.noteoff(channel, message.note)
            elif message.type == "note_off":
                synth.noteoff(channel, message.note)
            elif message.type == "control_change":
                synth.cc(channel, message.control, message.value)
            elif message.type == "pitchwheel":
                synth.pitch_bend(channel, message.pitch)
        pull(round(RELEASE_TAIL_SECONDS * sample_rate))
    finally:
        synth.delete()
    return np.concatenate(output, axis=0) if output else np.zeros((0, 2), dtype=np.float32)


def _file_part_name(part_name: str, part_id: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part_name).strip(" .")
    return name or part_id


def plan_mixes(
    takes: list[TakeRef], mixes: Literal["all", "full", "none"],
    instruments_sound: bool,
) -> list[PlannedMix]:
    """Plan full mixes, then each distinct part for lyrics before solfège."""
    if mixes not in {"all", "full", "none"}:
        raise ValueError("mixes must be all, full, or none.")
    if mixes == "none":
        return []
    plan: list[PlannedMix] = []
    for mode in ("lyrics", "solfege"):
        in_mode = [take for take in takes if take.mode == mode]
        if not in_mode:
            continue
        plan.append(PlannedMix("full", mode, None, f"Full mix ({mode}).mp3"))
        if mixes == "all" and instruments_sound:
            seen: set[str] = set()
            for take in in_mode:
                if take.part_id in seen:
                    continue
                seen.add(take.part_id)
                name = _file_part_name(take.part_name, take.part_id)
                plan.append(PlannedMix("part", mode, take.part_id, f"{name} ({mode}).mp3"))
    return plan


def write_mp3(audio: np.ndarray, sample_rate: int, path: Path) -> None:
    """Encode an already balanced stereo mix through ffmpeg."""
    data = _stereo(audio)
    path.parent.mkdir(parents=True, exist_ok=True)
    (
        ffmpeg.input("pipe:", format="f32le", ac=2, ar=sample_rate)
        .output(str(path), acodec="libmp3lame", audio_bitrate="192k", format="mp3")
        .overwrite_output()
        .run(input=data.tobytes(), capture_stdout=True, capture_stderr=True)
    )
