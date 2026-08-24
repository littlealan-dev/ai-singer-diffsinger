"""OpenUtau G2P fallbacks compatible with DiffSinger phonemizers.

Each bundled resource has OpenUtau's word dictionary and ONNX G2P model.
OpenUtau uses its dictionary first and then its model for a missing word.  An
optional SightSinger lexicon overlay can supply corrections for words whose
pronunciation requires lexical information that an ONNX fallback cannot infer.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Dict, Mapping, Tuple
import zipfile

import numpy as np
import onnxruntime as ort
import yaml


_GRAPHEMES = (
    "", "", "", "", "'", "-", "a", "b", "c", "d", "e", "f", "g", "h", "i",
    "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w",
    "x", "y", "z", "á", "é", "í", "ó", "ú", "ã", "ë", "ê", "ñ", "ü",
)
_PHONEMES = (
    "", "", "", "", "a", "b", "B", "ch", "d", "D", "e", "f", "g", "G", "gn",
    "i", "I", "k", "l", "ll", "m", "n", "o", "p", "r", "rr", "s", "t", "u",
    "U", "w", "x", "y", "Y", "z",
)
_OPENUTAU_ASSET_ROOT = Path(__file__).with_name("assets") / "openutau"
_G2P_PACK_CONFIG_PATH = _OPENUTAU_ASSET_ROOT / "g2p_packs.yaml"

_MODEL_PATH = _OPENUTAU_ASSET_ROOT / "g2p-es.zip"

_FRENCH_MILLEFEUILLE_GRAPHEMES = (
    "", "", "", "", "'", "-", "a", "b", "c", "d", "e", "f", "g", "h", "i",
    "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w",
    "x", "y", "z", "é", "è", "ê", "à", "â", "î", "ô", "ù", "û", "ç", "œ",
    "ï", "(", ")", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
)
_FRENCH_MILLEFEUILLE_PHONEMES = (
    "", "", "", "", "ah", "eh", "ae", "ee", "oe", "ih", "oh", "oo", "ou",
    "uh", "en", "in", "on", "uy", "y", "w", "f", "k", "p", "s", "sh", "t",
    "h", "b", "d", "g", "l", "m", "n", "r", "v", "z", "j", "ng", "q",
)
_FRENCH_MILLEFEUILLE_MODEL_PATH = _OPENUTAU_ASSET_ROOT / "g2p-fr-millefeuille.zip"
_ITALIAN_GRAPHEMES = (
    "", "", "", "", "'", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
    "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x",
    "y", "z", "à", "è", "é", "ì", "í", "ò", "ù", "ú",
)
_ITALIAN_PHONEMES = (
    "", "", "", "", "a", "b", "d", "dz", "dZZ", "e", "EE", "f", "g", "i",
    "j", "JJ", "k", "l", "LL", "m", "n", "nf", "ng", "o", "OO", "p", "r",
    "s", "SS", "t", "ts", "tSS", "u", "v", "w", "z",
)
_ITALIAN_MODEL_PATH = _OPENUTAU_ASSET_ROOT / "g2p-it.zip"
_PORTUGUESE_GRAPHEMES = (
    "", "", "", "", "-", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
    "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x",
    "y", "z", "à", "á", "â", "ã", "ç", "è", "é", "ê", "í", "î", "ó", "ô",
    "õ", "ú", "û", "ü",
)
_PORTUGUESE_PHONEMES = (
    "", "", "", "", "E", "J", "L", "O", "R", "S", "X", "Z", "a", "a~", "b",
    "d", "dZ", "e", "e~", "f", "g", "i", "i~", "j", "j~", "k", "l", "m", "n",
    "o", "o~", "p", "r", "s", "t", "tS", "u", "u~", "v", "w", "w~", "z",
)
_PORTUGUESE_MODEL_PATH = _OPENUTAU_ASSET_ROOT / "g2p-pt.zip"


@lru_cache(maxsize=1)
def _load_g2p_pack_config() -> Mapping[str, Mapping[str, str]]:
    """Load optional per-pack configuration without language-specific code."""
    if not _G2P_PACK_CONFIG_PATH.is_file():
        return {}
    loaded = yaml.safe_load(_G2P_PACK_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected a mapping in {_G2P_PACK_CONFIG_PATH}.")
    packs = loaded.get("packs", {})
    if not isinstance(packs, dict):
        raise ValueError(f"Expected a 'packs' mapping in {_G2P_PACK_CONFIG_PATH}.")
    return {
        str(language): settings
        for language, settings in packs.items()
        if isinstance(settings, dict)
    }


@lru_cache(maxsize=None)
def _load_lexicon(path: str) -> Dict[str, Tuple[str, ...]]:
    """Load a pronunciation lexicon used ahead of OpenUtau's packed dict."""
    lexicon_path = Path(path)
    loaded = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected a mapping in {lexicon_path}.")
    entries = loaded.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError(f"Expected an 'entries' mapping in {lexicon_path}.")
    lexicon: Dict[str, Tuple[str, ...]] = {}
    for word, phonemes in entries.items():
        if not isinstance(word, str) or not isinstance(phonemes, list):
            raise ValueError(f"Invalid pronunciation entry in {lexicon_path}.")
        if not all(isinstance(phone, str) and phone for phone in phonemes):
            raise ValueError(f"Invalid phonemes for '{word}' in {lexicon_path}.")
        lexicon[word.lower()] = tuple(phonemes)
    return lexicon


@lru_cache(maxsize=None)
def _load_overlay_lexicon(language: str) -> Dict[str, Tuple[str, ...]]:
    """Resolve a pack's configured app lexicon, if it has one."""
    settings = _load_g2p_pack_config().get(language, {})
    relative_path = settings.get("lexicon")
    if not relative_path:
        return {}
    lexicon_path = _OPENUTAU_ASSET_ROOT / relative_path
    return _load_lexicon(str(lexicon_path))


@lru_cache(maxsize=None)
def _load_pack(
    path: str,
    graphemes: Tuple[str, ...],
) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, int], ort.InferenceSession]:
    """Load one OpenUtau G2P dictionary and ONNX model once per process."""
    with zipfile.ZipFile(path) as archive:
        dictionary: Dict[str, Tuple[str, ...]] = {}
        for line in archive.read("dict.txt").decode("utf-8").splitlines():
            if not line or line.startswith(";;;") or "  " not in line:
                continue
            word, phonemes = line.split("  ", 1)
            dictionary[word.strip().lower()] = tuple(phonemes.split())
        model = archive.read("g2p.onnx")
    grapheme_indexes = {
        grapheme: index
        for index, grapheme in enumerate(graphemes)
        if index >= 4 and grapheme
    }
    return dictionary, grapheme_indexes, ort.InferenceSession(model)


@dataclass(frozen=True)
class OpenUtauG2pPack:
    """Reusable runner for an OpenUtau ``g2p-<language>.zip`` resource."""

    language: str
    path: Path
    graphemes: Tuple[str, ...]
    phonemes: Tuple[str, ...]
    remove_tail_digits: bool = False

    def _normalize_result(self, phonemes: Tuple[str, ...]) -> Tuple[str, ...]:
        if self.remove_tail_digits:
            return tuple(re.sub(r"\d+$", "", phone) for phone in phonemes)
        return phonemes

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare phonemes for a normalized lyric word."""
        normalized = str(word).lower()
        dictionary, grapheme_indexes, session = _load_pack(str(self.path), self.graphemes)
        overlay = _load_overlay_lexicon(self.language)
        if normalized in overlay:
            return self._normalize_result(overlay[normalized])
        if normalized in dictionary:
            return self._normalize_result(dictionary[normalized])

        encoded = [
            grapheme_indexes[character]
            for character in normalized
            if character in grapheme_indexes
        ]
        if not encoded:
            return ()
        source = np.asarray([encoded], dtype=np.int32)
        target = np.asarray([[2]], dtype=np.int32)
        position = np.asarray([0], dtype=np.int32)
        while position[0] < source.shape[1] and target.shape[1] < 48:
            prediction = int(
                session.run(None, {"src": source, "tgt": target, "t": position})[0][0]
            )
            if prediction == 2:
                position[0] += 1
            elif 0 <= prediction < len(self.phonemes):
                target = np.concatenate((target, np.asarray([[prediction]], dtype=np.int32)), axis=1)
            else:
                return ()
        return tuple(self.phonemes[index] for index in target[0][1:] if self.phonemes[index])


class OpenUtauSpanishG2p:
    """Spanish configuration of the reusable OpenUtau G2P pack runner."""

    _pack = OpenUtauG2pPack(
        language="es",
        path=_MODEL_PATH,
        graphemes=_GRAPHEMES,
        phonemes=_PHONEMES,
    )

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare OpenUtau Spanish phonemes for a normalized lyric word."""
        return self._pack.phonemize(word)


class OpenUtauFrenchMillefeuilleG2p:
    """French configuration of OpenUtau's ``g2p-fr-millefeuille`` pack."""

    _pack = OpenUtauG2pPack(
        language="fr",
        path=_FRENCH_MILLEFEUILLE_MODEL_PATH,
        graphemes=_FRENCH_MILLEFEUILLE_GRAPHEMES,
        phonemes=_FRENCH_MILLEFEUILLE_PHONEMES,
    )

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare OpenUtau French Millefeuille phonemes for a lyric word."""
        return self._pack.phonemize(word)


class OpenUtauItalianG2p:
    """Italian configuration of OpenUtau's ``g2p-it`` pack."""

    _pack = OpenUtauG2pPack(
        language="it",
        path=_ITALIAN_MODEL_PATH,
        graphemes=_ITALIAN_GRAPHEMES,
        phonemes=_ITALIAN_PHONEMES,
        remove_tail_digits=True,
    )

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare OpenUtau Italian phonemes for a normalized lyric word."""
        return self._pack.phonemize(word)


class OpenUtauPortugueseG2p:
    """Portuguese configuration of OpenUtau's ``g2p-pt`` pack."""

    _pack = OpenUtauG2pPack(
        language="pt",
        path=_PORTUGUESE_MODEL_PATH,
        graphemes=_PORTUGUESE_GRAPHEMES,
        phonemes=_PORTUGUESE_PHONEMES,
    )

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare OpenUtau Portuguese phonemes for a normalized lyric word."""
        return self._pack.phonemize(word)
