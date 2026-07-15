"""Language-specific G2P providers used after voicebank dictionary lookup.

Voicebank dictionaries remain the first pronunciation source.  This registry
only selects a fallback when a word is absent from that dictionary, so adding a
language is an explicit provider registration rather than a conditional in the
generic :class:`Phonemizer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata
from typing import Optional, Protocol, Sequence

from g2p_en import G2p

from .openutau_es_g2p import OpenUtauSpanishG2p


ARPABET_TO_VOICEBANK = {
    "AA": "aa", "AE": "ae", "AH": "ah", "AO": "ao", "AW": "aw",
    "AX": "ax", "AXR": "er", "AY": "ay", "B": "b", "CH": "ch",
    "D": "d", "DH": "dh", "DX": "dx", "EH": "eh", "ER": "er",
    "EY": "ey", "F": "f", "G": "g", "HH": "hh", "IH": "ih",
    "IX": "ih", "IY": "iy", "JH": "jh", "K": "k", "L": "l",
    "M": "m", "N": "n", "NG": "ng", "OW": "ow", "OY": "oy",
    "P": "p", "R": "r", "S": "s", "SH": "sh", "T": "t",
    "TH": "th", "UH": "uh", "UW": "uw", "UX": "uw", "V": "v",
    "W": "w", "Y": "y", "Z": "z", "ZH": "zh",
}


@dataclass(frozen=True)
class G2pInputError(ValueError):
    """Structured input failure from a language G2P provider."""

    reason: str
    normalized_token: str = ""
    unsupported_character: str = ""
    unsupported_character_name: str = ""
    unsupported_script: str = ""


class LanguageG2pProvider(Protocol):
    """Produces language-neutral voicebank phoneme symbols for a lyric token."""

    language: str

    def phonemize(self, token: str) -> Sequence[str]:
        """Return bare phoneme symbols; the caller applies voicebank mappings."""


def normalize_word_for_english_g2p(value: str) -> str:
    """Normalize a Latin word for the English G2P implementation."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Za-z']+", "", without_marks).lower()


def first_non_latin_letter(value: str) -> Optional[tuple[str, str, str]]:
    """Return the first alphabetic character outside the Latin script."""
    for character in value:
        if not character.isalpha():
            continue
        character_name = unicodedata.name(character, "")
        if "LATIN" in character_name:
            continue
        script = character_name.split(" ", 1)[0].title() if character_name else "Unknown"
        return character, character_name, script
    return None


class DiffSingerEnglishPhonemizer:
    """English fallback using g2p_en, expressed as bare voicebank symbols."""

    language = "en"

    @staticmethod
    @lru_cache(maxsize=1)
    def _g2p() -> G2p:
        try:
            return G2p()
        except LookupError as exc:
            raise RuntimeError(
                "g2p_en requires the NLTK cmudict corpus. "
                "Install it with: python -m nltk.downloader cmudict"
            ) from exc

    def phonemize(self, token: str) -> Sequence[str]:
        unsupported = first_non_latin_letter(token)
        if unsupported is not None:
            character, character_name, script = unsupported
            raise G2pInputError(
                reason="non_latin_lyrics_for_english_g2p",
                unsupported_character=character,
                unsupported_character_name=character_name,
                unsupported_script=script,
            )
        normalized = normalize_word_for_english_g2p(token)
        if not normalized:
            raise G2pInputError(
                reason="invalid_lyric_token_for_g2p",
                normalized_token=normalized,
            )
        phones = [phone for phone in self._g2p()(normalized) if re.search(r"[A-Za-z]", phone)]
        if not phones:
            return ()
        mapped = []
        for phone in phones:
            arpabet = re.sub(r"[0-9]", "", phone).upper()
            if arpabet not in ARPABET_TO_VOICEBANK:
                raise KeyError(f"Unsupported ARPABET symbol '{phone}' in G2P output.")
            mapped.append(ARPABET_TO_VOICEBANK[arpabet])
        return tuple(mapped)


class DiffSingerSpanishPhonemizer:
    """Spanish fallback using OpenUtau's bundled ``g2p-es`` pack."""

    language = "es"

    def __init__(self) -> None:
        self._g2p = OpenUtauSpanishG2p()

    def phonemize(self, token: str) -> Sequence[str]:
        return self._g2p.phonemize(token)
