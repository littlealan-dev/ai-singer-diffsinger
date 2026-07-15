"""
Tests for the phonemize API and underlying Phonemizer class.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
import tempfile

from src.api import phonemize
from src.phonemizer import Phonemizer, UnsupportedLyricTokenError
from src.phonemizer.language_g2p import DiffSingerSpanishPhonemizer
from src.phonemizer.language_pronunciation import (
    LanguagePronunciationRegistry,
    get_language_pronunciation_pipeline,
    prepare_lookup_lyric,
)


VOICEBANK_ROOT = Path(__file__).parent.parent / "assets/voicebanks/Raine_Rena_2.01"
PHONEMES_PATH = VOICEBANK_ROOT / "dsmain" / "phonemes.json"
DICTIONARY_PATH = VOICEBANK_ROOT / "dsvariance" / "dsdict-en.yaml" 
LANGUAGES_PATH = VOICEBANK_ROOT / "dsmain" / "languages.json"
KEIRO_ROOT = Path(__file__).parent.parent / "assets/voicebanks/Keiro_Revenant_v170/configs"
QIXUAN_ROOT = Path(__file__).parent.parent / "assets/voicebanks/Qixuan_v2.7.0_DiffSinger_OpenUtau"


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

    def test_latin_diacritics_fold_before_g2p(self) -> None:
        """Latin diacritics should fold generically for English G2P."""
        self.assertEqual(Phonemizer._normalize_word_for_g2p("à"), "a")
        self.assertEqual(Phonemizer._normalize_word_for_g2p("café"), "cafe")
        self.assertEqual(Phonemizer._normalize_word_for_g2p("naïve"), "naive")

    def test_non_latin_token_fails_before_english_g2p(self) -> None:
        """Non-Latin lyrics should not fall through to English G2P."""
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )

        with self.assertRaises(UnsupportedLyricTokenError) as ctx:
            phonemizer.phonemize_tokens(["Прийдіте"])

        self.assertEqual(ctx.exception.code, "unsupported_lyric_language")
        self.assertEqual(ctx.exception.reason, "non_latin_lyrics_for_english_g2p")
        self.assertEqual(ctx.exception.unsupported_script, "Cyrillic")

    def test_non_ascii_dictionary_entry_is_preserved_before_g2p(self) -> None:
        """Dictionary lookup should still support intentional non-ASCII entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.txt"
            dictionary_path = root / "dsdict-en.yaml"

            phonemes_path.write_text("<PAD>\nSP\nAP\nhh\n", encoding="utf8")
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: Прийдіте\n"
                "    phonemes: [hh]\n",
                encoding="utf8",
            )

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                language="en",
                allow_g2p=True,
            )

            result = phonemizer.phonemize_tokens(["Прийдіте"])
            self.assertEqual(result.phonemes, ["hh"])

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

    def test_non_english_dictionary_preserves_unicode_and_language_ids(self) -> None:
        """Dictionary-backed lyrics should work for non-English language codes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-ja.yaml"
            languages_path = root / "languages.json"

            phonemes_path.write_text(
                '{"SP": 0, "AP": 1, "ja/k": 2, "ja/a": 3}',
                encoding="utf8",
            )
            dictionary_path.write_text(
                "entries:\n"
                "  - grapheme: 歌\n"
                "    phonemes: [ja/k, ja/a]\n",
                encoding="utf8",
            )
            languages_path.write_text('{"en": 1, "ja": 2}', encoding="utf8")

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                languages_path=languages_path,
                language="ja",
            )

            result = phonemizer.phonemize_tokens(["歌"])
            self.assertEqual(result.phonemes, ["ja/k", "ja/a"])
            self.assertEqual(result.ids, [2, 3])
            self.assertEqual(result.language_ids, [2, 2])

    def test_unicode_grapheme_normalization_keeps_accents(self) -> None:
        self.assertEqual(Phonemizer._normalize_grapheme("  Canción! "), "canción")

    def test_non_english_missing_entry_does_not_use_english_g2p(self) -> None:
        """Unknown non-English words must not be pronounced by g2p_en."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-ja.yaml"
            languages_path = root / "languages.json"
            phonemes_path.write_text('{"SP": 0, "AP": 1}', encoding="utf8")
            dictionary_path.write_text("entries: []\n", encoding="utf8")
            languages_path.write_text('{"ja": 2}', encoding="utf8")

            phonemizer = Phonemizer(
                phonemes_path=phonemes_path,
                dictionary_path=dictionary_path,
                languages_path=languages_path,
                language="ja",
                allow_g2p=True,
            )

            with self.assertRaisesRegex(KeyError, "G2P fallback is not available"):
                phonemizer.phonemize_tokens(["未知"])

    def test_spanish_missing_dictionary_entry_uses_openutau_fallback(self) -> None:
        """OpenUtau's Spanish G2P handles ordinary words absent from dsdict-es."""
        result = phonemize(["Salve,"], KEIRO_ROOT, language="es")

        self.assertEqual(result["phonemes"], ["es/s", "es/a", "es/l", "es/B", "es/e"])
        self.assertEqual(result["language_ids"], [4, 4, 4, 4, 4])

    def test_spanish_g2p_is_resolved_by_the_language_registry(self) -> None:
        """Fallback and romanization selection is declarative, not conditional."""
        pipeline = get_language_pronunciation_pipeline("es")

        self.assertIsInstance(pipeline.g2p_fallback, DiffSingerSpanishPhonemizer)
        self.assertEqual(
            LanguagePronunciationRegistry.registered_languages(),
            ("en", "es", "ja", "zh"),
        )

    def test_qixuan_japanese_kana_uses_romaji_dictionary_entries(self) -> None:
        """Kana is romanized before Qixuan's dsdict-ja lookup, as in OpenUtau."""
        result = phonemize(["か", "キャ", "っ", "ティ"], QIXUAN_ROOT, language="ja")

        self.assertEqual(
            result["phonemes"],
            ["ja/k", "ja/a", "ja/ky", "ja/a", "ja/cl", "ja/t", "ja/i"],
        )
        self.assertEqual(result["language_ids"], [2, 2, 2, 2, 2, 2, 2])
        self.assertEqual(result["word_boundaries"], [2, 2, 1, 2])

    def test_qixuan_japanese_multimora_lyric_uses_dictionary_mora_fallback(self) -> None:
        """A normal score may place several Japanese morae on one note."""
        result = phonemize(["そう", "ああ", "きょう"], QIXUAN_ROOT, language="ja")

        self.assertEqual(
            result["phonemes"],
            [
                "ja/s", "ja/o", "ja/o",
                "ja/a", "ja/a",
                "ja/ky", "ja/o", "ja/o",
            ],
        )
        self.assertEqual(result["word_boundaries"], [3, 2, 3])

    def test_qixuan_chinese_hanzi_uses_phrase_pinyin_dictionary_entries(self) -> None:
        """Hanzi is romanized to tone-less Pinyin before Qixuan's dsdict-zh lookup."""
        result = phonemize(["你", "好"], QIXUAN_ROOT, language="zh")

        self.assertEqual(result["phonemes"], ["zh/n", "zh/i", "zh/h", "zh/ao"])
        self.assertEqual(result["language_ids"], [3, 3, 3, 3])
        self.assertEqual(result["word_boundaries"], [2, 2])

    def test_qixuan_chinese_strips_full_and_half_width_display_punctuation(self) -> None:
        """Mixed display punctuation must not be passed to the Chinese dictionary."""
        result = phonemize(["（巷，", "巷!）"], QIXUAN_ROOT, language="zh")

        self.assertEqual(result["phonemes"], ["zh/x", "zh/iang", "zh/x", "zh/iang"])
        self.assertEqual(result["word_boundaries"], [2, 2])

        prepared = get_language_pronunciation_pipeline("zh").prepare(["（巷，"])
        self.assertEqual(prepared[0].original, "（巷，")
        self.assertEqual(prepared[0].lookup, "xiang")

    def test_display_cleanup_removes_unicode_numbers_and_punctuation(self) -> None:
        """Numeric notation is display-only; scores must use singable lyric text."""
        chinese = get_language_pronunciation_pipeline("zh").prepare(["1.（巷，", "７！", "Ⅶ"])
        japanese = get_language_pronunciation_pipeline("ja").prepare(["7あ、", "東京！"])

        self.assertEqual([lyric.lookup for lyric in chinese], ["xiang", "", ""])
        self.assertEqual([lyric.lookup for lyric in japanese], ["a", "東京"])
        self.assertEqual(chinese[0].original, "1.（巷，")
        self.assertEqual(prepare_lookup_lyric("7don’t!", language="en"), "don't")

    def test_numeric_or_punctuation_only_lyric_requires_singable_text(self) -> None:
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            languages_path=LANGUAGES_PATH,
            language="en",
        )

        with self.assertRaisesRegex(UnsupportedLyricTokenError, "contains only numbers or display punctuation"):
            phonemizer.phonemize_tokens(["７！"])

    def test_qixuan_chinese_ignores_redundant_score_verse_label(self) -> None:
        """Existing parsed scores may retain a display label in their first lyric."""
        result = phonemize(["1.\u00a0每", "条"], QIXUAN_ROOT, language="zh")

        self.assertEqual(result["phonemes"], ["zh/m", "zh/ei", "zh/t", "zh/iao"])
        self.assertEqual(result["word_boundaries"], [2, 2])

    def test_qixuan_chinese_selective_dictionary_load_uses_phrase_pinyin(self) -> None:
        """Selective loading must prepare Hanzi before looking up dictionary entries."""
        phonemizer = Phonemizer(
            phonemes_path=QIXUAN_ROOT / "dsdur" / "0102_qixuan_newdict_dur.phonemes.json",
            dictionary_path=QIXUAN_ROOT / "dsdur" / "dsdict-zh.yaml",
            languages_path=QIXUAN_ROOT / "dsdur" / "0102_qixuan_newdict_dur.languages.json",
            language="zh",
            needed_graphemes=["重", "庆"],
        )

        result = phonemizer.phonemize_tokens(["重", "庆"])
        self.assertEqual(result.phonemes, ["zh/ch", "zh/ong", "zh/q", "zh/ing"])

    def test_japanese_kanji_is_not_silently_romanized(self) -> None:
        """Match OpenUtau: Japanese DiffSinger romanizes Kana, not Kanji."""
        prepared = get_language_pronunciation_pipeline("ja").prepare(["日", "に"])

        self.assertEqual([lyric.lookup for lyric in prepared], ["日", "ni"])

    def test_chinese_romanizer_preserves_phrase_token_alignment(self) -> None:
        prepared = get_language_pronunciation_pipeline("zh").prepare(["重", "庆", "！", "你"])

        self.assertEqual([lyric.lookup for lyric in prepared], ["chong", "qing", "", "ni"])

    def test_language_must_exist_in_voicebank_language_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            phonemes_path = root / "phonemes.json"
            dictionary_path = root / "dsdict-ja.yaml"
            languages_path = root / "languages.json"
            phonemes_path.write_text('{"SP": 0, "AP": 1}', encoding="utf8")
            dictionary_path.write_text("entries: []\n", encoding="utf8")
            languages_path.write_text('{"en": 1}', encoding="utf8")

            with self.assertRaisesRegex(ValueError, "Language 'ja'"):
                Phonemizer(
                    phonemes_path=phonemes_path,
                    dictionary_path=dictionary_path,
                    languages_path=languages_path,
                    language="ja",
                )

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
                "  - grapheme: 'how'\n"
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
                "  - grapheme: 'how'\n"
                "    phonemes:\n"
                "      - en/hh\n"
                "      - en/aw\n"
                "  - grapheme: world\n"
                "    phonemes:\n"
                "      - en/w\n"
                "      - en/er\n"
                "      - en/l\n"
                "      - en/d\n"
                "replacements:\n"
                "  - {from: aw, to: en/aw}\n"
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
            self.assertEqual(phonemizer._dictionary_replacements, {"aw": "en/aw"})
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
