"""
SVS Backend API Module

This module exposes the public APIs for singing voice synthesis.
"""

from src.api.score import parse_score, modify_score
from src.api.phonemize import phonemize
from src.api.inference import predict_durations, predict_pitch, predict_variance, synthesize_audio
from src.api.audio import save_audio
from src.api.backing_track_prompt import (
    build_elevenlabs_backing_track_prompt,
    build_elevenlabs_backing_track_prompt_from_metadata,
    extract_backing_track_metadata,
)
from src.api.synthesize import align_phonemes_to_notes, synthesize
from src.api.voicebank import list_voicebanks, get_voicebank_info
from src.api.voice_parts import preprocess_voice_parts

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
    "preprocess_voice_parts",
    "build_elevenlabs_backing_track_prompt",
    "build_elevenlabs_backing_track_prompt_from_metadata",
    "extract_backing_track_metadata",
    # Metadata
    "list_voicebanks",
    "get_voicebank_info",
]
