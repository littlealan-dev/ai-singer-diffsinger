from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Sequence

from g2p_en import G2p
import yaml

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
    phonemes: Sequence[str]
    ids: Sequence[int]
    language_ids: Sequence[int]


class Phonemizer:
    def __init__(
        self,
        *,
        phonemes_path: Path,
        dictionary_path: Path,
        languages_path: Optional[Path] = None,
        language: str = "en",
        allow_g2p: bool = True,
    ) -> None:
        if language != "en":
            raise NotImplementedError(
                f"Only English is supported for now (language='{language}')."
            )
        self.language = language
        self.allow_g2p = allow_g2p
        self.phonemes_path = Path(phonemes_path)
        self.dictionary_path = Path(dictionary_path)
        self.languages_path = Path(languages_path) if languages_path else None
        
        self._phoneme_to_id = self._load_phoneme_inventory(self.phonemes_path)
        self._dictionary = self._load_dictionary(self.dictionary_path)
        self._vowel_symbols, self._glide_symbols = self._load_symbol_types(self.dictionary_path)
        self._language_map = self._load_language_map(self.languages_path) if self.languages_path else {}
        self._g2p: Optional[G2p] = None

    def phonemize_tokens(self, tokens: Sequence[str]) -> PhonemeResult:
        phonemes: List[str] = []
        for token in tokens:
            phonemes.extend(self._phonemize_token(token))
        ids = [self._phoneme_to_id[p] for p in phonemes]
        
        # Resolve language ID for each phoneme
        # Assumes format "lang/phoneme" or fallback to 0
        lang_ids = []
        for p in phonemes:
            lang_code = p.split("/")[0] if "/" in p else ""
            lang_id = self._language_map.get(lang_code, 0)
            lang_ids.append(lang_id)
            
        return PhonemeResult(phonemes=phonemes, ids=ids, language_ids=lang_ids)

    def is_vowel(self, phoneme: str) -> bool:
        return phoneme in self._vowel_symbols

    def is_glide(self, phoneme: str) -> bool:
        return phoneme in self._glide_symbols

    def _phonemize_token(self, token: str) -> List[str]:
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
        validated = []
        for phoneme in phonemes:
            if phoneme not in self._phoneme_to_id:
                raise KeyError(
                    f"Unknown phoneme '{phoneme}' from token '{token}'. "
                    f"Check {self.phonemes_path} or update mappings."
                )
            validated.append(phoneme)
        return validated

    @staticmethod
    def _normalize_grapheme(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z']+", "", value).lower()
        return cleaned or value.strip()

    @staticmethod
    def _normalize_word_for_g2p(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z']+", "", value).lower()
        return cleaned

    @staticmethod
    def _is_arpabet(value: str) -> bool:
        return bool(re.search(r"[A-Za-z]", value))

    def _map_arpabet(self, phone: str) -> str:
        base = re.sub(r"[0-9]", "", phone).upper()
        if base not in ARPABET_TO_VOICEBANK:
            raise KeyError(
                f"Unsupported ARPABET symbol '{phone}' in G2P output."
            )
        # Prefix with language code (e.g. en/hh)
        # This aligns with OpenUtau's behavior when use_lang_id is true
        return f"{self.language}/{ARPABET_TO_VOICEBANK[base]}"

    def _get_g2p(self) -> G2p:
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
        if not path.exists():
            raise FileNotFoundError(
                f"Phoneme inventory not found at {path}. "
                "Expected a phonemes.json from the voicebank."
            )
        data = yaml.safe_load(path.read_text(encoding="utf8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid phonemes.json format at {path}.")
        return {str(k): int(v) for k, v in data.items()}
    
    @staticmethod
    def _load_language_map(path: Path) -> Dict[str, int]:
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
        if not path.exists():
            raise FileNotFoundError(
                f"Phoneme dictionary not found at {path}. "
                "Expected an OpenUtau dsdict.yaml (e.g. voicebank/dsvariance/dsdict.yaml)."
            )
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
        return dictionary

    def _load_symbol_types(self, path: Path) -> tuple[set[str], set[str]]:
        if not path.exists():
            return set(), set()
        data = yaml.safe_load(path.read_text(encoding="utf8"))
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
        return vowels, glides

    def _phonemes_match_language(self, phonemes: Iterable[str]) -> bool:
        for phoneme in phonemes:
            if "/" not in str(phoneme):
                continue
            # If phoneme has a language prefix (e.g. en/hh), check if it matches current language
            if not str(phoneme).startswith(f"{self.language}/"):
                return False
        return True
