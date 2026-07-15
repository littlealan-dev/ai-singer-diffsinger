from __future__ import annotations

"""Phonemization utilities for mapping lyrics to voicebank phonemes."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence

import yaml

from .language_g2p import (
    G2pInputError,
    first_non_latin_letter,
    get_language_g2p_provider,
    normalize_word_for_english_g2p,
)
from .phoneme_logic_handler import get_phoneme_logic_handler


@dataclass(frozen=True)
class PhonemeResult:
    """Phonemization result with IDs and language IDs."""
    phonemes: Sequence[str]
    ids: Sequence[int]
    language_ids: Sequence[int]


@dataclass(frozen=True)
class DictionaryBundle:
    """Loaded dictionary entries and symbol metadata."""
    dictionary: Dict[str, List[str]]
    vowels: set[str]
    glides: set[str]
    replacements: Dict[str, str]
    load_strategy: str


class UnsupportedLyricTokenError(ValueError):
    """Raised when a lyric token cannot be handled by the selected language path."""

    def __init__(
        self,
        *,
        token: str,
        language: str,
        reason: str,
        normalized_token: str = "",
        unsupported_character: str = "",
        unsupported_character_name: str = "",
        unsupported_script: str = "",
    ) -> None:
        self.token = token
        self.language = language
        self.reason = reason
        self.normalized_token = normalized_token
        self.unsupported_character = unsupported_character
        self.unsupported_character_name = unsupported_character_name
        self.unsupported_script = unsupported_script
        if reason == "non_latin_lyrics_for_english_g2p":
            self.code = "unsupported_lyric_language"
        else:
            self.code = "invalid_lyric_token"
        super().__init__(self.error_message)

    @property
    def error_message(self) -> str:
        if self.reason == "non_latin_lyrics_for_english_g2p":
            return (
                f"Token '{self.token}' contains non-Latin text that cannot be "
                f"phonemized by the '{self.language}' G2P fallback."
            )
        return (
            f"Token '{self.token}' has no usable letters for the "
            f"'{self.language}' G2P fallback."
        )


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with fallback."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_large_dict_threshold_bytes() -> int:
    """Return the active oversized-dictionary threshold."""
    return _env_int("VOICEBANK_LARGE_DICT_THRESHOLD_BYTES", 5_000_000)


class Phonemizer:
    """Phonemizer that uses dictionary lookup with optional G2P fallback."""
    def __init__(
        self,
        *,
        phonemes_path: Path,
        dictionary_path: Path,
        languages_path: Optional[Path] = None,
        language: str = "en",
        allow_g2p: bool = True,
        needed_graphemes: Optional[set[str]] = None,
    ) -> None:
        """Initialize phoneme inventory, dictionary, and optional G2P."""
        normalized_language = str(language or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]+)*", normalized_language):
            raise ValueError(f"Invalid language code '{language}'.")
        self.language = normalized_language
        self.allow_g2p = allow_g2p
        self.phonemes_path = Path(phonemes_path)
        self.dictionary_path = Path(dictionary_path)
        self.languages_path = Path(languages_path) if languages_path else None
        self._needed_graphemes = {
            self._normalize_grapheme(value)
            for value in (needed_graphemes or set())
            if self._normalize_grapheme(value)
        }
        
        self._phoneme_to_id = self._load_phoneme_inventory(self.phonemes_path)
        dictionary_bundle = self._load_dictionary_bundle(
            self.dictionary_path,
            needed_graphemes=self._needed_graphemes or None,
        )
        self._dictionary = dictionary_bundle.dictionary
        self._vowel_symbols = dictionary_bundle.vowels
        self._glide_symbols = dictionary_bundle.glides
        self._dictionary_replacements = dictionary_bundle.replacements
        self._dictionary_load_strategy = dictionary_bundle.load_strategy
        self._language_map = self._load_language_map(self.languages_path) if self.languages_path else {}
        if self._language_map and self.language not in self._language_map:
            supported = ", ".join(sorted(self._language_map))
            raise ValueError(
                f"Language '{self.language}' is not present in {self.languages_path}. "
                f"Available languages: {supported}."
            )
        self._phoneme_meta = self._load_phoneme_metadata(
            self.phonemes_path.with_name("phoneme_metadata.json")
        )
        self._g2p_provider = get_language_g2p_provider(self.language)
        self._logic_handler = get_phoneme_logic_handler(language)

    def distribute_slur(self, phonemes: Sequence[str], note_count: int) -> Optional[List[List[str]]]:
        """
        Distribute phonemes across notes for a slur.
        Returns:
            - List of phoneme lists (one per note) if a strategy exists.
            - None if no strategy exists (caller should use default/fallback logic).
        """
        return self._logic_handler.distribute_slur(phonemes, note_count, self)

    def phonemize_tokens(self, tokens: Sequence[str]) -> PhonemeResult:
        """Convert a list of tokens into phonemes and IDs."""
        phonemes: List[str] = []
        for token in tokens:
            phonemes.extend(self._phonemize_token(token))
        ids = [self._phoneme_to_id[p] for p in phonemes]

        # Resolve language ID for each phoneme.
        # Assumes format "lang/phoneme" or fallback to 0.
        lang_ids = []
        for p in phonemes:
            lang_code = p.split("/")[0] if "/" in p else ""
            lang_id = self._language_map.get(lang_code, 0)
            lang_ids.append(lang_id)
            
        return PhonemeResult(phonemes=phonemes, ids=ids, language_ids=lang_ids)

    def is_vowel(self, phoneme: str) -> bool:
        """Return True if the phoneme is a vowel."""
        return phoneme in self._vowel_symbols

    def is_glide(self, phoneme: str) -> bool:
        """Return True if the phoneme is a glide/semivowel."""
        return phoneme in self._glide_symbols

    def vowel_strength(self, phoneme: str) -> Optional[float]:
        """Return optional vowel strength metadata for the phoneme."""
        meta = self._phoneme_meta.get(phoneme)
        if not meta:
            return None
        return meta.get("vowel_strength")

    def _phonemize_token(self, token: str) -> List[str]:
        """Phonemize a single token using dictionary or G2P."""
        raw = token.strip()
        if not raw:
            return []
        if raw in self._phoneme_to_id:
            return [raw]
        if raw.upper() in self._phoneme_to_id:
            return [raw.upper()]
        normalized = self._normalize_grapheme(raw)
        if normalized and normalized in self._dictionary:
            return self._validate_phonemes(self._dictionary[normalized], raw)
        if not self.allow_g2p:
            raise KeyError(
                f"No dictionary entry for token '{raw}' in {self.dictionary_path}. "
                "Update the voicebank dsdict.yaml to include this grapheme, or enable G2P."
            )
        if self._g2p_provider is None:
            raise KeyError(
                f"No dictionary entry for token '{raw}' in {self.dictionary_path}. "
                f"G2P fallback is not available for language '{self.language}'; "
                "the selected voicebank dictionary must include this grapheme."
            )
        try:
            bare_phonemes = self._g2p_provider.phonemize(raw)
        except G2pInputError as exc:
            raise UnsupportedLyricTokenError(
                token=raw,
                language=self.language,
                reason=exc.reason,
                normalized_token=exc.normalized_token,
                unsupported_character=exc.unsupported_character,
                unsupported_character_name=exc.unsupported_character_name,
                unsupported_script=exc.unsupported_script,
            ) from exc
        if not bare_phonemes:
            raise KeyError(
                f"G2P produced no phonemes for token '{raw}'."
            )
        mapped = [self._map_g2p_phoneme(phoneme) for phoneme in bare_phonemes]
        return self._validate_phonemes(mapped, raw)

    def _map_g2p_phoneme(self, phoneme: str) -> str:
        """Map a provider's bare symbol through voicebank replacements and language IDs."""
        replacement = self._dictionary_replacements.get(phoneme)
        if replacement:
            return replacement
        return f"{self.language}/{phoneme}" if self._language_map else phoneme

    def _validate_phonemes(self, phonemes: Sequence[str], token: str) -> List[str]:
        """Ensure phonemes are present in the voicebank inventory."""
        validated = []
        for phoneme in phonemes:
            resolved = self._resolve_inventory_phoneme(phoneme)
            if resolved is None:
                raise KeyError(
                    f"Unknown phoneme '{phoneme}' from token '{token}'. "
                    f"Check {self.phonemes_path} or update mappings."
                )
            validated.append(resolved)
        return validated

    def _resolve_inventory_phoneme(self, phoneme: str) -> Optional[str]:
        """Resolve a phoneme against the inventory, allowing a narrow lang-prefix fallback."""
        if phoneme in self._phoneme_to_id:
            return phoneme
        prefix = f"{self.language}/"
        if phoneme.startswith(prefix):
            bare = phoneme[len(prefix):]
            if bare in self._phoneme_to_id:
                return bare
        return None

    @staticmethod
    def _load_phoneme_metadata(path: Path) -> Dict[str, Dict[str, float]]:
        """Load optional phoneme metadata (e.g., vowel strength)."""
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _normalize_grapheme(value: str) -> str:
        """Normalize a grapheme for dictionary lookup."""
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        normalized = normalized.replace("’", "'")
        cleaned = "".join(char for char in normalized if char.isalpha() or char == "'")
        return cleaned or normalized

    @staticmethod
    def _normalize_word_for_g2p(value: str) -> str:
        """Normalize a Latin word for English G2P processing."""
        return normalize_word_for_english_g2p(value)

    @staticmethod
    def _first_non_latin_letter(value: str) -> Optional[tuple[str, str, str]]:
        """Return the first alphabetic character outside the Latin script."""
        return first_non_latin_letter(value)

    @staticmethod
    def _load_phoneme_inventory(path: Path) -> Dict[str, int]:
        """Load phoneme inventory from phonemes.json or phonemes.txt."""
        if not path.exists():
            raise FileNotFoundError(
                f"Phoneme inventory not found at {path}. "
                "Expected a phonemes.json or phonemes.txt from the voicebank."
            )
        raw_text = path.read_text(encoding="utf8")
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
        return Phonemizer._parse_text_phoneme_inventory(raw_text, path)

    @staticmethod
    def _parse_text_phoneme_inventory(raw_text: str, path: Path) -> Dict[str, int]:
        """Load a line-based phoneme inventory from plain text."""
        phoneme_to_id: Dict[str, int] = {}
        for line in raw_text.splitlines():
            symbol = line.strip()
            if not symbol or symbol.startswith("#") or symbol.startswith(";"):
                continue
            if symbol in phoneme_to_id:
                raise ValueError(f"Duplicate phoneme '{symbol}' in phoneme inventory at {path}.")
            phoneme_to_id[symbol] = len(phoneme_to_id)
        if not phoneme_to_id:
            raise ValueError(f"Invalid phoneme inventory format at {path}.")
        return phoneme_to_id
    
    @staticmethod
    def _load_language_map(path: Path) -> Dict[str, int]:
        """Load language ID map from languages.json."""
        if not path.exists():
            raise FileNotFoundError(
                f"Languages map not found at {path}. "
                "Expected a languages.json from the voicebank."
            )
        data = yaml.safe_load(path.read_text(encoding="utf8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid languages.json format at {path}.")
        return {str(k).strip().lower(): int(v) for k, v in data.items()}

    def _load_dictionary(self, path: Path) -> Dict[str, List[str]]:
        """Load grapheme-to-phoneme entries from dsdict.yaml."""
        return self._load_dictionary_bundle(path, needed_graphemes=None).dictionary

    def _load_symbol_types(self, path: Path) -> tuple[set[str], set[str]]:
        """Load vowel/glide symbol sets from dsdict.yaml."""
        bundle = self._load_dictionary_bundle(path, needed_graphemes=None)
        return bundle.vowels, bundle.glides

    def _load_dictionary_bundle(
        self,
        path: Path,
        *,
        needed_graphemes: Optional[set[str]],
    ) -> DictionaryBundle:
        """Load dictionary entries and symbol metadata using adaptive strategy."""
        if not path.exists():
            raise FileNotFoundError(
                f"Phoneme dictionary not found at {path}. "
                "Expected an OpenUtau dsdict.yaml (e.g. voicebank/dsvariance/dsdict.yaml)."
            )
        if needed_graphemes and path.stat().st_size > get_large_dict_threshold_bytes():
            return self._load_dictionary_bundle_selective(path, needed_graphemes=needed_graphemes)
        return self._load_dictionary_bundle_eager(path)

    def _load_dictionary_bundle_eager(self, path: Path) -> DictionaryBundle:
        """Load the full dictionary with YAML parsing for normal-sized files."""
        data = yaml.safe_load(path.read_text(encoding="utf8"))
        entries = data.get("entries", []) if isinstance(data, dict) else []
        dictionary: Dict[str, List[str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            grapheme = entry.get("grapheme")
            phonemes = entry.get("phonemes")
            if not grapheme or not phonemes:
                continue
            if not self._phonemes_match_language(phonemes):
                continue
            key = self._normalize_grapheme(grapheme)
            if key not in dictionary:
                dictionary[key] = [str(p) for p in phonemes]
        replacements: Dict[str, str] = {}
        for replacement in data.get("replacements", []) if isinstance(data, dict) else []:
            if not isinstance(replacement, dict):
                continue
            source = str(replacement.get("from", "")).strip()
            target = str(replacement.get("to", "")).strip()
            if source and target:
                replacements[source] = target
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        vowels = {"SP", "AP"}
        glides = set()
        for entry in symbols:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol", "")).strip()
            symbol_type = str(entry.get("type", "")).strip().lower()
            if not symbol:
                continue
            if symbol_type == "vowel":
                vowels.add(symbol)
            if symbol_type in ("semivowel", "liquid"):
                glides.add(symbol)
        return DictionaryBundle(
            dictionary=dictionary,
            vowels=vowels,
            glides=glides,
            replacements=replacements,
            load_strategy="eager",
        )

    def _load_dictionary_bundle_selective(
        self,
        path: Path,
        *,
        needed_graphemes: set[str],
    ) -> DictionaryBundle:
        """Line-scan an oversized OpenUtau dictionary and keep only needed entries."""
        dictionary: Dict[str, List[str]] = {}
        vowels = {"SP", "AP"}
        glides = set()
        replacements: Dict[str, str] = {}
        in_symbols = False
        in_entries = False
        in_replacements = False
        current_symbol: Optional[str] = None
        current_grapheme: Optional[str] = None
        current_key: Optional[str] = None
        current_phonemes: Optional[List[str]] = None
        remaining = set(needed_graphemes)

        def finalize_entry() -> None:
            nonlocal current_grapheme, current_key, current_phonemes, remaining
            if current_grapheme and current_key and current_phonemes:
                if self._phonemes_match_language(current_phonemes) and current_key not in dictionary:
                    dictionary[current_key] = list(current_phonemes)
                    remaining.discard(current_key)
            current_grapheme = None
            current_key = None
            current_phonemes = None

        with path.open("r", encoding="utf8", errors="replace") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                if stripped == "symbols:":
                    in_symbols = True
                    in_entries = False
                    in_replacements = False
                    current_symbol = None
                    continue
                if stripped == "entries:":
                    if current_symbol is not None:
                        current_symbol = None
                    in_symbols = False
                    in_entries = True
                    in_replacements = False
                    continue
                if stripped == "replacements:":
                    finalize_entry()
                    in_symbols = False
                    in_entries = False
                    in_replacements = True
                    continue
                if in_replacements:
                    if stripped.startswith("- "):
                        replacement = yaml.safe_load(stripped[2:])
                        if isinstance(replacement, dict):
                            source = str(replacement.get("from", "")).strip()
                            target = str(replacement.get("to", "")).strip()
                            if source and target:
                                replacements[source] = target
                    continue
                if in_symbols:
                    if stripped.startswith("- symbol:"):
                        current_symbol = stripped[len("- symbol:"):].strip()
                        continue
                    if stripped.startswith("type:") and current_symbol:
                        symbol_type = stripped[len("type:"):].strip().lower()
                        if symbol_type == "vowel":
                            vowels.add(current_symbol)
                        if symbol_type in ("semivowel", "liquid"):
                            glides.add(current_symbol)
                        current_symbol = None
                    continue
                if not in_entries:
                    continue
                if stripped.startswith("- grapheme:"):
                    finalize_entry()
                    grapheme = stripped[len("- grapheme:"):].strip()
                    current_grapheme = grapheme
                    current_key = self._normalize_grapheme(grapheme)
                    current_phonemes = [] if current_key in needed_graphemes else None
                    continue
                if stripped.startswith("phonemes:"):
                    continue
                if stripped.startswith("- ") and current_phonemes is not None:
                    current_phonemes.append(stripped[2:].strip())
                    continue
            finalize_entry()
        return DictionaryBundle(
            dictionary=dictionary,
            vowels=vowels,
            glides=glides,
            replacements=replacements,
            load_strategy="selective",
        )

    def _phonemes_match_language(self, phonemes: Iterable[str]) -> bool:
        """Return True if phonemes match the current language prefix."""
        for phoneme in phonemes:
            if "/" not in str(phoneme):
                continue
            # If phoneme has a language prefix (e.g. en/hh), check if it matches current language
            if not str(phoneme).startswith(f"{self.language}/"):
                return False
        return True
