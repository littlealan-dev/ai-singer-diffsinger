"""Phrase-aware language preparation and optional G2P fallback selection.

This mirrors OpenUtau's DiffSinger flow: normalize/romanize a lyric phrase
first, look up the prepared form in the voicebank dictionary, then use a word
G2P fallback only for languages that provide one.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import string
from typing import Dict, Optional, Protocol, Sequence
import unicodedata

from pykakasi import kakasi
from pypinyin import Style, lazy_pinyin

from .language_g2p import (
    DiffSingerEnglishPhonemizer,
    DiffSingerSpanishPhonemizer,
    LanguageG2pProvider,
)


@dataclass(frozen=True)
class PreparedLyric:
    """A source lyric and the form used for voicebank dictionary lookup."""

    original: str
    lookup: str
    fallback_lookups: tuple[str, ...] = ()


def _is_internal_english_apostrophe(value: str, index: int, language: str) -> bool:
    """Keep apostrophes only when they join two letters in an English word."""
    return (
        language.split("-", 1)[0] == "en"
        and value[index] in {"'", "’"}
        and index > 0
        and index + 1 < len(value)
        and value[index - 1].isalpha()
        and value[index + 1].isalpha()
    )


def prepare_lookup_lyric(token: str, *, language: str) -> str:
    """Remove display-only numbers and punctuation before pronunciation lookup.

    The raw lyric remains in :class:`PreparedLyric` for score display and
    diagnostics. Numeric notation is deliberately not expanded: a score must
    use a singable spelling whose syllables match its sounding notes.
    """
    # Filter before NFKC so compatibility numerals such as ``Ⅶ`` cannot expand
    # into letters and avoid removal. A second numeric check below covers any
    # numeric character introduced by normalization.
    without_numbers = "".join(
        character
        for character in str(token)
        if not unicodedata.category(character).startswith("N")
    )
    normalized = unicodedata.normalize("NFKC", without_numbers)
    cleaned = []
    for index, character in enumerate(normalized):
        if _is_internal_english_apostrophe(normalized, index, language):
            cleaned.append("'")
        elif (
            not unicodedata.category(character).startswith(("N", "P"))
            and character not in string.punctuation
        ):
            cleaned.append(character)
    return "".join(cleaned).strip()


class LyricRomanizer(Protocol):
    """Prepares a complete lyric phrase while preserving token alignment."""

    def prepare(self, lyrics: Sequence[PreparedLyric]) -> Sequence[PreparedLyric]:
        """Romanize prepared lookup forms without changing their originals."""


class IdentityRomanizer:
    """Leave lyrics unchanged for languages already written in dictionary form."""

    def prepare(self, lyrics: Sequence[PreparedLyric]) -> Sequence[PreparedLyric]:
        return tuple(lyrics)


class OpenUtauJapaneseRomanizer:
    """OpenUtau-style Kana-to-romaji preparation for DiffSinger Japanese."""

    _kana_token = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff]+$")
    _dictionary_forms = {
        "っ": "cl",
        "ッ": "cl",
        "ティ": "ti",
        "ディ": "di",
        "トゥ": "tu",
        "ドゥ": "du",
    }
    _small_kana = frozenset(
        "ぁぃぅぇぉゃゅょゎゕゖ"
        "ァィゥェォャュョヮヵヶ"
    )
    _long_vowel_marks = frozenset({"ー", "ｰ"})
    _vowel_kana = {"a": "あ", "i": "い", "u": "う", "e": "え", "o": "お"}

    def __init__(self, dictionary_form: str = "romaji") -> None:
        if dictionary_form not in {"kana", "romaji"}:
            raise ValueError("Japanese dictionary form must be 'kana' or 'romaji'.")
        self.dictionary_form = dictionary_form

    @staticmethod
    @lru_cache(maxsize=1)
    def _converter():
        return kakasi()

    @classmethod
    @lru_cache(maxsize=4096)
    def _kana_to_romaji(cls, lyric: str) -> str:
        if lyric in cls._dictionary_forms:
            return cls._dictionary_forms[lyric]
        converted = cls._converter().convert(lyric)
        return "".join(str(part.get("hepburn", "")) for part in converted).lower()

    @classmethod
    def _mora_units(cls, lyric: str) -> tuple[str, ...]:
        """Split a Kana lyric into dictionary-sized Japanese mora units."""
        units: list[str] = []
        for character in lyric:
            if character in cls._small_kana and units and units[-1] not in cls._long_vowel_marks:
                units[-1] += character
            else:
                units.append(character)
        return tuple(units)

    @staticmethod
    def _last_vowel(value: str) -> str:
        for character in reversed(value):
            if character in "aiueo":
                return character
        return ""

    @staticmethod
    def _to_hiragana(value: str) -> str:
        """Normalize Katakana dictionary keys to their Hiragana equivalent."""
        return "".join(
            chr(ord(character) - 0x60) if "ァ" <= character <= "ヶ" else character
            for character in value
        )

    def _fallback_mora_lookups(self, lyric: str) -> tuple[str, ...]:
        """Return phonetic mora keys for a multi-mora Kana lyric.

        DiffSinger dictionaries are commonly keyed by one Japanese mora. A
        music score may place several morae on one note, so retain the whole
        romanization for an explicit voicebank entry while also preparing this
        deterministic dictionary fallback.
        """
        lookups: list[str] = []
        phonetic_morae: list[str] = []
        for unit in self._mora_units(lyric):
            raw_romanization = ""
            if unit in self._long_vowel_marks:
                vowel = self._last_vowel(phonetic_morae[-1]) if phonetic_morae else ""
                if not vowel:
                    return ()
                phonetic_mora = vowel
            else:
                raw_romanization = self._kana_to_romaji(unit)
                previous_vowel = self._last_vowel(phonetic_morae[-1]) if phonetic_morae else ""
                phonetic_mora = raw_romanization
                # Orthographic う / い often lengthens the preceding o / e vowel.
                if phonetic_mora == "u" and previous_vowel == "o":
                    phonetic_mora = "o"
                elif phonetic_mora == "i" and previous_vowel == "e":
                    phonetic_mora = "e"
            if not phonetic_mora:
                return ()
            phonetic_morae.append(phonetic_mora)
            if self.dictionary_form == "romaji":
                lookups.append(phonetic_mora)
            elif phonetic_mora != raw_romanization:
                lookups.append(self._vowel_kana[phonetic_mora])
            else:
                lookups.append(self._to_hiragana(unit))
        return tuple(lookups)

    def prepare(self, lyrics: Sequence[PreparedLyric]) -> Sequence[PreparedLyric]:
        prepared = []
        for lyric in lyrics:
            lookup = lyric.lookup
            fallback_lookups: tuple[str, ...] = ()
            if self._kana_token.fullmatch(lookup):
                fallback_lookups = self._fallback_mora_lookups(lookup)
                lookup = self._kana_to_romaji(lookup)
                if fallback_lookups == (lookup,):
                    fallback_lookups = ()
            prepared.append(
                PreparedLyric(
                    original=lyric.original,
                    lookup=lookup,
                    fallback_lookups=fallback_lookups,
                )
            )
        return tuple(prepared)


class OpenUtauChineseRomanizer:
    """Prepare Chinese DiffSinger lyrics as tone-less Pinyin for lookup."""

    _hanzi_token = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]$")

    @staticmethod
    def _pinyin_phrase(tokens: Sequence[str]) -> Sequence[str]:
        # pypinyin resolves common polyphones from contiguous phrase context.
        pinyin = lazy_pinyin("".join(tokens), style=Style.NORMAL, strict=False)
        return tuple(value.lower().replace("ü", "v").replace("u:", "v") for value in pinyin)

    def prepare(self, lyrics: Sequence[PreparedLyric]) -> Sequence[PreparedLyric]:
        lookups = [lyric.lookup for lyric in lyrics]
        index = 0
        while index < len(lookups):
            if not self._hanzi_token.fullmatch(lookups[index]):
                index += 1
                continue
            end = index + 1
            while end < len(lookups) and self._hanzi_token.fullmatch(lookups[end]):
                end += 1
            pinyin = self._pinyin_phrase(lookups[index:end])
            if len(pinyin) == end - index:
                lookups[index:end] = pinyin
            index = end
        return tuple(
            PreparedLyric(original=lyric.original, lookup=lookup)
            for lyric, lookup in zip(lyrics, lookups)
        )


@dataclass(frozen=True)
class LanguagePronunciationPipeline:
    """Language behavior applied before and after voicebank dictionary lookup."""

    language: str
    romanizer: LyricRomanizer
    g2p_fallback: Optional[LanguageG2pProvider] = None

    def prepare(self, tokens: Sequence[str]) -> Sequence[PreparedLyric]:
        """Create shared lookup forms, then apply language-specific romanization."""
        lyrics = tuple(
            PreparedLyric(
                original=str(token),
                lookup=prepare_lookup_lyric(str(token), language=self.language),
            )
            for token in tokens
        )
        return self.romanizer.prepare(lyrics)


class LanguagePronunciationRegistry:
    """Declarative language pipeline registry without phonemizer conditionals."""

    _pipelines: Dict[str, LanguagePronunciationPipeline] = {}
    _identity = IdentityRomanizer()

    @classmethod
    def register(cls, pipeline: LanguagePronunciationPipeline) -> None:
        language = pipeline.language.strip().lower()
        if not language:
            raise ValueError("A language pronunciation pipeline must declare its language.")
        cls._pipelines[language] = pipeline

    @classmethod
    def resolve(cls, language: str) -> LanguagePronunciationPipeline:
        normalized = str(language).strip().lower()
        return cls._pipelines.get(
            normalized,
            LanguagePronunciationPipeline(language=normalized, romanizer=cls._identity),
        )

    @classmethod
    def registered_languages(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._pipelines))


LanguagePronunciationRegistry.register(
    LanguagePronunciationPipeline(
        language="en",
        romanizer=IdentityRomanizer(),
        g2p_fallback=DiffSingerEnglishPhonemizer(),
    )
)
LanguagePronunciationRegistry.register(
    LanguagePronunciationPipeline(
        language="es",
        romanizer=IdentityRomanizer(),
        g2p_fallback=DiffSingerSpanishPhonemizer(),
    )
)
LanguagePronunciationRegistry.register(
    LanguagePronunciationPipeline(language="ja", romanizer=OpenUtauJapaneseRomanizer())
)
LanguagePronunciationRegistry.register(
    LanguagePronunciationPipeline(language="zh", romanizer=OpenUtauChineseRomanizer())
)


def get_language_pronunciation_pipeline(
    language: str,
    *,
    japanese_dictionary_form: Optional[str] = None,
) -> LanguagePronunciationPipeline:
    """Resolve phrase preparation and optional G2P behavior for a language."""
    normalized = str(language).strip().lower()
    if normalized == "ja" and japanese_dictionary_form is not None:
        return LanguagePronunciationPipeline(
            language="ja",
            romanizer=OpenUtauJapaneseRomanizer(japanese_dictionary_form),
        )
    return LanguagePronunciationRegistry.resolve(normalized)
