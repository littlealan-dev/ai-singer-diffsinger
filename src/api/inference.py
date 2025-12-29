"""
Inference APIs for duration, pitch, variance, and mel synthesis.
"""

import logging
import yaml
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.acoustic.model import LinguisticModel, DurationModel, PitchModel, VarianceModel, AcousticModel
from src.api.voicebank import load_voicebank_config, load_speaker_embed
from src.api.vocode import vocode
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


# Cache for loaded models
_model_cache: Dict[str, Any] = {}


def _get_model(model_class, model_path: Path, device: str = "cpu"):
    """Get or create a cached model instance."""
    cache_key = f"{model_class.__name__}:{model_path}"
    if cache_key not in _model_cache:
        _model_cache[cache_key] = model_class(model_path, device)
    return _model_cache[cache_key]


def _load_linguistic_model(voicebank_path: Path, device: str = "cpu") -> LinguisticModel:
    """Load linguistic encoder from voicebank."""
    dsdur_path = voicebank_path / "dsdur"
    if dsdur_path.exists():
        conf = yaml.safe_load((dsdur_path / "dsconfig.yaml").read_text())
        ling_path = dsdur_path / conf["linguistic"]
        return _get_model(LinguisticModel, ling_path.resolve(), device)
    raise FileNotFoundError("Linguistic model not found in voicebank")


def _load_duration_model(voicebank_path: Path, device: str = "cpu") -> DurationModel:
    """Load duration predictor from voicebank."""
    dsdur_path = voicebank_path / "dsdur"
    if dsdur_path.exists():
        conf = yaml.safe_load((dsdur_path / "dsconfig.yaml").read_text())
        dur_path = dsdur_path / conf["dur"]
        return _get_model(DurationModel, dur_path.resolve(), device)
    raise FileNotFoundError("Duration model not found in voicebank")


def _load_pitch_model(voicebank_path: Path, device: str = "cpu") -> Optional[PitchModel]:
    """Load pitch predictor from voicebank (optional)."""
    dspitch_path = voicebank_path / "dspitch"
    if dspitch_path.exists():
        conf = yaml.safe_load((dspitch_path / "dsconfig.yaml").read_text())
        pitch_path = dspitch_path / conf["pitch"]
        return _get_model(PitchModel, pitch_path.resolve(), device)
    return None


def _load_pitch_linguistic_model(
    voicebank_path: Path,
    device: str = "cpu",
) -> Optional[LinguisticModel]:
    """Load pitch-side linguistic encoder if available."""
    dspitch_path = voicebank_path / "dspitch"
    if dspitch_path.exists():
        conf = yaml.safe_load((dspitch_path / "dsconfig.yaml").read_text())
        ling_name = conf.get("linguistic")
        if ling_name:
            ling_path = dspitch_path / ling_name
            return _get_model(LinguisticModel, ling_path.resolve(), device)
    return None


def _load_variance_model(voicebank_path: Path, device: str = "cpu") -> Optional[VarianceModel]:
    """Load variance predictor from voicebank (optional)."""
    dsvariance_path = voicebank_path / "dsvariance"
    if dsvariance_path.exists():
        conf = yaml.safe_load((dsvariance_path / "dsconfig.yaml").read_text())
        if "variance" in conf:
            variance_path = dsvariance_path / conf["variance"]
            return _get_model(VarianceModel, variance_path.resolve(), device)
    return None


def _load_variance_linguistic_model(
    voicebank_path: Path,
    device: str = "cpu",
) -> Optional[LinguisticModel]:
    """Load variance-side linguistic encoder if available."""
    dsvariance_path = voicebank_path / "dsvariance"
    if dsvariance_path.exists():
        conf = yaml.safe_load((dsvariance_path / "dsconfig.yaml").read_text())
        ling_name = conf.get("linguistic")
        if ling_name:
            ling_path = dsvariance_path / ling_name
            return _get_model(LinguisticModel, ling_path.resolve(), device)
    return None


def _load_variance_config(voicebank_path: Path) -> Dict[str, Any]:
    """Load variance model config if available."""
    dsvariance_path = voicebank_path / "dsvariance"
    if not dsvariance_path.exists():
        return {}
    conf = yaml.safe_load((dsvariance_path / "dsconfig.yaml").read_text())
    return conf if isinstance(conf, dict) else {}


def _load_acoustic_model(voicebank_path: Path, device: str = "cpu") -> AcousticModel:
    """Load acoustic model from voicebank."""
    conf = yaml.safe_load((voicebank_path / "dsconfig.yaml").read_text())
    if "acoustic" in conf:
        return _get_model(AcousticModel, (voicebank_path / conf["acoustic"]).resolve(), device)
    dsmain = voicebank_path / "dsmain"
    if dsmain.exists():
        conf = yaml.safe_load((dsmain / "dsconfig.yaml").read_text())
        return _get_model(AcousticModel, (dsmain / conf["acoustic"]).resolve(), device)
    raise FileNotFoundError("Acoustic model not found in voicebank")


def predict_durations(
    phoneme_ids: List[int],
    word_boundaries: List[int],
    word_durations: List[float],
    word_pitches: List[float],
    voicebank: Union[str, Path],
    *,
    language_ids: Optional[List[int]] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Predict timing for each phoneme.
    
    Args:
        phoneme_ids: Token IDs from phonemize
        word_boundaries: Phoneme count per word from phonemize
        word_durations: Duration of each word group in frames
        word_pitches: MIDI pitch of each word group
        voicebank: Voicebank path
        language_ids: Language ID per phoneme (optional)
        device: Device to run inference on
        
    Returns:
        Dict with:
        - durations: Frames per phoneme
        - total_frames: Total audio frames
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "predict_durations input=%s",
            summarize_payload(
                {
                    "phoneme_ids": phoneme_ids,
                    "word_boundaries": word_boundaries,
                    "word_durations": word_durations,
                    "word_pitches": word_pitches,
                    "voicebank": str(voicebank),
                    "language_ids": language_ids,
                    "device": device,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    # Load models
    linguistic = _load_linguistic_model(voicebank_path, device)
    duration = _load_duration_model(voicebank_path, device)
    spk_embed = load_speaker_embed(voicebank_path)
    
    # Prepare tensors
    tokens_tensor = np.array(phoneme_ids, dtype=np.int64)[None, :]
    word_div_tensor = np.array(word_boundaries, dtype=np.int64)[None, :]
    word_dur_tensor = np.array(word_durations, dtype=np.int64)[None, :]
    
    if language_ids is None:
        language_ids = [0] * len(phoneme_ids)
    languages_tensor = np.array(language_ids, dtype=np.int64)[None, :]
    
    # Run linguistic encoder
    ling_inputs = {
        "tokens": tokens_tensor,
        "languages": languages_tensor if config.get("use_lang_id") else np.zeros_like(tokens_tensor),
        "word_div": word_div_tensor,
        "word_dur": word_dur_tensor,
    }
    ling_out = linguistic.run(ling_inputs)
    encoder_out = ling_out[0]
    x_masks = ling_out[1]
    
    # Expand note pitches to phoneme level
    ph_midi = []
    for midi, count in zip(word_pitches, word_boundaries):
        ph_midi.extend([int(round(midi))] * count)
    ph_midi_tensor = np.array(ph_midi, dtype=np.int64)[None, :]
    
    # Prepare speaker embedding
    spk_embed_tokens = np.repeat(spk_embed[None, None, :], tokens_tensor.shape[1], axis=1).astype(np.float32)
    
    # Run duration predictor
    duration_out = duration.forward(encoder_out, x_masks, ph_midi_tensor, spk_embed_tokens)
    ph_dur_pred = duration_out[0]
    
    # Align durations to match note timing
    ph_durations = _align_durations(ph_dur_pred, word_boundaries, [int(d) for d in word_durations])
    
    result = {
        "durations": ph_durations.tolist(),
        "total_frames": int(ph_durations.sum()),
        "encoder_out": encoder_out,  # Pass through for next steps
        "x_masks": x_masks,
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("predict_durations output=%s", summarize_payload(result))
    return result


def _align_durations(
    ph_dur_pred: np.ndarray,
    word_div: List[int],
    word_dur: List[int],
) -> np.ndarray:
    """Align predicted durations to match note timing."""
    ph2word = _build_ph2word(word_div)
    if ph2word.shape[0] != ph_dur_pred.shape[0]:
        raise ValueError("Phoneme durations and word_div are misaligned")
    
    ph_dur_pred = np.maximum(ph_dur_pred, 0.0)
    word_dur_in = np.zeros(len(word_div), dtype=np.float32)
    for ph_idx, word_idx in enumerate(ph2word):
        word_dur_in[word_idx - 1] += ph_dur_pred[ph_idx]
    
    alpha = np.ones_like(word_dur_in)
    for idx, total in enumerate(word_dur_in):
        if total > 0:
            alpha[idx] = word_dur[idx] / total
    
    ph_dur = np.round(ph_dur_pred * alpha[ph2word - 1]).astype(np.int64)
    ph_dur = np.maximum(ph_dur, 1)
    
    # Adjust rounding drift
    offset = 0
    for word_idx, count in enumerate(word_div):
        target = word_dur[word_idx]
        span = ph_dur[offset:offset + count]
        diff = target - int(span.sum())
        if diff != 0 and count > 0:
            span[-1] = max(1, span[-1] + diff)
            ph_dur[offset:offset + count] = span
        offset += count
    
    return ph_dur


def _build_ph2word(word_div: List[int]) -> np.ndarray:
    """Build phoneme-to-word index mapping."""
    ph2word = []
    for idx, count in enumerate(word_div, start=1):
        ph2word.extend([idx] * count)
    return np.array(ph2word, dtype=np.int64)


def predict_pitch(
    phoneme_ids: List[int],
    durations: List[int],
    note_pitches: List[float],
    note_durations: List[int],
    note_rests: List[bool],
    voicebank: Union[str, Path],
    *,
    language_ids: Optional[List[int]] = None,
    encoder_out: Optional[np.ndarray] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Generate natural pitch curves.
    
    Args:
        phoneme_ids: Token IDs
        durations: Frames per phoneme
        note_pitches: MIDI pitch per note
        note_durations: Frames per note
        note_rests: Whether each note is a rest
        voicebank: Voicebank path
        language_ids: Language ID per phoneme (optional)
        encoder_out: Encoder output from predict_durations (optional)
        device: Device for inference
        
    Returns:
        Dict with:
        - f0: Pitch curve in Hz (one value per frame)
        - pitch_midi: Pitch curve in MIDI notes
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "predict_pitch input=%s",
            summarize_payload(
                {
                    "phoneme_ids": phoneme_ids,
                    "durations": durations,
                    "note_pitches": note_pitches,
                    "note_durations": note_durations,
                    "note_rests": note_rests,
                    "voicebank": str(voicebank),
                    "language_ids": language_ids,
                    "encoder_out": encoder_out,
                    "device": device,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    n_frames = sum(durations)
    
    # Try to load pitch model
    pitch_model = _load_pitch_model(voicebank_path, device)
    
    if pitch_model is None:
        # Fallback: naive MIDI-derived pitch
        f0 = _naive_pitch(note_pitches, note_durations, note_rests)
        result = {
            "f0": f0[:n_frames].tolist(),
            "pitch_midi": _hz_to_midi(f0[:n_frames]).tolist(),
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("predict_pitch output=%s", summarize_payload(result))
        return result
    
    pitch_linguistic = _load_pitch_linguistic_model(voicebank_path, device)
    spk_embed = load_speaker_embed(voicebank_path)

    tokens_tensor = np.array(phoneme_ids, dtype=np.int64)[None, :]
    durations_tensor = np.array(durations, dtype=np.int64)[None, :]
    if language_ids is None:
        language_ids = [0] * len(phoneme_ids)
    languages_tensor = np.array(language_ids, dtype=np.int64)[None, :]

    if pitch_linguistic is not None:
        pitch_ling_inputs = {
            "tokens": tokens_tensor,
            "languages": languages_tensor if config.get("use_lang_id") else np.zeros_like(tokens_tensor),
            "ph_dur": durations_tensor,
        }
        pitch_encoder_out = pitch_linguistic.run(pitch_ling_inputs)[0]
    elif encoder_out is not None:
        pitch_encoder_out = encoder_out
    else:
        f0 = _naive_pitch(note_pitches, note_durations, note_rests)
        result = {
            "f0": f0[:n_frames].tolist(),
            "pitch_midi": _hz_to_midi(f0[:n_frames]).tolist(),
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("predict_pitch output=%s", summarize_payload(result))
        return result

    note_midi = np.array(note_pitches, dtype=np.float32)[None, :]
    note_rest = np.array(note_rests, dtype=bool)[None, :]
    note_dur = np.array(note_durations, dtype=np.int64)[None, :]
    pitch = np.full((1, n_frames), 60.0, dtype=np.float32)
    expr = np.ones((1, n_frames), dtype=np.float32)
    retake = np.ones((1, n_frames), dtype=bool)
    spk_embed_frames = np.repeat(spk_embed[None, None, :], n_frames, axis=1).astype(np.float32)

    pitch_inputs = {
        "encoder_out": pitch_encoder_out,
        "ph_dur": durations_tensor,
        "note_midi": note_midi,
        "note_rest": note_rest,
        "note_dur": note_dur,
        "pitch": pitch,
        "expr": expr,
        "retake": retake,
        "spk_embed": spk_embed_frames,
        "steps": np.array(config.get("steps", 10), dtype=np.int64),
    }

    pitch_pred = pitch_model.run(pitch_inputs)[0]
    pitch_midi = pitch_pred.astype(np.float32)
    f0 = 440.0 * (2.0 ** ((pitch_midi - 69.0) / 12.0))
    result = {
        "f0": f0[0].tolist(),
        "pitch_midi": pitch_midi[0].tolist(),
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("predict_pitch output=%s", summarize_payload(result))
    return result


def _naive_pitch(
    note_pitches: List[float],
    note_durations: List[int],
    note_rests: List[bool],
) -> np.ndarray:
    """Generate naive F0 curve from MIDI notes."""
    f0 = []
    for midi, dur, is_rest in zip(note_pitches, note_durations, note_rests):
        if is_rest or midi <= 0:
            freq = 0.0
        else:
            freq = 440.0 * (2.0 ** ((midi - 69.0) / 12.0))
        f0.extend([freq] * dur)
    return np.array(f0, dtype=np.float32)


def _hz_to_midi(f0: np.ndarray) -> np.ndarray:
    """Convert Hz to MIDI note numbers."""
    with np.errstate(divide="ignore", invalid="ignore"):
        midi = 69.0 + 12.0 * np.log2(f0 / 440.0)
        midi = np.where(f0 > 0, midi, 0.0)
    return midi


def predict_variance(
    phoneme_ids: List[int],
    durations: List[int],
    f0: List[float],
    voicebank: Union[str, Path],
    *,
    language_ids: Optional[List[int]] = None,
    encoder_out: Optional[np.ndarray] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Generate expressive parameters (breathiness, tension, voicing).
    
    Args:
        phoneme_ids: Token IDs
        durations: Frames per phoneme
        f0: Pitch curve in Hz
        voicebank: Voicebank path
        language_ids: Language ID per phoneme (optional)
        encoder_out: Encoder output from predict_durations (optional)
        device: Device for inference
        
    Returns:
        Dict with:
        - breathiness: Breathiness curve (0.0-1.0 per frame)
        - tension: Tension curve
        - voicing: Voicing curve
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "predict_variance input=%s",
            summarize_payload(
                {
                    "phoneme_ids": phoneme_ids,
                    "durations": durations,
                    "f0": f0,
                    "voicebank": str(voicebank),
                    "language_ids": language_ids,
                    "encoder_out": encoder_out,
                    "device": device,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    n_frames = len(f0)
    
    # Try to load variance model
    variance_model = _load_variance_model(voicebank_path, device)
    
    if variance_model is None:
        # Fallback: zeros
        result = {
            "breathiness": [0.0] * n_frames,
            "tension": [0.0] * n_frames,
            "voicing": [0.0] * n_frames,
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("predict_variance output=%s", summarize_payload(result))
        return result

    config = load_voicebank_config(voicebank_path)
    variance_conf = _load_variance_config(voicebank_path)
    variance_linguistic = _load_variance_linguistic_model(voicebank_path, device)
    spk_embed = load_speaker_embed(voicebank_path)

    tokens_tensor = np.array(phoneme_ids, dtype=np.int64)[None, :]
    durations_tensor = np.array(durations, dtype=np.int64)[None, :]
    if language_ids is None:
        language_ids = [0] * len(phoneme_ids)
    languages_tensor = np.array(language_ids, dtype=np.int64)[None, :]

    if variance_linguistic is not None:
        ling_inputs = {
            "tokens": tokens_tensor,
            "languages": languages_tensor if config.get("use_lang_id") else np.zeros_like(tokens_tensor),
            "ph_dur": durations_tensor,
        }
        variance_encoder_out = variance_linguistic.run(ling_inputs)[0]
    elif encoder_out is not None:
        variance_encoder_out = encoder_out
    else:
        result = {
            "breathiness": [0.0] * n_frames,
            "tension": [0.0] * n_frames,
            "voicing": [0.0] * n_frames,
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("predict_variance output=%s", summarize_payload(result))
        return result

    pitch_midi = _hz_to_midi(np.array(f0, dtype=np.float32))
    pitch_midi = pitch_midi.astype(np.float32)[None, :]

    predict_energy = bool(variance_conf.get("predict_energy", False))
    predict_breathiness = bool(variance_conf.get("predict_breathiness", False))
    predict_voicing = bool(variance_conf.get("predict_voicing", False))
    predict_tension = bool(variance_conf.get("predict_tension", False))
    num_variances = sum(
        [
            int(predict_energy),
            int(predict_breathiness),
            int(predict_voicing),
            int(predict_tension),
        ]
    )
    if num_variances <= 0:
        num_variances = 3

    spk_embed_frames = np.repeat(spk_embed[None, None, :], n_frames, axis=1).astype(np.float32)
    variance_inputs = {
        "encoder_out": variance_encoder_out,
        "ph_dur": durations_tensor,
        "pitch": pitch_midi,
        "breathiness": np.zeros((1, n_frames), dtype=np.float32),
        "voicing": np.zeros((1, n_frames), dtype=np.float32),
        "tension": np.zeros((1, n_frames), dtype=np.float32),
        "retake": np.ones((1, n_frames, num_variances), dtype=bool),
        "spk_embed": spk_embed_frames,
        "steps": np.array(config.get("steps", 10), dtype=np.int64),
    }
    if predict_energy:
        variance_inputs["energy"] = np.zeros((1, n_frames), dtype=np.float32)

    variance_out = variance_model.run(variance_inputs)
    if predict_energy and len(variance_out) >= 4:
        _, breathiness, voicing, tension = variance_out[:4]
    elif len(variance_out) >= 3:
        breathiness, voicing, tension = variance_out[:3]
    else:
        result = {
            "breathiness": [0.0] * n_frames,
            "tension": [0.0] * n_frames,
            "voicing": [0.0] * n_frames,
        }
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("predict_variance output=%s", summarize_payload(result))
        return result

    breathiness = np.squeeze(breathiness).astype(np.float32)
    voicing = np.squeeze(voicing).astype(np.float32)
    tension = np.squeeze(tension).astype(np.float32)

    result = {
        "breathiness": breathiness.tolist(),
        "tension": tension.tolist(),
        "voicing": voicing.tolist(),
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("predict_variance output=%s", summarize_payload(result))
    return result


def synthesize_mel(
    phoneme_ids: List[int],
    durations: List[int],
    f0: List[float],
    voicebank: Union[str, Path],
    *,
    breathiness: Optional[List[float]] = None,
    tension: Optional[List[float]] = None,
    voicing: Optional[List[float]] = None,
    language_ids: Optional[List[int]] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Generate the Mel spectrogram.
    
    Args:
        phoneme_ids: Token IDs
        durations: Frames per phoneme
        f0: Pitch curve in Hz
        voicebank: Voicebank path
        breathiness, tension, voicing: Variance curves (optional)
        language_ids: Language ID per phoneme (optional)
        device: Device for inference
        
    Returns:
        Dict with:
        - mel: Mel spectrogram [frames × mel_bins]
        - sample_rate: Audio sample rate
        - hop_size: Samples per frame
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "synthesize_mel input=%s",
            summarize_payload(
                {
                    "phoneme_ids": phoneme_ids,
                    "durations": durations,
                    "f0": f0,
                    "voicebank": str(voicebank),
                    "breathiness": breathiness,
                    "tension": tension,
                    "voicing": voicing,
                    "language_ids": language_ids,
                    "device": device,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    acoustic = _load_acoustic_model(voicebank_path, device)
    spk_embed = load_speaker_embed(voicebank_path)
    
    n_frames = len(f0)
    
    # Prepare tensors
    tokens_tensor = np.array(phoneme_ids, dtype=np.int64)[None, :]
    durations_tensor = np.array(durations, dtype=np.int64)[None, :]
    f0_tensor = np.array(f0, dtype=np.float32)[None, :]
    
    if language_ids is None:
        language_ids = [0] * len(phoneme_ids)
    languages_tensor = np.array(language_ids, dtype=np.int64)[None, :]
    
    # Variance defaults
    if breathiness is None:
        breathiness = [0.0] * n_frames
    if tension is None:
        tension = [0.0] * n_frames
    if voicing is None:
        voicing = [0.0] * n_frames
    
    spk_embed_frames = np.repeat(spk_embed[None, None, :], n_frames, axis=1).astype(np.float32)
    depth = float(config.get("max_depth", 1.0)) if config.get("use_variable_depth") else 1.0
    
    acoustic_inputs = {
        "tokens": tokens_tensor,
        "languages": languages_tensor if config.get("use_lang_id") else np.zeros_like(tokens_tensor),
        "durations": durations_tensor,
        "f0": f0_tensor,
        "breathiness": np.array(breathiness, dtype=np.float32)[None, :],
        "voicing": np.array(voicing, dtype=np.float32)[None, :],
        "tension": np.array(tension, dtype=np.float32)[None, :],
        "gender": np.zeros((1, n_frames), dtype=np.float32),
        "velocity": np.ones((1, n_frames), dtype=np.float32),
        "spk_embed": spk_embed_frames,
        "depth": np.array(depth, dtype=np.float32),
        "steps": np.array(config.get("steps", 10), dtype=np.int64),
    }
    
    mel = acoustic.run(acoustic_inputs)[0]
    
    result = {
        "mel": mel.tolist(),
        "sample_rate": config.get("sample_rate", 44100),
        "hop_size": config.get("hop_size", 512),
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("synthesize_mel output=%s", summarize_payload(result))
    return result


def synthesize_audio(
    phoneme_ids: List[int],
    durations: List[int],
    f0: List[float],
    voicebank: Union[str, Path],
    *,
    breathiness: Optional[List[float]] = None,
    tension: Optional[List[float]] = None,
    voicing: Optional[List[float]] = None,
    language_ids: Optional[List[int]] = None,
    vocoder_path: Optional[Union[str, Path]] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Generate audio by running mel synthesis and vocoding.
    
    Args:
        phoneme_ids: Token IDs
        durations: Frames per phoneme
        f0: Pitch curve in Hz
        voicebank: Voicebank path or ID
        breathiness, tension, voicing: Variance curves (optional)
        language_ids: Language ID per phoneme (optional)
        vocoder_path: Optional explicit vocoder path
        device: Device for inference
        
    Returns:
        Dict with:
        - waveform: Audio samples as list
        - sample_rate: Audio sample rate
        - hop_size: Samples per frame
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "synthesize_audio input=%s",
            summarize_payload(
                {
                    "phoneme_ids": phoneme_ids,
                    "durations": durations,
                    "f0": f0,
                    "voicebank": str(voicebank),
                    "breathiness": breathiness,
                    "tension": tension,
                    "voicing": voicing,
                    "language_ids": language_ids,
                    "vocoder_path": str(vocoder_path) if vocoder_path else None,
                    "device": device,
                }
            ),
        )
    mel_result = synthesize_mel(
        phoneme_ids=phoneme_ids,
        durations=durations,
        f0=f0,
        voicebank=voicebank,
        breathiness=breathiness,
        tension=tension,
        voicing=voicing,
        language_ids=language_ids,
        device=device,
    )
    audio_result = vocode(
        mel=mel_result["mel"],
        f0=f0,
        voicebank=voicebank,
        vocoder_path=vocoder_path,
        device=device,
    )
    result = {
        "waveform": audio_result["waveform"],
        "sample_rate": audio_result["sample_rate"],
        "hop_size": mel_result["hop_size"],
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("synthesize_audio output=%s", summarize_payload(result))
    return result
