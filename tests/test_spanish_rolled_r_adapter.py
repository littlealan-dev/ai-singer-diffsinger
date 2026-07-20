from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from src.api import parse_score
from src.api.syllable_alignment import align
from src.api.synthesize import _apply_anchor_constrained_timing, align_phonemes_to_notes


_ROOT = Path(__file__).parent.parent
_QIXUAN_ROOT = _ROOT / "assets/voicebanks/Qixuan_v2.7.0_DiffSinger_OpenUtau"
_GUANTANAMERA_SCORE = _ROOT / "assets/test_data/guantanamera-lead-sheet-with-lyrics.mxl"


_ROLLED_R_ADAPTER = {
    "id": "qixuan_es_jp_rolled_r",
    "logical_symbol": "@qixuan_es_rr_roll",
    "collapse_phonemes": ["ja/r", "ja/a", "ja/r", "ja/a", "ja/r"],
    "expand_phonemes": ["ja/r", "ja/a", "ja/r", "ja/a", "ja/r"],
    "prefix_frames": [3, 3, 3, 3],
    "main_onset_frames": 2,
}


class _StubPhonemizer:
    _phoneme_to_id = {
        "SP": 0,
        "ja/r": 1,
        "ja/a": 2,
        "ja/o": 3,
        "ja/s": 4,
    }
    _language_map = {"ja": 2}

    def is_vowel(self, phoneme: str) -> bool:
        return phoneme in {"ja/a", "ja/o"}

    def is_glide(self, phoneme: str) -> bool:
        return False


def test_qixuan_spanish_roll_expands_after_logical_alignment() -> None:
    """Only the aligned runtime payload contains Qixuan's five-phone rr prefix."""
    logical_result = {
        "phonemes": ["@qixuan_es_rr_roll", "ja/o", "ja/s", "ja/a"],
        "phoneme_ids": [-1, 3, 4, 2],
        "language_ids": [0, 2, 2, 2],
        "word_boundaries": [4],
    }
    notes = [
        {
            "is_rest": False,
            "lyric": "ro",
            "syllabic": "begin",
            "offset_beats": 0.0,
            "duration_beats": 1.0,
            "pitch_midi": 60,
        },
        {
            "is_rest": False,
            "lyric": "sa",
            "syllabic": "end",
            "offset_beats": 1.0,
            "duration_beats": 1.0,
            "pitch_midi": 62,
        },
    ]

    with patch(
        "src.api.syllable_alignment.resolve_manifest_pronunciation_adapters",
        return_value=[_ROLLED_R_ADAPTER],
    ), patch("src.api.syllable_alignment.phonemize", return_value=logical_result):
        payload = align(
            notes=notes,
            start_frames=[0, 56],
            end_frames=[56, 112],
            timing_midi=[60.0, 62.0],
            note_durations=[56, 56],
            phonemizer=_StubPhonemizer(),  # type: ignore[arg-type]
            voicebank_path=Path("Qixuan_v2.7.0_DiffSinger_OpenUtau"),
            language="es",
            include_phonemes=True,
        )

    assert payload["phonemes"] == [
        "ja/r", "ja/a", "ja/r", "ja/a", "ja/r", "ja/o", "ja/s", "ja/a",
    ]
    assert payload["word_boundaries"] == [6, 2]
    assert payload["phoneme_timing_rules"] == [
        {
            "start": 0,
            "end": 5,
            "prefix_frames": [3, 3, 3, 3],
            "main_onset_frames": 2,
            "adapter_id": "qixuan_es_jp_rolled_r",
        },
        None,
    ]


def test_qixuan_spanish_roll_reserves_139ms_and_keeps_the_real_vowel() -> None:
    """The selected 139ms ra-ra prefix leaves the anchor tail to the real ``ro`` vowel."""
    durations = _apply_anchor_constrained_timing(
        durations=[8.0, 8.0, 8.0, 8.0, 8.0, 20.0, 6.0, 20.0],
        word_boundaries=[6, 2],
        group_anchor_frames=[
            {"start_frame": 0, "end_frame": 56},
            {"start_frame": 56, "end_frame": 112},
        ],
        vowel_flags=[False, True, False, True, False, True, False, True],
        phoneme_timing_rules=[
            {
                "prefix_frames": [3, 3, 3, 3],
                "main_onset_frames": 2,
            },
            None,
        ],
    )

    assert durations[:6] == [3, 3, 3, 3, 2, 42]
    assert sum(durations[:6]) == 56
    assert sum(durations[6:]) == 56


def test_qixuan_gw_onset_scales_with_note_budget_within_clamp() -> None:
    """The Spanish /g w/ approximation remains audible at multiple tempos."""
    rule = {
        "adaptive_onset_prefix_count": 2,
        "adaptive_onset_frame_ratio": 0.06,
        "adaptive_onset_min_frames": 2,
        "adaptive_onset_max_frames": 5,
    }

    def apply(anchor_frames: int) -> list[int]:
        return _apply_anchor_constrained_timing(
            durations=[1.3, 0.8, 25.6, 16.5],
            word_boundaries=[4],
            group_anchor_frames=[
                {"start_frame": 0, "end_frame": anchor_frames, "note_index": 0}
            ],
            vowel_flags=[False, False, True, False],
            phoneme_timing_rules=[rule],
        )

    fast = apply(25)
    normal = apply(50)
    slow = apply(66)
    very_slow = apply(120)

    assert fast[:2] == [2, 2]
    assert normal[:2] == [3, 3]
    assert slow[:2] == [4, 4]
    assert very_slow[:2] == [5, 5]
    assert all(sum(values) == frames for values, frames in (
        (fast, 25),
        (normal, 50),
        (slow, 66),
        (very_slow, 120),
    ))


def test_qixuan_spanish_syllable_anchors_match_duration_model_context(
    monkeypatch,
) -> None:
    """The narrow glide fix must leave duration and anchor divisions consistent."""
    monkeypatch.setenv("SYLLABLE_ALIGNER_V2", "1")
    score = parse_score(_GUANTANAMERA_SCORE)
    excerpt = deepcopy(score)
    for part in excerpt["parts"]:
        part["notes"] = [
            note
            for note in part.get("notes", [])
            if int(note.get("measure_number") or 0) <= 8
        ]

    payload = align_phonemes_to_notes(
        excerpt,
        _QIXUAN_ROOT,
        language="es",
        include_phonemes=True,
    )

    # Rendered anchors own the full /g w a n/ onset, while the duration model
    # retains its legacy prefix/carry context over the identical phone stream.
    assert payload["word_boundaries"][:5] == [4, 2, 2, 2, 2]
    assert payload["duration_model_word_boundaries"][:6] == [2, 3, 2, 2, 2, 1]
    assert len(payload["duration_model_word_durations"]) == len(
        payload["duration_model_word_boundaries"]
    )
    assert sum(payload["word_boundaries"]) == len(payload["phonemes"])
