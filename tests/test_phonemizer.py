"""
Tests for the phonemize API and underlying Phonemizer class.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
import tempfile

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

    def test_dictionary_phonemes_fall_back_to_bare_inventory_symbol(self) -> None:
        """If en/x is missing but bare x exists, validation should use the bare symbol."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-en.yaml"
            languages_path = root / "languages.json"

            phonemes_path.write_text('{"SP": 0, "AP": 1, "hh": 2, "en/aw": 3}', encoding="utf8")
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: how\n"
                "    phonemes: [en/hh, en/aw]\n",
                encoding="utf8",
            )
            languages_path.write_text('{"en": 1}', encoding="utf8")

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                languages_path=languages_path,
                language="en",
                allow_g2p=False,
            )

            result = phonemizer.phonemize_tokens(["how"])
            self.assertEqual(result.phonemes, ["hh", "en/aw"])
            self.assertEqual(result.ids, [2, 3])
            self.assertEqual(result.language_ids, [0, 1])

    def test_text_phoneme_inventory_loads_with_sequential_ids(self) -> None:
        """Plain text phoneme inventories should assign ids by line order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.txt"
            dictionary_path = root / "dsdict-en.yaml"

            phonemes_path.write_text("<PAD>\nSP\nAP\nhh\naw\n", encoding="utf8")
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: how\n"
                "    phonemes: [hh, aw]\n",
                encoding="utf8",
            )

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                language="en",
                allow_g2p=False,
            )

            self.assertEqual(phonemizer._phoneme_to_id["<PAD>"], 0)
            self.assertEqual(phonemizer._phoneme_to_id["SP"], 1)
            self.assertEqual(phonemizer._phoneme_to_id["AP"], 2)
            result = phonemizer.phonemize_tokens(["how"])
            self.assertEqual(result.phonemes, ["hh", "aw"])
            self.assertEqual(result.ids, [3, 4])

    def test_text_phoneme_inventory_ignores_comments_and_blank_lines(self) -> None:
        """Text phoneme inventories should ignore comments and blank lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.txt"
            dictionary_path = root / "dsdict-en.yaml"

            phonemes_path.write_text(
                "# comment\n"
                "<PAD>\n"
                "\n"
                "; semicolon comment\n"
                "SP\n"
                "AP\n"
                "hh\n",
                encoding="utf8",
            )
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: hush\n"
                "    phonemes: [hh]\n",
                encoding="utf8",
            )

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                language="en",
                allow_g2p=False,
            )

            self.assertEqual(phonemizer._phoneme_to_id, {"<PAD>": 0, "SP": 1, "AP": 2, "hh": 3})

    def test_text_phoneme_inventory_rejects_duplicates(self) -> None:
        """Duplicate symbols in a text phoneme inventory should fail loudly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.txt"
            dictionary_path = root / "dsdict-en.yaml"

            phonemes_path.write_text("<PAD>\nSP\nSP\n", encoding="utf8")
            dictionary_path.write_text("entries: []\n", encoding="utf8")

            with self.assertRaises(ValueError) as ctx:
                Phonemizer(
                    phonemes_path=phonemes_path,
                    dictionary_path=dictionary_path,
                    language="en",
                    allow_g2p=False,
                )
            self.assertIn("Duplicate phoneme 'SP'", str(ctx.exception))

    def test_text_inventory_dictionary_phonemes_fall_back_to_bare_inventory_symbol(self) -> None:
        """Bare symbols in text inventory should satisfy en/x dictionary phonemes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.txt"
            dictionary_path = root / "dsdict-en.yaml"
            languages_path = root / "languages.json"

            phonemes_path.write_text("<PAD>\nSP\nAP\nhh\nen/aw\n", encoding="utf8")
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: how\n"
                "    phonemes: [en/hh, en/aw]\n",
                encoding="utf8",
            )
            languages_path.write_text('{"en": 1}', encoding="utf8")

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                languages_path=languages_path,
                language="en",
                allow_g2p=False,
            )

            result = phonemizer.phonemize_tokens(["how"])
            self.assertEqual(result.phonemes, ["hh", "en/aw"])
            self.assertEqual(result.ids, [3, 4])
            self.assertEqual(result.language_ids, [0, 1])

    def test_small_dictionary_keeps_eager_load_strategy(self) -> None:
        """Normal-sized dictionaries should preserve the eager YAML load path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-en.yaml"
            languages_path = root / "languages.json"

            phonemes_path.write_text('{"SP": 0, "AP": 1, "en/hh": 2, "en/aw": 3}', encoding="utf8")
            dictionary_path.write_text(
                "symbols:\n"
                "  - symbol: SP\n"
                "    type: vowel\n"
                "  - symbol: AP\n"
                "    type: vowel\n"
                "entries:\n"
                "  - grapheme: how\n"
                "    phonemes: [en/hh, en/aw]\n",
                encoding="utf8",
            )
            languages_path.write_text('{"en": 1}', encoding="utf8")

            with unittest.mock.patch.dict(os.environ, {"VOICEBANK_LARGE_DICT_THRESHOLD_BYTES": "1000000"}, clear=False):
                phonemizer = Phonemizer(
                    phonemes_path=phonemes_path,
                    dictionary_path=dictionary_path,
                    languages_path=languages_path,
                    language="en",
                    allow_g2p=False,
                    needed_graphemes={"how"},
                )

            self.assertEqual(phonemizer._dictionary_load_strategy, "eager")
            result = phonemizer.phonemize_tokens(["how"])
            self.assertEqual(result.phonemes, ["en/hh", "en/aw"])

    def test_large_dictionary_uses_selective_load_strategy(self) -> None:
        """Oversized dictionaries should load only requested graphemes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-en.yaml"
            languages_path = root / "languages.json"

            phonemes_path.write_text(
                '{"SP": 0, "AP": 1, "en/hh": 2, "en/aw": 3, "en/w": 4, "en/er": 5, "en/l": 6, "en/d": 7}',
                encoding="utf8",
            )
            dictionary_path.write_text(
                "symbols:\n"
                "  - symbol: SP\n"
                "    type: vowel\n"
                "  - symbol: AP\n"
                "    type: vowel\n"
                "  - symbol: en/aw\n"
                "    type: vowel\n"
                "  - symbol: en/er\n"
                "    type: vowel\n"
                "entries:\n"
                "  - grapheme: how\n"
                "    phonemes:\n"
                "      - en/hh\n"
                "      - en/aw\n"
                "  - grapheme: world\n"
                "    phonemes:\n"
                "      - en/w\n"
                "      - en/er\n"
                "      - en/l\n"
                "      - en/d\n"
                + ("# filler to force selective path\n" * 100),
                encoding="utf8",
            )
            languages_path.write_text('{"en": 1}', encoding="utf8")

            with unittest.mock.patch.dict(os.environ, {"VOICEBANK_LARGE_DICT_THRESHOLD_BYTES": "1"}, clear=False):
                phonemizer = Phonemizer(
                    phonemes_path=phonemes_path,
                    dictionary_path=dictionary_path,
                    languages_path=languages_path,
                    language="en",
                    allow_g2p=False,
                    needed_graphemes={"how"},
                )

            self.assertEqual(phonemizer._dictionary_load_strategy, "selective")
            self.assertEqual(phonemizer._dictionary, {"how": ["en/hh", "en/aw"]})
            self.assertTrue(phonemizer.is_vowel("SP"))
            self.assertTrue(phonemizer.is_vowel("en/aw"))
            result = phonemizer.phonemize_tokens(["how"])
            self.assertEqual(result.phonemes, ["en/hh", "en/aw"])


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
