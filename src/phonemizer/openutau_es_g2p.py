"""OpenUtau G2P fallbacks compatible with DiffSinger phonemizers.

Each bundled resource has OpenUtau's word dictionary and ONNX G2P model.
OpenUtau uses its dictionary first and then its model for a missing word; this
module follows the same order.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Dict, Tuple
import zipfile

import numpy as np
import onnxruntime as ort


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
_MODEL_PATH = Path(__file__).with_name("assets") / "openutau" / "g2p-es.zip"

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
_FRENCH_MILLEFEUILLE_MODEL_PATH = (
    Path(__file__).with_name("assets") / "openutau" / "g2p-fr-millefeuille.zip"
)
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
_ITALIAN_MODEL_PATH = Path(__file__).with_name("assets") / "openutau" / "g2p-it.zip"
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
_PORTUGUESE_MODEL_PATH = Path(__file__).with_name("assets") / "openutau" / "g2p-pt.zip"


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

    path: Path
    graphemes: Tuple[str, ...]
    phonemes: Tuple[str, ...]
    remove_tail_digits: bool = False

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare phonemes for a normalized lyric word."""
        normalized = str(word).lower()
        dictionary, grapheme_indexes, session = _load_pack(str(self.path), self.graphemes)
        if normalized in dictionary:
            result = dictionary[normalized]
            if self.remove_tail_digits:
                return tuple(re.sub(r"\\d+$", "", phone) for phone in result)
            return result

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
        path=_PORTUGUESE_MODEL_PATH,
        graphemes=_PORTUGUESE_GRAPHEMES,
        phonemes=_PORTUGUESE_PHONEMES,
    )

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare OpenUtau Portuguese phonemes for a normalized lyric word."""
        return self._pack.phonemize(word)
