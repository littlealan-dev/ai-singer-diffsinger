from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Sequence

import yaml
@dataclass(frozen=True)
class PhonemeResult:
    phonemes: Sequence[str]
    ids: Sequence[int]


class Phonemizer:
    def __init__(
        self,
        *,
        phonemes_path: Path,
        dictionary_path: Path,
        language: str = "en",
    ) -> None:
        if language != "en":
            raise NotImplementedError(
                f"Only English is supported for now (language='{language}')."
            )
        self.language = language
        self.phonemes_path = Path(phonemes_path)
        self.dictionary_path = Path(dictionary_path)
        self._phoneme_to_id = self._load_phoneme_inventory(self.phonemes_path)
        self._dictionary = self._load_dictionary(self.dictionary_path)

    def phonemize_tokens(self, tokens: Sequence[str]) -> PhonemeResult:
        phonemes: List[str] = []
        for token in tokens:
            phonemes.extend(self._phonemize_token(token))
        ids = [self._phoneme_to_id[p] for p in phonemes]
        return PhonemeResult(phonemes=phonemes, ids=ids)

    def _phonemize_token(self, token: str) -> List[str]:
        raw = token.strip()
        if not raw:
            return []
        normalized = self._normalize_grapheme(raw)
        if normalized and normalized in self._dictionary:
            return self._validate_phonemes(self._dictionary[normalized], raw)
        raise KeyError(
            f"No dictionary entry for token '{raw}' in {self.dictionary_path}. "
            "Update the voicebank dsdict.yaml to include this grapheme."
        )

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

    def _phonemes_match_language(self, phonemes: Iterable[str]) -> bool:
        for phoneme in phonemes:
            if "/" not in str(phoneme):
                continue
            if not str(phoneme).startswith(f"{self.language}/"):
                return False
        return True
