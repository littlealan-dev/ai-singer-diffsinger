from __future__ import annotations

import unittest
from pathlib import Path

from src.phonemizer import Phonemizer


VOICEBANK_ROOT = Path("assets/voicebanks/Raine_Rena_2.01")
PHONEMES_PATH = VOICEBANK_ROOT / "dsmain" / "phonemes.json"
DICTIONARY_PATH = VOICEBANK_ROOT / "dsvariance" / "dsdict.yaml"


class PhonemizerTests(unittest.TestCase):
    def test_missing_dictionary_raises(self) -> None:
        missing_path = Path("assets/voicebanks/missing/dsdict.yaml")
        with self.assertRaises(FileNotFoundError) as ctx:
            Phonemizer(
                phonemes_path=PHONEMES_PATH,
                dictionary_path=missing_path,
                language="en",
            )
        self.assertIn("dsdict.yaml", str(ctx.exception))

    def test_dictionary_entry_maps_to_ids(self) -> None:
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            language="en",
        )
        result = phonemizer.phonemize_tokens(["-"])
        self.assertEqual(result.phonemes, ["SP"])
        self.assertEqual(result.ids, [phonemizer._phoneme_to_id["SP"]])

    def test_missing_token_raises(self) -> None:
        phonemizer = Phonemizer(
            phonemes_path=PHONEMES_PATH,
            dictionary_path=DICTIONARY_PATH,
            language="en",
        )
        with self.assertRaises(KeyError):
            phonemizer.phonemize_tokens(["zzzzzz"])


if __name__ == "__main__":
    unittest.main()
