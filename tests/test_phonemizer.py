"""
Tests for the phonemize API and underlying Phonemizer class.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from src.api import phonemize
from src.phonemizer import Phonemizer


VOICEBANK_ROOT = Path(__file__).parent.parent / "assets/voicebanks/Raine_Rena_2.01"
PHONEMES_PATH = VOICEBANK_ROOT / "dsmain" / "phonemes.json"
DICTIONARY_PATH = VOICEBANK_ROOT / "dsvariance" / "dsdict-en.yaml" 
LANGUAGES_PATH = VOICEBANK_ROOT / "dsmain" / "languages.json"


class PhonemizerClassTests(unittest.TestCase):
    """Tests for the underlying Phonemizer class."""
    
    def test_missing_dictionary_raises(self) -> None:
        """Missing dictionary should raise FileNotFoundError."""
        missing_path = Path("assets/voicebanks/missing/dsdict.yaml")
        with self.assertRaises(FileNotFoundError) as ctx:
            Phonemizer(
                phonemes_path=PHONEMES_PATH,
                dictionary_path=missing_path,
                languages_path=LANGUAGES_PATH,
                language="en",
            )
        self.assertIn("dsdict.yaml", str(ctx.exception))

    def test_g2p_fallback_with_language_id(self) -> None:
        """G2P should produce phonemes with language prefixes."""
        try:
            import nltk
            nltk.data.find("corpora/cmudict")
        except Exception:
            self.skipTest("cmudict is not available for g2p_en.")
            
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )
        result = phonemizer.phonemize_tokens(["amazing"])
        self.assertGreater(len(result.phonemes), 0)
        self.assertEqual(len(result.phonemes), len(result.ids))
        self.assertEqual(len(result.phonemes), len(result.language_ids))
        
        # Check that language IDs are correct (English = 1)
        for ph in result.phonemes:
            self.assertTrue(ph.startswith("en/"))
        for lang_id in result.language_ids:
            self.assertEqual(lang_id, 1)

    def test_missing_token_without_g2p_raises(self) -> None:
        """Unknown token without G2P should raise KeyError."""
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
            allow_g2p=False,
        )
        with self.assertRaises(KeyError):
            phonemizer.phonemize_tokens(["zzzzzz"])

    def test_direct_phoneme_tokens(self) -> None:
        """Phoneme tokens should pass through unchanged."""
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )
        result = phonemizer.phonemize_tokens(["SP", "en/aa"])
        self.assertEqual(result.phonemes, ["SP", "en/aa"])
        self.assertEqual(result.language_ids, [0, 1])


class PhonemizeAPITests(unittest.TestCase):
    """Tests for the phonemize API function."""
    
    @classmethod
    def setUpClass(cls):
        if not VOICEBANK_ROOT.exists():
            raise unittest.SkipTest(f"Voicebank not found at {VOICEBANK_ROOT}")
        try:
            import nltk
            nltk.data.find("corpora/cmudict")
        except Exception:
            raise unittest.SkipTest("cmudict not available for g2p_en")
    
    def test_phonemize_api_returns_dict(self):
        """phonemize API should return structured dict."""
        result = phonemize(["hello"], VOICEBANK_ROOT)
        
        self.assertIsInstance(result, dict)
        self.assertIn("phonemes", result)
        self.assertIn("phoneme_ids", result)
        self.assertIn("language_ids", result)
        self.assertIn("word_boundaries", result)
    
    def test_phonemize_api_multiple_words(self):
        """phonemize API should handle multiple words."""
        result = phonemize(["hello", "world", "test"], VOICEBANK_ROOT)
        
        self.assertEqual(len(result["word_boundaries"]), 3)
        total_phonemes = sum(result["word_boundaries"])
        self.assertEqual(len(result["phonemes"]), total_phonemes)
    
    def test_phonemize_api_empty_input(self):
        """phonemize API should handle empty input."""
        result = phonemize([], VOICEBANK_ROOT)
        
        self.assertEqual(len(result["phonemes"]), 0)
        self.assertEqual(len(result["word_boundaries"]), 0)


if __name__ == "__main__":
    unittest.main()
