"""
Vocoder API.
"""

import yaml
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.vocoder.model import Vocoder
from src.api.voicebank import load_voicebank_config


# Cache for loaded vocoders
_vocoder_cache: Dict[str, Vocoder] = {}


def _load_vocoder(voicebank_path: Path, device: str = "cpu") -> Vocoder:
    """Load vocoder from voicebank."""
    cache_key = f"vocoder:{voicebank_path}:{device}"
    if cache_key in _vocoder_cache:
        return _vocoder_cache[cache_key]
    
    conf = load_voicebank_config(voicebank_path)
    
    # Try vocoder path from config
    if "vocoder" in conf:
        vocoder_path = (voicebank_path / conf["vocoder"]).resolve()
        if vocoder_path.is_dir():
            vocoder_yaml = vocoder_path / "vocoder.yaml"
            if vocoder_yaml.exists():
                vocoder_conf = yaml.safe_load(vocoder_yaml.read_text())
                model_name = vocoder_conf.get("model")
                if model_name:
                    vocoder_path = vocoder_path / model_name
            else:
                vocoder_path = vocoder_path / "vocoder.onnx"
        voc = Vocoder(vocoder_path, device)
        _vocoder_cache[cache_key] = voc
        return voc
    
    # Try dsvocoder directory
    dsvocoder = voicebank_path / "dsvocoder"
    if dsvocoder.exists():
        vocoder_yaml = dsvocoder / "vocoder.yaml"
        if vocoder_yaml.exists():
            vocoder_conf = yaml.safe_load(vocoder_yaml.read_text())
            model_name = vocoder_conf.get("model")
            if model_name:
                voc = Vocoder((dsvocoder / model_name).resolve(), device)
                _vocoder_cache[cache_key] = voc
                return voc
        voc = Vocoder(dsvocoder / "vocoder.onnx", device)
        _vocoder_cache[cache_key] = voc
        return voc
    
    raise FileNotFoundError("Vocoder not found in voicebank")


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
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    # Load vocoder
    if vocoder_path:
        vocoder = Vocoder(Path(vocoder_path), device)
    else:
        vocoder = _load_vocoder(voicebank_path, device)
    
    # Convert to numpy
    mel_np = np.array(mel, dtype=np.float32)
    f0_np = np.array(f0, dtype=np.float32)
    
    # Ensure correct shapes
    if mel_np.ndim == 2:
        mel_np = mel_np[None, :]  # Add batch dim
    if f0_np.ndim == 1:
        f0_np = f0_np[None, :]  # Add batch dim
    
    # Run vocoder
    waveform = vocoder.forward(mel_np, f0_np)
    
    return {
        "waveform": waveform.flatten().tolist(),
        "sample_rate": config.get("sample_rate", 44100),
    }
