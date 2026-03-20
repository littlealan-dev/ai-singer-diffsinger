"""
Vocoder API.
"""

import logging
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.vocoder.model import Vocoder
from src.api.voicebank import load_voicebank_config, resolve_vocoder_model_path
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


# Cache for loaded vocoders
_vocoder_cache: Dict[str, Vocoder] = {}


def _load_vocoder(voicebank_path: Path, device: str = "cpu") -> Vocoder:
    """Load vocoder from voicebank."""
    cache_key = f"vocoder:{voicebank_path}:{device}"
    if cache_key in _vocoder_cache:
        return _vocoder_cache[cache_key]

    voc = Vocoder(resolve_vocoder_model_path(voicebank_path), device)
    _vocoder_cache[cache_key] = voc
    return voc


def vocode(
    mel: List[List[float]],
    f0: List[float],
    voicebank: Union[str, Path],
    *,
    vocoder_path: Optional[Union[str, Path]] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Convert Mel spectrogram to audio waveform.
    
    Args:
        mel: Mel spectrogram [frames × mel_bins]
        f0: Pitch curve in Hz (one value per frame)
        voicebank: Voicebank path (for default vocoder)
        vocoder_path: Optional explicit vocoder path
        device: Device for inference
        
    Returns:
        Dict with:
        - waveform: Audio samples as list
        - sample_rate: Sample rate
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "vocode input=%s",
            summarize_payload(
                {
                    "mel": mel,
                    "f0": f0,
                    "voicebank": str(voicebank),
                    "vocoder_path": str(vocoder_path) if vocoder_path else None,
                    "device": device,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)

    # Load vocoder.
    if vocoder_path:
        vocoder = Vocoder(Path(vocoder_path), device)
    else:
        vocoder = _load_vocoder(voicebank_path, device)

    # Convert to numpy.
    mel_np = np.array(mel, dtype=np.float32)
    f0_np = np.array(f0, dtype=np.float32)

    # Ensure correct shapes.
    if mel_np.ndim == 2:
        mel_np = mel_np[None, :]  # Add batch dim
    if f0_np.ndim == 1:
        f0_np = f0_np[None, :]  # Add batch dim
    
    # Run vocoder.
    waveform = vocoder.forward(mel_np, f0_np)
    
    result = {
        "waveform": waveform.flatten().tolist(),
        "sample_rate": config.get("sample_rate", 44100),
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("vocode output=%s", summarize_payload(result))
    return result
