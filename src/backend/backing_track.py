from __future__ import annotations

"""Backing-track prompt writing and ElevenLabs composition helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import json
import logging
import copy
import re

import ffmpeg
import numpy as np
import soundfile as sf

from src.api.backing_track_prompt import (
    extract_backing_track_metadata,
)
from src.backend.config import Settings
from src.backend.llm_client import LlmClient
from src.backend.llm_factory import create_llm_client
from src.backend.llm_prompt import PromptBundle
from src.backend.secret_manager import read_secret


_PROMPT_WRITER_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent / "config" / "backing_track_prompt_writer_system_prompt.txt"
)


@dataclass(frozen=True)
class BackingTrackGenerationResult:
    """Completed backing-track artifact and prompt details."""

    output_path: Path
    duration_seconds: float
    prompt: str
    metadata: Dict[str, Any]
    output_format: str
    wav_output_path: Optional[Path] = None


@dataclass(frozen=True)
class CombinedBackingTrackGenerationResult:
    """Completed combined melody+backing artifact and reused backing metadata."""

    output_path: Path
    duration_seconds: float
    output_format: str
    metadata: Dict[str, Any]
    backing_track_output_path: Path
    backing_track_wav_output_path: Path
    backing_track_duration_seconds: float
    reused_existing_backing_track: bool
    prompt: Optional[str] = None


def generate_backing_track(
    *,
    file_path: str | Path,
    output_path: str | Path,
    style_request: str,
    additional_requirements: Optional[str],
    settings: Settings,
    output_format: str = "mp3_44100_128",
    seed: Optional[int] = None,
    llm_client: Optional[LlmClient] = None,
    logger: Optional[logging.Logger] = None,
) -> BackingTrackGenerationResult:
    """Generate an instrumental backing track for a score and save it to disk."""
    resolved_file_path = Path(file_path).resolve()
    resolved_output_path = Path(output_path).resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = extract_backing_track_metadata(resolved_file_path)
    prompt = write_backing_track_prompt(
        metadata=metadata,
        style_request=style_request,
        additional_requirements=additional_requirements,
        settings=settings,
        llm_client=llm_client,
        logger=logger,
    )
    waveform, sample_rate = _generate_backing_waveform(
        prompt=prompt,
        metadata=metadata,
        output_format=output_format,
        seed=seed,
        settings=settings,
    )
    save_result = _save_waveform(
        waveform=waveform,
        output_path=resolved_output_path,
        sample_rate=sample_rate,
        requested_output_format=output_format,
        keep_wav=True,
    )
    final_output_path = Path(save_result["path"])
    duration_seconds = float(save_result["duration_seconds"])
    wav_output_path = save_result.get("wav_path")
    return BackingTrackGenerationResult(
        output_path=final_output_path,
        duration_seconds=duration_seconds,
        prompt=prompt,
        metadata=metadata,
        output_format=output_format,
        wav_output_path=Path(wav_output_path) if isinstance(wav_output_path, str) else None,
    )


def generate_combined_backing_track(
    *,
    file_path: str | Path,
    output_path: str | Path,
    singing_audio_path: str | Path,
    style_request: str,
    additional_requirements: Optional[str],
    settings: Settings,
    output_format: str = "mp3_44100_128",
    seed: Optional[int] = None,
    llm_client: Optional[LlmClient] = None,
    logger: Optional[logging.Logger] = None,
    existing_backing_output_path: Optional[str | Path] = None,
    existing_backing_wav_path: Optional[str | Path] = None,
) -> CombinedBackingTrackGenerationResult:
    """Generate or reuse a backing track and return a combined melody+backing mix."""
    resolved_output_path = Path(output_path).resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_singing_audio_path = Path(singing_audio_path).resolve()
    if not resolved_singing_audio_path.exists():
        raise RuntimeError("Singing audio source for combined backing track was not found.")

    metadata = extract_backing_track_metadata(Path(file_path).resolve())
    reused_existing = False
    prompt: Optional[str] = None
    backing_output_path_resolved = (
        Path(existing_backing_output_path).resolve()
        if existing_backing_output_path
        else None
    )
    backing_wav_path_resolved = (
        Path(existing_backing_wav_path).resolve()
        if existing_backing_wav_path
        else None
    )
    backing_duration_seconds = float(metadata.get("duration_seconds") or 0.0)

    if backing_wav_path_resolved and backing_wav_path_resolved.exists():
        reused_existing = True
        if backing_output_path_resolved is None:
            candidate = backing_wav_path_resolved.with_suffix(".mp3")
            if candidate.exists():
                backing_output_path_resolved = candidate
            else:
                backing_output_path_resolved = backing_wav_path_resolved
    else:
        backing_base_name = f"{resolved_output_path.stem}.backing_source.mp3"
        generated_backing = generate_backing_track(
            file_path=file_path,
            output_path=resolved_output_path.with_name(backing_base_name),
            style_request=style_request,
            additional_requirements=additional_requirements,
            settings=settings,
            output_format=output_format,
            seed=seed,
            llm_client=llm_client,
            logger=logger,
        )
        prompt = generated_backing.prompt
        metadata = generated_backing.metadata
        backing_output_path_resolved = generated_backing.output_path
        backing_wav_path_resolved = generated_backing.wav_output_path
        backing_duration_seconds = generated_backing.duration_seconds
    if backing_wav_path_resolved is None or not backing_wav_path_resolved.exists():
        raise RuntimeError("Reusable backing-track WAV was not available for combined generation.")

    vocal_waveform = _load_audio_waveform(resolved_singing_audio_path)
    backing_waveform = _load_audio_waveform(backing_wav_path_resolved)
    mixed_waveform = _mix_vocal_and_backing(vocal_waveform, backing_waveform)
    save_result = _save_waveform(
        waveform=mixed_waveform,
        output_path=resolved_output_path,
        sample_rate=44100,
        requested_output_format=output_format,
        keep_wav=False,
    )
    return CombinedBackingTrackGenerationResult(
        output_path=Path(save_result["path"]),
        duration_seconds=float(save_result["duration_seconds"]),
        output_format=output_format,
        metadata=metadata,
        backing_track_output_path=backing_output_path_resolved or backing_wav_path_resolved,
        backing_track_wav_output_path=backing_wav_path_resolved,
        backing_track_duration_seconds=backing_duration_seconds,
        reused_existing_backing_track=reused_existing,
        prompt=prompt,
    )


def write_backing_track_prompt(
    *,
    metadata: Dict[str, Any],
    style_request: str,
    additional_requirements: Optional[str],
    settings: Settings,
    llm_client: Optional[LlmClient] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Use the configured LLM to turn score metadata into an ElevenLabs prompt."""
    active_logger = logger or logging.getLogger(__name__)
    client = llm_client or create_llm_client(settings)
    if client is None:
        active_logger.error("backing_track_prompt_writer_error reason=llm_client_unavailable")
        raise RuntimeError("Backing-track prompt writer LLM is unavailable.")

    bundle = PromptBundle(
        static_prompt_text=_load_prompt_writer_system_prompt(),
        dynamic_prompt_text=(
            "Backing Track Metadata:\n"
            f"{json.dumps(_prompt_writer_metadata(metadata), indent=2, sort_keys=True)}\n"
            "End Backing Track Metadata."
        ),
    )
    history = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "style_request": style_request,
                    "additional_requirements": additional_requirements or "",
                },
                sort_keys=True,
            ),
        }
    ]
    raw_text = client.generate(bundle, history)
    prompt = _parse_prompt_writer_response(raw_text)
    if prompt:
        return prompt
    active_logger.error(
        "backing_track_prompt_writer_error reason=invalid_llm_response response=%s",
        raw_text,
    )
    raise RuntimeError("Backing-track prompt writer returned an invalid response.")


def _compose_with_elevenlabs(
    *,
    prompt: str,
    music_length_ms: int,
    output_format: str,
    seed: Optional[int],
    settings: Settings,
):
    """Call the ElevenLabs Music API and return the streaming audio iterator."""
    from elevenlabs.client import ElevenLabs

    api_key = _resolve_elevenlabs_api_key(settings)
    if not api_key:
        raise RuntimeError("ElevenLabs API key is not configured.")
    client = ElevenLabs(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "prompt": prompt,
        "music_length_ms": music_length_ms,
        "model_id": settings.elevenlabs_music_model,
        "force_instrumental": True,
        "output_format": output_format,
    }
    if seed is not None:
        kwargs["seed"] = seed
    return client.music.compose(**kwargs)


def _resolve_elevenlabs_api_key(settings: Settings) -> str:
    """Load the ElevenLabs API key using the standard env/secret pattern."""
    app_env = settings.app_env.lower()
    if app_env in {"dev", "development", "local", "test"}:
        return settings.elevenlabs_api_key
    return read_secret(
        settings,
        settings.elevenlabs_api_key_secret,
        settings.elevenlabs_api_key_secret_version,
    )


def _load_prompt_writer_system_prompt() -> str:
    """Load the backing-track prompt writer system prompt from disk."""
    return _PROMPT_WRITER_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _should_prepend_pickup_silence(metadata: Dict[str, Any]) -> bool:
    """Return whether the score has a pickup that needs backend silence prepend."""
    pickup = metadata.get("pickup")
    return isinstance(pickup, dict) and bool(pickup.get("has_pickup"))


def _parse_output_sample_rate(output_format: str) -> int:
    """Extract the sample rate component from an ElevenLabs output format string."""
    match = re.match(r"^[a-z0-9]+_(\d+)(?:_|$)", str(output_format).strip().lower())
    if not match:
        raise RuntimeError(f"Unsupported ElevenLabs output format: {output_format}")
    return int(match.group(1))


def _parse_mp3_bitrate(output_format: str) -> str:
    """Extract MP3 bitrate from ElevenLabs output format strings like mp3_44100_128."""
    match = re.match(r"^mp3_\d+_(\d+)$", str(output_format).strip().lower())
    if not match:
        raise RuntimeError(
            f"Pickup silence prepend currently supports mp3_* final output formats, got: {output_format}"
        )
    return f"{match.group(1)}k"


def _measure_duration_seconds(metadata: Dict[str, Any]) -> float:
    """Compute the duration of one notated measure from score metadata."""
    beats_per_measure = int(metadata.get("beats_per_measure") or 4)
    beat_type = int(metadata.get("beat_type") or 4)
    bpm = float(metadata.get("bpm") or 120.0)
    if beat_type <= 0:
        beat_type = 4
    if bpm <= 0:
        bpm = 120.0
    quarter_notes_per_measure = float(beats_per_measure) * (4.0 / float(beat_type))
    return quarter_notes_per_measure * 60.0 / bpm


def _generate_backing_waveform(
    *,
    prompt: str,
    metadata: Dict[str, Any],
    output_format: str,
    seed: Optional[int],
    settings: Settings,
) -> Tuple[np.ndarray, int]:
    """Generate raw backing-track waveform from ElevenLabs and apply pickup handling."""
    sample_rate = _parse_output_sample_rate(output_format)
    audio_chunks = _compose_with_elevenlabs(
        prompt=prompt,
        music_length_ms=int(round(float(metadata["duration_seconds"]) * 1000.0)),
        output_format=f"pcm_{sample_rate}",
        seed=seed,
        settings=settings,
    )
    waveform = _pcm_bytes_to_waveform(b"".join(audio_chunks))
    if _should_prepend_pickup_silence(metadata):
        silence_samples = int(round(_measure_duration_seconds(metadata) * sample_rate))
        if silence_samples > 0:
            if waveform.ndim == 1:
                silence = np.zeros(silence_samples, dtype=np.float32)
            else:
                silence = np.zeros((silence_samples, waveform.shape[1]), dtype=np.float32)
            waveform = np.concatenate([silence, waveform.astype(np.float32)], axis=0)
    return waveform, sample_rate


def _pcm_bytes_to_waveform(audio_bytes: bytes) -> np.ndarray:
    """Decode raw 16-bit little-endian PCM bytes to float32 waveform."""
    if not audio_bytes:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32)
    channels = _infer_pcm_channel_count(audio_bytes)
    waveform = pcm / 32768.0
    if channels > 1:
        waveform = waveform.reshape(-1, channels)
    return waveform


def _infer_pcm_channel_count(audio_bytes: bytes) -> int:
    """Infer the channel count for ElevenLabs music PCM.

    Music renders are stereo in practice. Fall back to mono if the buffer length does
    not divide cleanly into stereo frames.
    """
    sample_count = len(audio_bytes) // 2
    if sample_count % 2 == 0:
        return 2
    return 1


def _save_waveform(
    *,
    waveform: np.ndarray,
    output_path: Path,
    sample_rate: int,
    requested_output_format: str,
    keep_wav: bool,
) -> Dict[str, Any]:
    """Persist a mono or stereo waveform to the requested final format."""
    fmt = str(requested_output_format).strip().lower()
    channels = 1 if waveform.ndim == 1 else int(waveform.shape[1])
    if fmt.startswith("mp3_"):
        mp3_path = output_path.with_suffix(".mp3")
        wav_path = output_path.with_suffix(".wav")
        if keep_wav:
            sf.write(str(wav_path), waveform, sample_rate)
        _encode_mp3_waveform(waveform, sample_rate, mp3_path, _parse_mp3_bitrate(fmt), channels)
        final_path = mp3_path
    elif fmt.startswith("pcm_"):
        final_path = output_path.with_suffix(".wav")
        sf.write(str(final_path), waveform, sample_rate)
    else:
        raise RuntimeError(
            f"Pickup silence prepend currently supports mp3_* or pcm_* final output formats, got: {requested_output_format}"
        )
    duration_seconds = waveform.shape[0] / float(sample_rate) if waveform.size else 0.0
    return {
        "path": str(final_path.resolve()),
        "duration_seconds": duration_seconds,
        "sample_rate": sample_rate,
        "wav_path": str(wav_path.resolve()) if fmt.startswith("mp3_") and keep_wav else None,
    }


def _encode_mp3_waveform(
    waveform: np.ndarray,
    sample_rate: int,
    output_path: Path,
    bitrate: str,
    channels: int,
) -> None:
    """Encode mono or stereo float32 waveform to MP3 using ffmpeg."""
    if waveform.dtype != np.float32:
        waveform = waveform.astype(np.float32)
    try:
        process = (
            ffmpeg
            .input("pipe:0", format="f32le", ac=channels, ar=sample_rate)
            .output(str(output_path), format="mp3", audio_bitrate=bitrate)
            .overwrite_output()
            .run_async(pipe_stdin=True, pipe_stdout=True, pipe_stderr=True)
        )
        process.stdin.write(waveform.tobytes(order="C"))
        process.stdin.close()
        stderr = process.stderr.read()
        retcode = process.wait()
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg binary not found. Install ffmpeg to write mp3.") from exc
    if retcode != 0:
        message = stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"ffmpeg mp3 encoding failed: {message}")


def _load_audio_waveform(
    path: Path,
    *,
    sample_rate: int = 44100,
    channels: int = 2,
) -> np.ndarray:
    """Decode an audio file to stereo float32 waveform at the target sample rate."""
    try:
        stdout, stderr = (
            ffmpeg
            .input(str(path))
            .output(
                "pipe:1",
                format="f32le",
                acodec="pcm_f32le",
                ac=channels,
                ar=sample_rate,
            )
            .run(capture_stdout=True, capture_stderr=True)
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg binary not found. Install ffmpeg to decode audio.") from exc
    except ffmpeg.Error as exc:
        message = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else str(exc)
        raise RuntimeError(f"ffmpeg audio decode failed: {message}") from exc
    waveform = np.frombuffer(stdout, dtype="<f4")
    if waveform.size == 0:
        return np.zeros((0, channels), dtype=np.float32)
    if waveform.size % channels != 0:
        raise RuntimeError(f"Decoded audio frame count is not divisible by channel count for {path}.")
    return waveform.reshape(-1, channels).astype(np.float32)


def _pad_waveform_to_length(waveform: np.ndarray, frame_count: int) -> np.ndarray:
    """Right-pad a waveform with silence to the target number of frames."""
    current_frames = waveform.shape[0]
    if current_frames >= frame_count:
        return waveform
    pad_shape = (frame_count - current_frames, waveform.shape[1])
    padding = np.zeros(pad_shape, dtype=np.float32)
    return np.concatenate([waveform, padding], axis=0)


def _mix_vocal_and_backing(
    vocal_waveform: np.ndarray,
    backing_waveform: np.ndarray,
    *,
    vocal_gain: float = 0.7,
    backing_gain: float = 0.7,
) -> np.ndarray:
    """Mix two stereo waveforms with fixed pre-mix gain and peak protection."""
    frame_count = max(vocal_waveform.shape[0], backing_waveform.shape[0])
    vocal = _pad_waveform_to_length(vocal_waveform, frame_count) * float(vocal_gain)
    backing = _pad_waveform_to_length(backing_waveform, frame_count) * float(backing_gain)
    mixed = (vocal + backing).astype(np.float32)
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1.0:
        mixed *= 0.98 / peak
    return mixed


def _prompt_writer_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize metadata for the prompt-writer LLM.

    The prompt writer should treat measure 1 as a normal bar and should not receive
    pickup-specific cues. If measure 1 lacks an explicit harmony, reuse the first
    available chord as the starting harmony so the writer can describe a full-bar
    accompaniment from the beginning.
    """
    sanitized = copy.deepcopy(metadata)
    sanitized.pop("pickup", None)
    progression = sanitized.get("chord_progression")
    if not isinstance(progression, list):
        return sanitized
    first_chord = None
    has_measure_one = False
    for entry in progression:
        if not isinstance(entry, dict):
            continue
        measure = entry.get("measure")
        chords = entry.get("chords")
        if measure == 1:
            has_measure_one = True
        if first_chord is None and isinstance(chords, list) and chords:
            candidate = str(chords[0]).strip()
            if candidate:
                first_chord = candidate
    if not has_measure_one and first_chord:
        sanitized["chord_progression"] = [{"measure": 1, "chords": [first_chord]}, *progression]
    return sanitized


def _parse_prompt_writer_response(text: str) -> Optional[str]:
    """Extract the JSON prompt field from an LLM response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("json"):
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    return prompt or None
