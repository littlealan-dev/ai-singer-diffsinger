"""
SVS Backend API Module

This module exposes the public APIs for singing voice synthesis.
"""

from src.api.score import parse_score, modify_score
from src.api.phonemize import phonemize
from src.api.inference import predict_durations, predict_pitch, predict_variance, synthesize_audio
from src.api.audio import save_audio
from src.api.synthesize import align_phonemes_to_notes, synthesize
from src.api.voicebank import list_voicebanks, get_voicebank_info

__all__ = [
    # Step 1: Score
    "parse_score",
    "modify_score",
    # Step 2: Phoneme prep
    "phonemize",
    "align_phonemes_to_notes",
    # Steps 3-6: Inference
    "predict_durations",
    "predict_pitch",
    "predict_variance",
    "synthesize_audio",
    # Output
    "save_audio",
    # Convenience
    "synthesize",
    # Metadata
    "list_voicebanks",
    "get_voicebank_info",
]
