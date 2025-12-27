"""
Phonemization API.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.phonemizer.phonemizer import Phonemizer
from src.api.voicebank import load_voicebank_config


def phonemize(
    lyrics: List[str],
    voicebank: Union[str, Path],
    *,
    language: str = "en",
) -> Dict[str, Any]:
    """
    Convert lyrics to phoneme sequences.
    
    Args:
        lyrics: List of lyric strings (one per note/word)
        voicebank: Voicebank path or ID
        language: Language code (default: "en")
        
    Returns:
        Dict with:
        - phonemes: List of phoneme strings
        - phoneme_ids: List of token IDs
        - language_ids: List of language IDs
        - word_boundaries: List of phoneme counts per word
        
    Example:
        phonemize(["hello", "world"], "Raine_Rena")
        → {
            "phonemes": ["hh", "ah", "l", "ow", "w", "er", "l", "d"],
            "phoneme_ids": [15, 4, 21, 32, 45, 12, 21, 8],
            "language_ids": [1, 1, 1, 1, 1, 1, 1, 1],
            "word_boundaries": [4, 4]
          }
    """
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    # Resolve paths from config
    phonemes_path = (voicebank_path / config.get("phonemes", "phonemes.json")).resolve()
    languages_path = None
    if "languages" in config:
        languages_path = (voicebank_path / config["languages"]).resolve()
    
    # Find dictionary
    dictionary_path = _find_dictionary(voicebank_path)
    
    phonemizer = Phonemizer(
        phonemes_path=phonemes_path,
        dictionary_path=dictionary_path,
        languages_path=languages_path,
        language=language,
        allow_g2p=True,
    )
    
    all_phonemes: List[str] = []
    all_ids: List[int] = []
    all_lang_ids: List[int] = []
    word_boundaries: List[int] = []
    
    for lyric in lyrics:
        result = phonemizer.phonemize_tokens([lyric])
        all_phonemes.extend(result.phonemes)
        all_ids.extend(result.ids)
        all_lang_ids.extend(result.language_ids)
        word_boundaries.append(len(result.phonemes))
    
    return {
        "phonemes": all_phonemes,
        "phoneme_ids": all_ids,
        "language_ids": all_lang_ids,
        "word_boundaries": word_boundaries,
    }


def _find_dictionary(voicebank_path: Path) -> Path:
    """Find phoneme dictionary in voicebank."""
    candidates = [
        voicebank_path / "dsvariance" / "dsdict.yaml",
        voicebank_path / "dsdur" / "dsdict.yaml",
        voicebank_path / "dsdur" / "dsdict-en.yaml",
        voicebank_path / "dsdict.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(
        f"Could not find phoneme dictionary in {voicebank_path}"
    )
