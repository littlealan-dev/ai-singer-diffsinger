from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.api.syllable_alignment import (
    _build_group_anchor_frames,
    _compress_group_to_anchor_budget,
    align,
)
from src.api.timing_errors import InfeasibleAnchorError


class _StubPhonemizer:
    def __init__(self, vowels: set[str]) -> None:
        self._vowels = set(vowels)
        self._phoneme_to_id = {"SP": 0}

    def is_vowel(self, phoneme: str) -> bool:
        return phoneme in self._vowels

    def is_glide(self, phoneme: str) -> bool:
        return False


class TestSyllableAlignmentAnchorBudget(unittest.TestCase):
    def test_compress_prefers_adjacent_and_nearest_vowel(self) -> None:
        phonemizer = _StubPhonemizer({"ao"})
        phonemes = ["g", "l", "ao", "r"]
        ids = [1, 2, 3, 4]
        lang_ids = [0, 0, 0, 0]

        out_ph, out_ids, out_lang = _compress_group_to_anchor_budget(
            phonemes=phonemes,
            ids=ids,
            lang_ids=lang_ids,
            anchor_total=3,
            phonemizer=phonemizer,  # type: ignore[arg-type]
            group_index=0,
            note_index=0,
        )

        # "l" (index 1) is adjacent to another consonant and nearest to the vowel.
        self.assertEqual(out_ph, ["g", "ao", "r"])
        self.assertEqual(out_ids, [1, 3, 4])
        self.assertEqual(out_lang, [0, 0, 0])

    def test_raises_when_all_vowels_and_budget_still_impossible(self) -> None:
        phonemizer = _StubPhonemizer({"aa", "ao"})
        with self.assertRaises(InfeasibleAnchorError):
            _compress_group_to_anchor_budget(
                phonemes=["aa", "ao"],
                ids=[1, 2],
                lang_ids=[0, 0],
                anchor_total=1,
                phonemizer=phonemizer,  # type: ignore[arg-type]
                group_index=1,
                note_index=5,
            )

    def test_same_note_run_splits_budget_with_prefix_minimal(self) -> None:
        phonemizer = _StubPhonemizer({"ey"})
        phrase_groups = [
            {
                "phonemes": ["k"],
                "ids": [1],
                "lang_ids": [0],
                "note_idx": 0,
                "position": 0,
                "tone": 60.0,
            },
            {
                "phonemes": ["ey"],
                "ids": [2],
                "lang_ids": [0],
                "note_idx": 0,
                "position": 0,
                "tone": 60.0,
            },
        ]
        positions, durations, anchors = _build_group_anchor_frames(
            phrase_groups=phrase_groups,
            note_durations=[10],
            phonemizer=phonemizer,  # type: ignore[arg-type]
        )
        self.assertEqual(positions, [0, 1])
        self.assertEqual(durations, [1, 9])
        self.assertEqual(anchors[0]["end_frame"] - anchors[0]["start_frame"], 1)
        self.assertEqual(anchors[1]["end_frame"] - anchors[1]["start_frame"], 9)

    @patch("src.api.syllable_alignment.phonemize")
    def test_phrase_initial_prefix_becomes_own_group(self, mock_phonemize) -> None:
        mock_phonemize.return_value = {
            "phonemes": ["k", "ey"],
            "phoneme_ids": [1, 2],
            "language_ids": [0, 0],
            "word_boundaries": [2],
        }
        phonemizer = _StubPhonemizer({"ey"})
        notes = [
            {
                "is_rest": False,
                "lyric": "key",
                "syllabic": "single",
                "offset_beats": 0.0,
                "duration_beats": 1.0,
                "pitch_midi": 60,
            }
        ]
        payload = align(
            notes=notes,
            start_frames=[0],
            end_frames=[12],
            timing_midi=[60.0],
            note_durations=[12],
            phonemizer=phonemizer,  # type: ignore[arg-type]
            voicebank_path=Path("."),
            include_phonemes=True,
        )
        self.assertEqual(payload["phonemes"], ["k", "ey"])
        self.assertEqual(payload["word_boundaries"], [1, 1])
        self.assertEqual(payload["group_note_indices"], [0, 0])
        self.assertEqual(payload["word_durations"], [1, 11])


if __name__ == "__main__":
    unittest.main()
