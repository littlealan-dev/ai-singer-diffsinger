from __future__ import annotations

"""Audio mixdown helpers for backend export jobs."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import ffmpeg
import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class MixTrackSource:
    """Resolved audio source and gain for a mixdown lane."""

    key: str
    part_id: str
    label: str
    source_path: Path
    volume: float


def render_mix_to_wav(
    tracks: Iterable[MixTrackSource],
    output_path: Path,
    *,
    progress_callback: Callable[[float], None] | None = None,
) -> dict[str, float | int]:
    """Render source tracks into one WAV file, applying per-track volume."""
    sources = list(tracks)
    if not sources:
        raise ValueError("No audible tracks to export.")

    progress_callback = progress_callback or (lambda _progress: None)
    progress_callback(0.0)
    decoded: list[tuple[MixTrackSource, np.ndarray, int]] = []
    sample_rate: int | None = None
    max_samples = 0
    max_channels = 1

    for index, source in enumerate(sources):
        data, rate = _read_audio(source.source_path)
        if data.size == 0:
            raise ValueError(f"Track has no audio samples: {source.key}")
        if sample_rate is None:
            sample_rate = int(rate)
        elif int(rate) != sample_rate:
            raise ValueError(
                f"Track sample rate mismatch for {source.key}: {rate} != {sample_rate}"
            )
        decoded.append((source, data, int(rate)))
        max_samples = max(max_samples, int(data.shape[0]))
        max_channels = max(max_channels, int(data.shape[1]))
        progress_callback(0.1 + 0.35 * ((index + 1) / len(sources)))

    assert sample_rate is not None
    mix = np.zeros((max_samples, max_channels), dtype=np.float32)
    for index, (source, data, _rate) in enumerate(decoded):
        adjusted = _match_channels(data, max_channels) * float(source.volume)
        mix[: adjusted.shape[0], : adjusted.shape[1]] += adjusted
        progress_callback(0.45 + 0.35 * ((index + 1) / len(decoded)))

    mix = np.clip(mix, -1.0, 1.0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), mix, sample_rate, subtype="PCM_16")
    progress_callback(1.0)
    return {
        "duration_seconds": round(max_samples / float(sample_rate), 3),
        "sample_rate": sample_rate,
        "channels": max_channels,
    }


def _read_audio(path: Path) -> tuple[np.ndarray, int]:
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
        return data, int(rate)
    except Exception:
        return _read_audio_with_ffmpeg(path)


def _read_audio_with_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    probe = ffmpeg.probe(str(path))
    audio_stream = next(
        (
            stream
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    if not isinstance(audio_stream, dict):
        raise ValueError(f"Audio stream not found: {path}")
    sample_rate = int(audio_stream.get("sample_rate") or 0)
    channels = int(audio_stream.get("channels") or 0)
    if sample_rate <= 0 or channels <= 0:
        raise ValueError(f"Audio stream metadata is incomplete: {path}")
    raw, _stderr = (
        ffmpeg.input(str(path))
        .output("pipe:", format="f32le", acodec="pcm_f32le")
        .run(capture_stdout=True, capture_stderr=True, quiet=True)
    )
    data = np.frombuffer(raw, dtype=np.float32)
    if data.size % channels:
        data = data[: data.size - (data.size % channels)]
    return data.reshape((-1, channels)), sample_rate


def _match_channels(data: np.ndarray, channels: int) -> np.ndarray:
    if data.shape[1] == channels:
        return data
    if data.shape[1] == 1:
        return np.repeat(data, channels, axis=1)
    if data.shape[1] > channels:
        return data[:, :channels]
    padding = np.repeat(data[:, -1:], channels - data.shape[1], axis=1)
    return np.concatenate([data, padding], axis=1)
