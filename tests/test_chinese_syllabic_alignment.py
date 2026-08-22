from __future__ import annotations

from pathlib import Path

import pytest

from src.api import parse_score
from src.api import syllable_alignment
from src.api.synthesize import align_phonemes_to_notes


_ROOT = Path(__file__).parent.parent
_FIXTURE = _ROOT / "tests/fixtures/chinese_grouped_and_single_character.musicxml"
_LIEE = _ROOT / "assets/voicebanks/Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"


def test_chinese_syllabic_chain_preserves_hanzi_events_and_extension_timing() -> None:
    score = parse_score(_FIXTURE, verse_number=1)
    notes = score["parts"][0]["notes"]

    raw_groups = syllable_alignment._group_notes(notes)
    raw_lyric_groups = [group for group in raw_groups if not group["is_rest"]]
    assert [syllable_alignment._resolve_group_lyric(group) for group in raw_lyric_groups] == [
        "恩典、",
        "美",
        "丽",
    ]

    for language in ("zh", "zh-yue"):
        groups = syllable_alignment.split_chinese_syllabic_groups(raw_groups, language)
        lyric_groups = [group for group in groups if not group["is_rest"]]
        assert [syllable_alignment._resolve_group_lyric(group) for group in lyric_groups] == [
            "恩",
            "典、",
            "美",
            "丽",
        ]
        assert lyric_groups[0]["note_indices"] == [0, 1]
        assert lyric_groups[0]["sustain_indices"] == [1]
        assert lyric_groups[1]["note_indices"] == [2]


@pytest.mark.skipif(not _LIEE.is_dir(), reason="LIEE voicebank is not installed")
@pytest.mark.parametrize("language", ["zh", "zh-yue"])
@pytest.mark.parametrize("v2", [False, True])
def test_liee_aligns_grouped_and_single_character_chinese(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    v2: bool,
) -> None:
    """Both aligners must G2P per Hanzi and retain the preceding extension."""
    monkeypatch.setenv("SYLLABLE_ALIGNER_V2", "1" if v2 else "0")
    payload = align_phonemes_to_notes(
        parse_score(_FIXTURE, verse_number=1),
        _LIEE,
        language=language,
        include_phonemes=True,
    )

    assert payload["phonemes"]
    assert all(not "\u3400" <= phoneme[:1] <= "\u9fff" for phoneme in payload["phonemes"])
    # The extension notes remain timing-only holds rather than receiving a
    # second syllable onset. The lyric carriers are notes 0/2 and 4/6.
    if v2:
        assert payload["note_slur"][1] == 1
        assert payload["note_slur"][5] == 1
        assert payload["note_slur"][2] == 0
        assert payload["note_slur"][6] == 0
    else:
        assert [0, 1] in payload["slur_groups"]
        assert [4, 5] in payload["slur_groups"]
        assert [0, 1, 2] not in payload["slur_groups"]
