"""Spanish G2P fallback compatible with OpenUtau's DiffSinger phonemizer.

The bundled model is OpenUtau's ``g2p-es.zip`` resource.  OpenUtau uses its
word dictionary first and then this model for a Spanish word that is absent
from ``dsdict-es.yaml``; this module follows the same order.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
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

    @lru_cache(maxsize=4096)
    def phonemize(self, word: str) -> Tuple[str, ...]:
        """Return bare phonemes for a normalized lyric word."""
        normalized = str(word).lower()
        dictionary, grapheme_indexes, session = _load_pack(str(self.path), self.graphemes)
        if normalized in dictionary:
            return dictionary[normalized]

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
