"""
Phonemization API.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.phonemizer.phonemizer import Phonemizer
from src.api.voicebank import load_voicebank_config
from src.api.voicebank_cache import (
    resolve_manifest_japanese_dictionary_form,
    resolve_manifest_pronunciation_adapters,
)
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


_SOLFEGE_ENGLISH_PRONUNCIATIONS = {
    "do": "doh",
    "di": "dee",
    "ra": "rah",
    "re": "ray",
    "ri": "ree",
    "me": "may",
    "mi": "mee",
    "fa": "fah",
    "fi": "fee",
    "se": "say",
    "so": "soh",
    "sol": "soh",
    "si": "see",
    "le": "lay",
    "la": "lah",
    "li": "lee",
    "te": "tay",
    "ti": "tee",
}


def patch_solfege_pronunciations(lyrics: List[str]) -> List[str]:
    """Return English singing spellings for recognized whole-token solfege lyrics."""
    return [
        _SOLFEGE_ENGLISH_PRONUNCIATIONS.get(lyric.strip().casefold(), lyric)
        for lyric in lyrics
    ]


def phonemize(
    lyrics: List[str],
    voicebank: Union[str, Path],
    *,
    language: str = "en",
    solfege_pronunciation_patch: bool = False,
    logical_pronunciation: bool = False,
) -> Dict[str, Any]:
    """
    Convert lyrics to phoneme sequences.
    
    Args:
        lyrics: List of lyric strings (one per note/word)
        voicebank: Voicebank path or ID
        language: Language code (default: "en")
        solfege_pronunciation_patch: Apply deterministic English solfege spellings
        logical_pronunciation: Keep manifest-declared multi-phone workarounds as
            logical markers for the alignment layer. Markers are never valid
            model tokens and must be expanded before model inputs are built.
        
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
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "phonemize input=%s",
            summarize_payload(
                {
                    "lyrics": lyrics,
                    "voicebank": str(voicebank),
                    "language": language,
                    "solfege_pronunciation_patch": solfege_pronunciation_patch,
                    "logical_pronunciation": logical_pronunciation,
                }
            ),
        )
    if not isinstance(solfege_pronunciation_patch, bool):
        raise ValueError("solfege_pronunciation_patch must be a boolean.")
    if not isinstance(logical_pronunciation, bool):
        raise ValueError("logical_pronunciation must be a boolean.")

    effective_lyrics = list(lyrics)
    if solfege_pronunciation_patch and language.strip().lower() == "en":
        effective_lyrics = patch_solfege_pronunciations(effective_lyrics)

    voicebank_path = Path(voicebank)
    # Load voicebank config to locate phoneme and language assets.
    config = load_voicebank_config(voicebank_path)
    
    # Resolve paths from config.
    phonemes_path = (voicebank_path / config.get("phonemes", "phonemes.json")).resolve()
    languages_path = None
    if "languages" in config:
        languages_path = (voicebank_path / config["languages"]).resolve()
    
    # Find dictionary for token-to-phoneme lookup.
    dictionary_path = _find_dictionary(voicebank_path, language=language)
    
    # Build phonemizer with fallback G2P enabled.
    phonemizer = Phonemizer(
        phonemes_path=phonemes_path,
        dictionary_path=dictionary_path,
        languages_path=languages_path,
        language=language,
        allow_g2p=True,
        # The phonemizer prepares this full sequence before deciding which
        # dictionary entries it needs (important for Chinese phrase context).
        needed_graphemes=effective_lyrics,
        japanese_dictionary_form=resolve_manifest_japanese_dictionary_form(voicebank_path),
    )
    phoneme_result = phonemizer.phonemize_tokens(effective_lyrics)
    result = {
        "phonemes": list(phoneme_result.phonemes),
        "phoneme_ids": list(phoneme_result.ids),
        "language_ids": list(phoneme_result.language_ids),
        "word_boundaries": list(phoneme_result.word_boundaries),
    }
    if logical_pronunciation:
        adapters = resolve_manifest_pronunciation_adapters(voicebank_path, language)
        if adapters:
            result = _collapse_logical_pronunciation(result, adapters)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("phonemize output=%s", summarize_payload(result))
    return result


def _collapse_logical_pronunciation(
    result: Dict[str, Any],
    adapters: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Collapse configured runtime-phone spans into alignment-only markers."""
    phonemes = list(result.get("phonemes") or [])
    ids = list(result.get("phoneme_ids") or [])
    language_ids = list(result.get("language_ids") or [])
    boundaries = list(result.get("word_boundaries") or [])
    if not phonemes or not boundaries:
        return result

    collapsed_phonemes: List[str] = []
    collapsed_ids: List[int] = []
    collapsed_language_ids: List[int] = []
    collapsed_boundaries: List[int] = []
    offset = 0
    for count in boundaries:
        end = offset + int(count)
        word_phonemes = phonemes[offset:end]
        word_ids = ids[offset:end]
        word_language_ids = language_ids[offset:end]
        offset = end
        idx = 0
        collapsed_count = 0
        while idx < len(word_phonemes):
            adapter = next(
                (
                    candidate
                    for candidate in adapters
                    if word_phonemes[idx : idx + len(candidate["collapse_phonemes"])]
                    == candidate["collapse_phonemes"]
                ),
                None,
            )
            if adapter is None:
                collapsed_phonemes.append(word_phonemes[idx])
                collapsed_ids.append(word_ids[idx])
                collapsed_language_ids.append(word_language_ids[idx])
                idx += 1
                collapsed_count += 1
                continue
            collapsed_phonemes.append(str(adapter["logical_symbol"]))
            # Alignment expands this marker before any model stage. The sentinel
            # prevents accidental use as a model token if that contract is broken.
            collapsed_ids.append(-1)
            collapsed_language_ids.append(0)
            idx += len(adapter["collapse_phonemes"])
            collapsed_count += 1
        collapsed_boundaries.append(collapsed_count)

    if offset != len(phonemes):
        raise ValueError("word_boundaries do not consume phonemize output")
    return {
        **result,
        "phonemes": collapsed_phonemes,
        "phoneme_ids": collapsed_ids,
        "language_ids": collapsed_language_ids,
        "word_boundaries": collapsed_boundaries,
    }


def _dictionary_candidates(voicebank_path: Path, language: str = "en") -> List[Path]:
    """Return dictionary candidates in the order they should be checked."""
    language = (language or "en").strip()
    language_specific = f"dsdict-{language}.yaml" if language else "dsdict-en.yaml"
    return [
        voicebank_path / "dsvariance" / language_specific,
        voicebank_path / "dsdur" / language_specific,
        voicebank_path / "dsvariance" / "dsdict.yaml",
        voicebank_path / "dsdur" / "dsdict.yaml",
        voicebank_path / language_specific,
        voicebank_path / "dsdict.yaml",
    ]


def _find_dictionary(voicebank_path: Path, language: str = "en") -> Path:
    """Find phoneme dictionary in voicebank."""
    candidates = _dictionary_candidates(voicebank_path, language=language)
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(
        f"Could not find phoneme dictionary for language '{language}' in {voicebank_path}"
    )
