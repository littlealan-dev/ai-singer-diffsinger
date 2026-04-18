from __future__ import annotations

"""Phonemization utilities for mapping lyrics to voicebank phonemes."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Sequence

from g2p_en import G2p
import yaml

from .phoneme_logic_handler import get_phoneme_logic_handler

ARPABET_TO_VOICEBANK = {
    "AA": "aa",
    "AE": "ae",
    "AH": "ah",
    "AO": "ao",
    "AW": "aw",
    "AX": "ax",
    "AXR": "er",
    "AY": "ay",
    "B": "b",
    "CH": "ch",
    "D": "d",
    "DH": "dh",
    "DX": "dx",
    "EH": "eh",
    "ER": "er",
    "EY": "ey",
    "F": "f",
    "G": "g",
    "HH": "hh",
    "IH": "ih",
    "IX": "ih",
    "IY": "iy",
    "JH": "jh",
    "K": "k",
    "L": "l",
    "M": "m",
    "N": "n",
    "NG": "ng",
    "OW": "ow",
    "OY": "oy",
    "P": "p",
    "R": "r",
    "S": "s",
    "SH": "sh",
    "T": "t",
    "TH": "th",
    "UH": "uh",
    "UW": "uw",
    "UX": "uw",
    "V": "v",
    "W": "w",
    "Y": "y",
    "Z": "z",
    "ZH": "zh",
}


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
    load_strategy: str


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
        if language != "en":
            raise NotImplementedError(
                f"Only English is supported for now (language='{language}')."
            )
        self.language = language
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
        self._dictionary_load_strategy = dictionary_bundle.load_strategy
        self._language_map = self._load_language_map(self.languages_path) if self.languages_path else {}
        self._phoneme_meta = self._load_phoneme_metadata(
            self.phonemes_path.with_name("phoneme_metadata.json")
        )
        self._g2p: Optional[G2p] = None
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
        cleaned = self._normalize_word_for_g2p(raw)
        if not cleaned:
            raise KeyError(
                f"Token '{raw}' has no usable letters for G2P lookup."
            )
        phones = [p for p in self._get_g2p()(cleaned) if self._is_arpabet(p)]
        if not phones:
            raise KeyError(
                f"G2P produced no phonemes for token '{raw}'."
            )
        mapped = [self._map_arpabet(p) for p in phones]
        return self._validate_phonemes(mapped, raw)

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
        cleaned = re.sub(r"[^A-Za-z']+", "", value).lower()
        return cleaned or value.strip()

    @staticmethod
    def _normalize_word_for_g2p(value: str) -> str:
        """Normalize a word for G2P processing."""
        cleaned = re.sub(r"[^A-Za-z']+", "", value).lower()
        return cleaned

    @staticmethod
    def _is_arpabet(value: str) -> bool:
        """Return True if the token looks like an ARPABET symbol."""
        return bool(re.search(r"[A-Za-z]", value))

    def _map_arpabet(self, phone: str) -> str:
        """Map ARPABET symbol to the voicebank phoneme set."""
        base = re.sub(r"[0-9]", "", phone).upper()
        if base not in ARPABET_TO_VOICEBANK:
            raise KeyError(
                f"Unsupported ARPABET symbol '{phone}' in G2P output."
            )
        # Prefix with language code (e.g. en/hh).
        # This aligns with OpenUtau's behavior when use_lang_id is true.
        return f"{self.language}/{ARPABET_TO_VOICEBANK[base]}"

    def _get_g2p(self) -> G2p:
        """Lazily construct the G2P engine."""
        if self._g2p is None:
            try:
                self._g2p = G2p()
            except LookupError as exc:
                raise RuntimeError(
                    "g2p_en requires the NLTK cmudict corpus. "
                    "Install it with: python -m nltk.downloader cmudict"
                ) from exc
        return self._g2p

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
        return {str(k): int(v) for k, v in data.items()}

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
        in_symbols = False
        in_entries = False
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
                    current_symbol = None
                    continue
                if stripped == "entries:":
                    if current_symbol is not None:
                        current_symbol = None
                    in_symbols = False
                    in_entries = True
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
                    if not remaining:
                        break
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
