"""
Tests for the phoneme logic handler in Phonemizer.
"""

import unittest
from pathlib import Path
from src.phonemizer.phonemizer import Phonemizer

# Re-use paths from test_phonemizer.py
VOICEBANK_ROOT = Path(__file__).parent.parent / "assets/voicebanks/Raine_Rena_2.01"
PHONEMES_PATH = VOICEBANK_ROOT / "dsmain" / "phonemes.json"
DICTIONARY_PATH = VOICEBANK_ROOT / "dsvariance" / "dsdict-en.yaml" 
LANGUAGES_PATH = VOICEBANK_ROOT / "dsmain" / "languages.json"

from src.phonemizer.phoneme_logic_handler import PhonemeLogicHandler
from src.phonemizer.phoneme_logic_handler_en import EnglishPhonemeLogicHandler

class PhonemeLogicHandlerTests(unittest.TestCase):
    """Tests for distribute_slur method."""

    def setUp(self):
        if not VOICEBANK_ROOT.exists():
            self.skipTest(f"Voicebank not found at {VOICEBANK_ROOT}")
        
        self.phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )

    def test_distribute_slur_english_but(self):
        """Test 'but' (b ah t) distributed over 3 notes."""
        # "but" -> b ah t -> en/b, en/ah, en/t
        phonemes = ["en/b", "en/ah", "en/t"]
        note_count = 3
        
        # Expected:
        # Note 1: b ah
        # Note 2: ah
        # Note 3: ah t
        expected = [
            ["en/b", "en/ah"],
            ["en/ah"],
            ["en/ah", "en/t"]
        ]
        
        result = self.phonemizer.distribute_slur(phonemes, note_count)
        self.assertEqual(result, expected)

    def test_distribute_slur_english_sing(self):
        """Test 'sing' (s ih ng) distributed over 3 notes."""
        # "sing" -> s ih ng -> en/s, en/ih, en/ng
        phonemes = ["en/s", "en/ih", "en/ng"]
        note_count = 3
        
        # Expected:
        # Note 1: s ih
        # Note 2: ih
        # Note 3: ih ng
        expected = [
            ["en/s", "en/ih"],
            ["en/ih"],
            ["en/ih", "en/ng"]
        ]
        
        result = self.phonemizer.distribute_slur(phonemes, note_count)
        self.assertEqual(result, expected)

    def test_distribute_slur_english_am(self):
        """Test 'am' (ae m) distributed over 3 notes."""
        # "am" -> ae m -> en/ae, en/m
        phonemes = ["en/ae", "en/m"]
        note_count = 3
        
        # Expected:
        # Note 1: ae
        # Note 2: ae
        # Note 3: ae m
        expected = [
            ["en/ae"],
            ["en/ae"],
            ["en/ae", "en/m"]
        ]
        
        result = self.phonemizer.distribute_slur(phonemes, note_count)
        self.assertEqual(result, expected)

    def test_distribute_slur_single_note(self):
        """Test distribution over 1 note (should just return all phonemes)."""
        phonemes = ["en/b", "en/ah", "en/t"]
        note_count = 1
        
        # Note: logic might handle this or synthesize.py might not call it.
        # But if called, it should probably return [phonemes] or similar.
        # However, synthesize.py only calls it if len(note_indices) > 1.
        # So we can skip or test behavior. 
        # Let's assume the method handles it gracefully.
        
        result = self.phonemizer.distribute_slur(phonemes, note_count)
        expected = [["en/b", "en/ah", "en/t"]]
        self.assertEqual(result, expected)

    def test_distribute_slur_non_english(self):
        """Test that non-English languages return default handler (None result)."""
        from src.phonemizer.phoneme_logic_handler import get_phoneme_logic_handler
        
        # Japanese (ja) has no specific handler file, so it should return base handler
        handler = get_phoneme_logic_handler("ja")
        
        # The base handler's distribute_slur returns None
        result = handler.distribute_slur(["a"], 2, self.phonemizer)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
