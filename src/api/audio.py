"""
Audio output API.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import soundfile as sf
import ffmpeg

from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


def save_audio(
    waveform: Union[List[float], np.ndarray],
    output_path: Union[str, Path],
    *,
    sample_rate: int = 44100,
    format: str = "wav",
    mp3_bitrate: str = "256k",
    keep_wav: bool = False,
) -> Dict[str, Any]:
    """
    Write audio to a file.
    
    Args:
        waveform: Audio samples (list or numpy array)
        output_path: File path to save
        sample_rate: Sample rate (default: 44100)
        format: Audio format - "wav" or "mp3" (default: "wav")
        mp3_bitrate: MP3 bitrate (e.g. "256k")
        keep_wav: When writing mp3, also keep a wav copy
        
    Returns:
        Dict with:
        - path: Absolute path to saved file
        - duration_seconds: Audio duration
        - sample_rate: Sample rate used
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "save_audio input=%s",
            summarize_payload(
                {
                    "waveform": waveform,
                    "output_path": str(output_path),
                    "sample_rate": sample_rate,
                    "format": format,
                    "mp3_bitrate": mp3_bitrate,
                    "keep_wav": keep_wav,
                }
            ),
        )
    output_path = Path(output_path)

    # Ensure parent directory exists.
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to numpy if needed.
    if isinstance(waveform, list):
        waveform = np.array(waveform, dtype=np.float32)

    # Flatten if needed.
    waveform = waveform.flatten()

    # Normalize if out of range.
    max_val = np.abs(waveform).max()
    if max_val > 1.0:
        waveform = waveform / max_val
    
    format = (format or "wav").lower()
    if format == "mp3":
        mp3_path = output_path.with_suffix(".mp3")
        wav_path = output_path.with_suffix(".wav")
        if keep_wav:
            sf.write(str(wav_path), waveform, sample_rate)
        _encode_mp3(waveform, sample_rate, mp3_path, mp3_bitrate)
        output_path = mp3_path
    else:
        if not str(output_path).endswith(".wav"):
            output_path = output_path.with_suffix(".wav")
        sf.write(str(output_path), waveform, sample_rate)
    
    duration = len(waveform) / sample_rate
    
    result = {
        "path": str(output_path.resolve()),
        "duration_seconds": duration,
        "sample_rate": sample_rate,
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("save_audio output=%s", summarize_payload(result))
    return result


def _encode_mp3(
    waveform: np.ndarray,
    sample_rate: int,
    output_path: Path,
    bitrate: str,
) -> None:
    """Encode a mono float32 waveform to MP3 using ffmpeg."""
    if waveform.dtype != np.float32:
        waveform = waveform.astype(np.float32)
    try:
        # Stream raw PCM samples to ffmpeg for MP3 encoding.
        process = (
            ffmpeg
            .input("pipe:0", format="f32le", ac=1, ar=sample_rate)
            .output(str(output_path), format="mp3", audio_bitrate=bitrate)
            .overwrite_output()
            .run_async(pipe_stdin=True, pipe_stdout=True, pipe_stderr=True)
        )
        process.stdin.write(waveform.tobytes())
        process.stdin.close()
        stderr = process.stderr.read()
        retcode = process.wait()
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg binary not found. Install ffmpeg to write mp3.") from exc
    if retcode != 0:
        message = stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"ffmpeg mp3 encoding failed: {message}")
