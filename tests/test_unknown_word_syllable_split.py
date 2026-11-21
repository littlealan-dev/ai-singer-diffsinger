"""Regression coverage for English syllabic chains that join unknown words."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api import parse_score, syllable_alignment
from src.api.synthesize import _init_phonemizer, align_phonemes_to_notes


_ROOT = Path(__file__).parent.parent
_FIXTURE = _ROOT / "tests/fixtures/syllabic_unknown_word_join.musicxml"
_BANKS = (
    "Qixuan_v2.7.0_DiffSinger_OpenUtau",
    "PM-31_Commercial_Indigo",
)
_BASE_NOTES = {
    _BANKS[0]: {
        0: ["SP", "n", "ao", "r", "ch"],
        1: ["ah", "z", "t"],
        5: ["eh", "v"],
        6: ["er"],
        7: ["iy", "l"],
        8: ["ah", "v", "s", "w"],
        9: [],
        10: ["ih", "m", "z"],
        11: [],
        12: ["ax", "m"],
        13: ["ey", "z"],
        14: ["ih", "ng"],
        15: [],
    },
    _BANKS[1]: {
        0: ["SP", "n", "ao", "r", "ch"],
        1: ["ah", "z", "t"],
        5: ["eh", "v"],
        6: ["er"],
        7: ["iy", "l"],
        8: ["ah", "v", "s", "w"],
        9: [],
        10: ["ih", "m", "z"],
        11: [],
        12: ["ah", "m"],
        13: ["ey", "z"],
        14: ["ih", "ng"],
        15: [],
    },
}


def _bank_path(name: str) -> Path:
    return _ROOT / "assets/voicebanks" / name


def _phonemes_by_note(payload: dict, note_count: int) -> list[list[str]]:
    by_note = [[] for _ in range(note_count)]
    offset = 0
    for count, note_idx in zip(payload["word_boundaries"], payload["group_note_indices"]):
        by_note[note_idx].extend(
            phoneme.removeprefix("en/")
            for phoneme in payload["phonemes"][offset : offset + count]
        )
        offset += count
    assert offset == len(payload["phonemes"])
    return by_note


@pytest.mark.parametrize("bank_name", _BANKS)
def test_unknown_word_chain_sings_each_syllable(
    monkeypatch: pytest.MonkeyPatch, bank_name: str
) -> None:
    bank = _bank_path(bank_name)
    if not bank.is_dir():
        pytest.skip(f"{bank_name} voicebank is not installed")
    monkeypatch.setenv("SYLLABLE_ALIGNER_V2", "1")
    score = parse_score(_FIXTURE, verse_number=1)
    payload = align_phonemes_to_notes(
        score, bank, language="en", include_phonemes=True
    )
    by_note = _phonemes_by_note(payload, len(score["parts"][0]["notes"]))

    assert "iy" in by_note[3]
    # The aligner carries an onset onto the preceding note. Compare the stream
    # from the start of "ten" through "in" without pinning the t to one note.
    span = [phoneme for note in by_note[1:5] for phoneme in note]
    assert span[span.index("t") :] == ["t", "eh", "n", "m", "iy", "ih", "n"]
    for note_idx, expected in _BASE_NOTES[bank_name].items():
        assert by_note[note_idx] == expected


@pytest.mark.parametrize("bank_name", _BANKS)
def test_is_known_word(bank_name: str) -> None:
    bank = _bank_path(bank_name)
    if not bank.is_dir():
        pytest.skip(f"{bank_name} voicebank is not installed")
    phonemizer = _init_phonemizer(
        bank, "en", needed_graphemes=["ten", "every", "tenme", "love"]
    )
    assert phonemizer.is_known_word("ten")
    assert phonemizer.is_known_word("every")
    assert not phonemizer.is_known_word("tenme")
    assert not phonemizer.is_known_word("")
    if bank_name == _BANKS[1]:
        assert phonemizer.is_known_word("love")


def test_split_pass_skips_non_english_and_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_phonemize(*args: object, **kwargs: object) -> None:
        pytest.fail("no additional phonemize call expected")

    monkeypatch.setattr(syllable_alignment, "phonemize", unexpected_phonemize)

    class KnownWordPhonemizer:
        def is_known_word(self, token: str) -> bool:
            return True

        def is_vowel(self, phoneme: str) -> bool:
            return phoneme == "eh"

    groups = [{
        "notes": [{"lyric": "ten"}, {"lyric": "me"}],
        "note_indices": [0, 1],
        "is_rest": False,
        "is_syllabic_chain": True,
        "carrier_indices": [0, 1],
        "sustain_indices": [],
    }]
    bundles = [{"phonemes": ["eh"], "ids": [1], "lang_ids": [0]}]
    for language, adapters in (("es", []), ("en", [{"example": True}]), ("en", [])):
        result_groups, result_bundles = (
            syllable_alignment.split_unknown_word_syllabic_groups(
                groups,
                bundles,
                KnownWordPhonemizer(),
                voicebank_path=Path("unused"),
                language=language,
                solfege_pronunciation_patch=False,
                pronunciation_adapters=adapters,
            )
        )
        assert result_groups is groups
        assert result_bundles is bundles
