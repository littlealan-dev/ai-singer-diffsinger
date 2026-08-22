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

from .openutau_es_g2p import OpenUtauFrenchMillefeuilleG2p, OpenUtauSpanishG2p


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


class DiffSingerFrenchMillefeuillePhonemizer:
    """French fallback using OpenUtau's ``g2p-fr-millefeuille`` pack."""

    language = "fr"

    def __init__(self) -> None:
        self._g2p = OpenUtauFrenchMillefeuilleG2p()

    def phonemize(self, token: str) -> Sequence[str]:
        return self._g2p.phonemize(token)


class DiffSingerCantoneseJyutpingPhonemizer:
    """Map tone-free Jyutping syllables to LIEE's shared phone inventory.

    OpenUtau's DIFFS ZH-YUE phonemizer first converts Hanzi to tone-free
    Jyutping. LIEE's shipped ``dsdict-zh-yue.yaml`` supplies the common phone
    symbols but no Jyutping word entries, so SightSinger expands each Jyutping
    syllable here before applying the dictionary's replacements.
    """

    language = "zh-yue"
    _ONSETS = (
        ("gw", ("g", "w")),
        ("kw", ("k", "w")),
        ("ng", ("ng",)),
        ("b", ("b",)), ("p", ("p",)), ("m", ("m",)), ("f", ("f",)),
        ("d", ("d",)), ("t", ("t",)), ("n", ("n",)), ("l", ("l",)),
        ("g", ("g",)), ("k", ("k",)), ("h", ("h",)), ("w", ("w",)),
        ("z", ("dz",)), ("c", ("cz",)), ("s", ("s",)), ("j", ("j",)),
    )
    _RIMES = {
        "aap": ("aa", "p"), "aat": ("aa", "t"), "aak": ("aa", "k"),
        "aam": ("aa", "m"), "aan": ("aa", "n"), "aang": ("aa", "ng"),
        "aai": ("aa", "y"), "aau": ("aa", "w"), "aa": ("aa",),
        "ap": ("a", "p"), "at": ("a", "t"), "ak": ("a", "k"),
        "am": ("a", "m"), "an": ("a", "n"), "ang": ("a", "ng"),
        "ai": ("a", "y"), "au": ("a", "w"), "a": ("a",),
        "oet": ("E", "t"), "oek": ("E", "k"), "oeng": ("E", "ng"),
        "oen": ("E", "n"), "oei": ("E", "y"), "oe": ("E",),
        "eot": ("eh", "t"), "eon": ("eh", "n"), "eoi": ("eh", "y"),
        "eo": ("eh",),
        "ep": ("e", "p"), "et": ("e", "t"), "ek": ("e", "k"),
        "em": ("e", "m"), "en": ("e", "n"), "eng": ("e", "ng"),
        "ei": ("e", "y"), "eu": ("e", "w"), "e": ("e",),
        "op": ("o", "p"), "ot": ("o", "t"), "ok": ("o", "k"),
        "om": ("o", "m"), "on": ("o", "n"), "ong": ("o", "ng"),
        "oi": ("o", "y"), "ou": ("o", "w"), "o": ("o",),
        "yut": ("y", "u", "t"), "yun": ("y", "u", "n"), "yung": ("y", "u", "ng"),
        "yu": ("y", "u"),
        "ip": ("i", "p"), "it": ("i", "t"), "ik": ("i", "k"),
        "im": ("i", "m"), "in": ("i", "n"), "ing": ("i", "ng"),
        "iu": ("i", "w"), "i": ("i",),
        "up": ("u", "p"), "ut": ("u", "t"), "uk": ("u", "k"),
        "um": ("u", "m"), "un": ("u", "n"), "ung": ("u", "ng"),
        "ui": ("u", "y"), "u": ("u",),
        "m": ("m",), "ng": ("ng",),
    }

    def phonemize(self, token: str) -> Sequence[str]:
        syllable = str(token).lower().rstrip("123456")
        for onset, phones in self._ONSETS:
            if syllable.startswith(onset) and syllable != onset:
                rime = syllable[len(onset):]
                mapped_rime = self._RIMES.get(rime)
                if mapped_rime is not None:
                    return (*phones, *mapped_rime)
        return self._RIMES.get(syllable, ())
