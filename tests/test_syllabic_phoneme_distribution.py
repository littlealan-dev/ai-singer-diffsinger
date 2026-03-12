from __future__ import annotations

import unittest
from pathlib import Path

from src.api.synthesize import (
    _build_phoneme_groups,
    _compute_note_timing,
    _group_notes,
    _init_phonemizer,
    _resolve_group_lyric,
    _split_phonemize_result,
    phonemize,
)


class TestSyllabicPhonemeDistribution(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.voicebank = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        if not self.voicebank.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank}")

    def test_begin_end_same_pitch_distributes_to_both_notes(self) -> None:
        # Synthetic two-note syllabic split on same pitch ("voic-es")
        notes = [
            {
                "is_rest": False,
                "offset_beats": 0.0,
                "duration_beats": 0.5,
                "pitch_midi": 65.0,
                "lyric": "voic",
                "syllabic": "begin",
                "lyric_is_extended": False,
                "tie_type": None,
            },
            {
                "is_rest": False,
                "offset_beats": 0.5,
                "duration_beats": 0.5,
                "pitch_midi": 65.0,
                "lyric": "es",
                "syllabic": "end",
                "lyric_is_extended": False,
                "tie_type": None,
            },
        ]
        tempos = [{"offset_beats": 0.0, "bpm": 120.0}]
        frame_ms = 512 / 44100 * 1000
        start, end, _, midi, _ = _compute_note_timing(notes, tempos, frame_ms)

        groups = _group_notes(notes)
        self.assertEqual(len(groups), 1)
        self.assertEqual(_resolve_group_lyric(groups[0]), "voices")

        phoneme_result = phonemize(["voices"], self.voicebank)
        word_phonemes = _split_phonemize_result(phoneme_result)
        phonemizer = _init_phonemizer(self.voicebank)
        aligned = _build_phoneme_groups(
            groups,
            start,
            end,
            midi,
            phonemizer,
            word_phonemes,
        )

        note0 = aligned["note_phonemes"].get(0, [])
        note1 = aligned["note_phonemes"].get(1, [])
        self.assertTrue(note0, "first syllable note should have phonemes")
        self.assertTrue(note1, "second syllable note should have phonemes")
        self.assertTrue(
            any(phonemizer.is_vowel(ph) for ph in note0),
            "first syllable should include vowel phoneme",
        )
        self.assertTrue(
            any(phonemizer.is_vowel(ph) for ph in note1),
            "second syllable should include vowel phoneme",
        )

    def test_voices_and_gratitude_each_syllable_on_eighth_note(self) -> None:
        # 5 eighth-notes at 120 BPM:
        # voic-es (2 syllables) + grat-i-tude (3 syllables)
        notes = [
            {
                "is_rest": False,
                "offset_beats": 0.0,
                "duration_beats": 0.5,
                "pitch_midi": 65.0,
                "lyric": "voic",
                "syllabic": "begin",
                "lyric_is_extended": False,
                "tie_type": None,
            },
            {
                "is_rest": False,
                "offset_beats": 0.5,
                "duration_beats": 0.5,
                "pitch_midi": 65.0,
                "lyric": "es",
                "syllabic": "end",
                "lyric_is_extended": False,
                "tie_type": None,
            },
            {
                "is_rest": False,
                "offset_beats": 1.0,
                "duration_beats": 0.5,
                "pitch_midi": 67.0,
                "lyric": "grat",
                "syllabic": "begin",
                "lyric_is_extended": False,
                "tie_type": None,
            },
            {
                "is_rest": False,
                "offset_beats": 1.5,
                "duration_beats": 0.5,
                "pitch_midi": 67.0,
                "lyric": "i",
                "syllabic": "middle",
                "lyric_is_extended": False,
                "tie_type": None,
            },
            {
                "is_rest": False,
                "offset_beats": 2.0,
                "duration_beats": 0.5,
                "pitch_midi": 67.0,
                "lyric": "tude",
                "syllabic": "end",
                "lyric_is_extended": False,
                "tie_type": None,
            },
        ]
        tempos = [{"offset_beats": 0.0, "bpm": 120.0}]
        frame_ms = 512 / 44100 * 1000
        start, end, _, midi, _ = _compute_note_timing(notes, tempos, frame_ms)

        groups = _group_notes(notes)
        self.assertEqual(len(groups), 2)
        self.assertEqual(_resolve_group_lyric(groups[0]), "voices")
        self.assertEqual(_resolve_group_lyric(groups[1]), "gratitude")

        phonemizer = _init_phonemizer(self.voicebank)
        all_word_phonemes = _split_phonemize_result(
            phonemize(["voices", "gratitude"], self.voicebank)
        )
        aligned = _build_phoneme_groups(
            groups,
            start,
            end,
            midi,
            phonemizer,
            all_word_phonemes,
        )

        # Each syllable note should have its own phonemes.
        for idx in range(5):
            per_note = aligned["note_phonemes"].get(idx, [])
            self.assertTrue(per_note, f"note {idx} should have phonemes")
            self.assertTrue(
                any(phonemizer.is_vowel(ph) for ph in per_note),
                f"note {idx} should contain a vowel phoneme",
            )

        # Per-note sung durations should remain uniform for all five eighth-notes.
        self.assertEqual(len(aligned["word_durations"]), 5)
        self.assertLessEqual(
            max(aligned["word_durations"]) - min(aligned["word_durations"]),
            1,
            "word durations should stay equal within 1 frame rounding tolerance",
        )


if __name__ == "__main__":
    unittest.main()
