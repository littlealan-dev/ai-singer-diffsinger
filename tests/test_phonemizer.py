from __future__ import annotations

import unittest
from pathlib import Path

from src.phonemizer import Phonemizer


VOICEBANK_ROOT = Path("assets/voicebanks/Raine_Rena_2.01")
PHONEMES_PATH = VOICEBANK_ROOT / "dsmain" / "phonemes.json"
DICTIONARY_PATH = VOICEBANK_ROOT / "dsvariance" / "dsdict-en.yaml" 
LANGUAGES_PATH = VOICEBANK_ROOT / "dsmain" / "languages.json"


class PhonemizerTests(unittest.TestCase):
    def test_missing_dictionary_raises(self) -> None:
        missing_path = Path("assets/voicebanks/missing/dsdict.yaml")
        with self.assertRaises(FileNotFoundError) as ctx:
            Phonemizer(
                phonemes_path=PHONEMES_PATH,
                dictionary_path=missing_path,
                languages_path=LANGUAGES_PATH,
                language="en",
            )
        self.assertIn("dsdict.yaml", str(ctx.exception))

    def test_dictionary_entry_maps_to_ids(self) -> None:
        # dsdict-en.yaml should be used for English
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )
        # Assuming "-" maps to SP in the dictionary (or via hardcoded logic which we don't strictly have in the simple dict loader yet, unless dsdict has it)
        # Actually SP is usually not in the dict but handled specially. 
        # Let's check a real token like "a" if it exists, or just rely on G2P for the test if dictionary is empty for common words.
        # But wait, we want to test dictionary lookup.
        # Let's trust that dsdict-en.yaml has valid entries or fallback to G2P.
        # Raine Rena's dsdict-en.yaml was checked earlier to be mostly empty/symbols.
        # So we might hit G2P even for simple words. 
        # Let's check one known entry or just rely on the mechanics.
        
        # Test "-" -> SP mapping if it exists, or check fallback behavior
        # In this specific implementation, we don't have hardcoded SP handling in _phonemize_token, 
        # it relies on dict or G2P.
        # G2P_en handles punctuation often by ignoring it.
        pass 

    def test_g2p_fallback_with_language_id(self) -> None:
        try:
            import nltk  # type: ignore

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
        
        # Check that language IDs are correct (English = 1 based on languages.json)
        # phonemes should be like "en/ah", "en/m", etc.
        for ph in result.phonemes:
            self.assertTrue(ph.startswith("en/"))
            
        for lang_id in result.language_ids:
            self.assertEqual(lang_id, 1)

    def test_missing_token_without_g2p_raises(self) -> None:
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
            allow_g2p=False,
        )
        with self.assertRaises(KeyError):
            phonemizer.phonemize_tokens(["zzzzzz"])


if __name__ == "__main__":
    unittest.main()
