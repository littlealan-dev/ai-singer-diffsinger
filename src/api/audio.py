"""
Audio output API.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import soundfile as sf

from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


def save_audio(
    waveform: Union[List[float], np.ndarray],
    output_path: Union[str, Path],
    *,
    sample_rate: int = 44100,
    format: str = "wav",
) -> Dict[str, Any]:
    """
    Write audio to a file.
    
    Args:
        waveform: Audio samples (list or numpy array)
        output_path: File path to save
        sample_rate: Sample rate (default: 44100)
        format: Audio format - "wav" or "mp3" (default: "wav")
        
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
                }
            ),
        )
    output_path = Path(output_path)
    
    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Convert to numpy if needed
    if isinstance(waveform, list):
        waveform = np.array(waveform, dtype=np.float32)
    
    # Flatten if needed
    waveform = waveform.flatten()
    
    # Normalize if out of range
    max_val = np.abs(waveform).max()
    if max_val > 1.0:
        waveform = waveform / max_val
    
    # Set output format suffix
    if format == "mp3":
        if not str(output_path).endswith(".mp3"):
            output_path = output_path.with_suffix(".mp3")
    else:
        if not str(output_path).endswith(".wav"):
            output_path = output_path.with_suffix(".wav")
    
    # Write file
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
