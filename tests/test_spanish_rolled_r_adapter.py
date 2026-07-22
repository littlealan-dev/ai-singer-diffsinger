from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from src.api import parse_score
from src.api.syllable_alignment import _syllable_start_indices, align
from src.api.synthesize import _apply_anchor_constrained_timing, align_phonemes_to_notes


_ROOT = Path(__file__).parent.parent
_QIXUAN_ROOT = _ROOT / "assets/voicebanks/Qixuan_v2.7.0_DiffSinger_OpenUtau"
_GUANTANAMERA_SCORE = _ROOT / "assets/test_data/guantanamera-lead-sheet-with-lyrics.mxl"


_ROLLED_R_ADAPTER = {
    "id": "qixuan_es_jp_rolled_r",
    "logical_symbol": "@qixuan_es_rr_roll",
    "collapse_phonemes": ["ja/r", "ja/a", "ja/r", "ja/a", "ja/r"],
    "expand_phonemes": ["ja/r", "ja/a", "ja/r"],
    "prefix_frames": [1, 1],
    "main_onset_frames": 1,
}


class _StubPhonemizer:
    _phoneme_to_id = {
        "SP": 0,
        "ja/r": 1,
        "ja/a": 2,
        "ja/o": 3,
        "ja/s": 4,
        "ja/t": 5,
        "ja/y": 6,
        "ja/e": 7,
    }
    _language_map = {"ja": 2}

    def is_vowel(self, phoneme: str) -> bool:
        return phoneme in {"ja/a", "ja/e", "ja/o"}

    def is_glide(self, phoneme: str) -> bool:
        return phoneme == "ja/y"


def test_qixuan_spanish_roll_expands_after_logical_alignment() -> None:
    """Only the aligned runtime payload contains Qixuan's compact rr substitute."""
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
        "ja/r", "ja/a", "ja/r", "ja/o", "ja/s", "ja/a",
    ]
    assert payload["word_boundaries"] == [4, 2]
    assert payload["phoneme_timing_rules"] == [
        {
            "start": 0,
            "end": 3,
            "prefix_frames": [1, 1],
            "main_onset_frames": 1,
            "adapter_id": "qixuan_es_jp_rolled_r",
            "adaptive_onset_prefix_count": 3,
                "adaptive_onset_frame_ratio": 0.10,
                "adaptive_onset_min_frames": 1,
                "adaptive_onset_max_frames": 4,
        },
        {"min_vowel_frame_ratio": 0.5},
    ]


def test_rolled_r_rule_stays_on_rra_after_a_declared_glide_onset() -> None:
    """A /t j/ prepend must not shift an internal rolled-R span left."""
    logical_result = {
        "phonemes": [
            "ja/t", "ja/y", "ja/e", "@qixuan_es_rr_roll", "ja/a",
        ],
        "phoneme_ids": [5, 6, 7, -1, 2],
        "language_ids": [2, 2, 2, 0, 2],
        "word_boundaries": [5],
    }
    onset_adapters = [
        {
            "id": "qixuan_es_ty_onset",
            "prefix_phonemes": ["ja/t", "ja/y"],
            "preserve_following_syllable_onsets": True,
            "onset_timing": {
                "frame_ratio": 0.06,
                "min_frames": 2,
                "max_frames": 5,
            },
        },
        {
            "id": "qixuan_es_rolled_r_onset",
            "logical_adapter_id": "qixuan_es_jp_rolled_r",
            "prefix_phonemes": ["ja/r", "ja/a", "ja/r"],
            "preserve_following_syllable_onsets": True,
            "onset_timing": {
                "frame_ratio": 0.02,
                "min_frames": 1,
                "max_frames": 2,
            },
        },
    ]
    notes = [
        {
            "is_rest": False,
            "lyric": "tie",
            "syllabic": "begin",
            "offset_beats": 0.0,
            "duration_beats": 1.0,
            "pitch_midi": 60,
        },
        {
            "is_rest": False,
            "lyric": "rra",
            "syllabic": "end",
            "offset_beats": 1.0,
            "duration_beats": 1.0,
            "pitch_midi": 62,
        },
    ]

    with patch(
        "src.api.syllable_alignment.resolve_manifest_pronunciation_adapters",
        return_value=[_ROLLED_R_ADAPTER],
    ), patch(
        "src.api.syllable_alignment.resolve_manifest_onset_anchor_adapters",
        return_value=onset_adapters,
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

    assert payload["word_boundaries"] == [3, 4]
    assert payload["phonemes"] == [
        "ja/t", "ja/y", "ja/e",
        "ja/r", "ja/a", "ja/r", "ja/a",
    ]
    assert payload["phoneme_timing_rules"][0]["adaptive_onset_prefix_count"] == 2
    rolled_r_rule = payload["phoneme_timing_rules"][1]
    assert rolled_r_rule["adapter_id"] == "qixuan_es_jp_rolled_r"
    assert rolled_r_rule["adaptive_onset_prefix_count"] == 3


def test_rolled_r_virtual_vowels_do_not_consume_later_syllable_anchors() -> None:
    """``ro-ga-mos`` keeps the /ga/ and /mos/ vowel on their scored notes."""
    starts = _syllable_start_indices(
        [
            "ja/r", "ja/a", "ja/r", "ja/o",
            "ja/g", "ja/a", "ja/m", "ja/o", "ja/s",
        ],
        _StubPhonemizer(),  # type: ignore[arg-type]
        logical_spans=[{"start": 0, "end": 3}],
        forced_following_onsets=2,
    )

    assert starts == [0, 4, 6]


def test_rolled_r_keeps_a_short_final_vowel_on_its_scored_note() -> None:
    """Only the first following onset is forced for ``ro-ga-mos``.

    The logical span itself retains the ``ga`` onset.  Limiting the extra
    forced onset leaves the /m/ transition on ``ga`` and /o s/ on short
    ``mos``, rather than squeezing /m o s/ into the final sixteenth-note.
    """
    starts = _syllable_start_indices(
        ["ja/r", "ja/a", "ja/r", "ja/o", "ja/g", "ja/a", "ja/m", "ja/o", "ja/s"],
        _StubPhonemizer(),  # type: ignore[arg-type]
        logical_spans=[{"start": 0, "end": 3}],
        forced_following_onsets=1,
    )

    assert starts == [0, 4, 7]


def test_short_following_syllable_reserves_frames_for_its_vowel() -> None:
    """A short /o s k/ anchor cannot let its consonants consume the vowel."""
    durations = _apply_anchor_constrained_timing(
        durations=[2.0, 3.0, 6.0],
        word_boundaries=[3],
        group_anchor_frames=[{"start_frame": 0, "end_frame": 11}],
        vowel_flags=[True, False, False],
        phoneme_timing_rules=[{"min_vowel_frame_ratio": 0.5}],
    )

    assert durations == [6, 3, 2]


def test_qixuan_spanish_roll_scales_from_short_to_sustained_notes() -> None:
    """The compact /r a r/ onset is short on fast notes and rolls on sustained ones."""
    rule = {
        "adaptive_onset_prefix_count": 3,
        "adaptive_onset_frame_ratio": 0.10,
        "adaptive_onset_min_frames": 1,
        "adaptive_onset_max_frames": 4,
    }

    def apply(anchor_frames: int) -> list[int]:
        return _apply_anchor_constrained_timing(
            durations=[8.0, 8.0, 8.0, 20.0],
            word_boundaries=[4],
            group_anchor_frames=[{"start_frame": 0, "end_frame": anchor_frames}],
            vowel_flags=[False, True, False, True],
            phoneme_timing_rules=[rule],
        )

    assert apply(11) == [1, 1, 1, 8]
    assert apply(43) == [4, 4, 4, 31]


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


def test_pm31_spanish_roll_uses_a_compact_double_r_onset() -> None:
    """PM-31 uses two native /r/ phones without inserting artificial /a/ vowels."""
    rule = {
        "adaptive_onset_prefix_count": 2,
        "adaptive_onset_frame_ratio": 0.02,
        "adaptive_onset_min_frames": 1,
        "adaptive_onset_max_frames": 2,
    }

    def apply(anchor_frames: int) -> list[int]:
        return _apply_anchor_constrained_timing(
            durations=[5.0, 5.0, 40.0],
            word_boundaries=[3],
            group_anchor_frames=[
                {"start_frame": 0, "end_frame": anchor_frames, "note_index": 0}
            ],
            vowel_flags=[False, False, True],
            phoneme_timing_rules=[rule],
        )

    standard_tempo = apply(56)
    slow_tempo = apply(120)

    # At 120 BPM / quarter note (56 frames), the consonant-only roll uses only
    # 23 ms and leaves the real Spanish vowel as the note's sustained body.
    assert standard_tempo == [1, 1, 54]
    # The clamp scales only on very long anchors and still conserves the note.
    assert slow_tempo == [2, 2, 116]
    assert sum(standard_tempo) == 56
    assert sum(slow_tempo) == 120


def test_qixuan_spanish_roll_keeps_a_coda_inside_the_compact_anchor() -> None:
    """Qixuan's compact /r a r/ substitute leaves the vowel and coda together."""
    rule = {
        "adaptive_onset_prefix_count": 3,
        "adaptive_onset_frame_ratio": 0.02,
        "adaptive_onset_min_frames": 1,
        "adaptive_onset_max_frames": 2,
    }
    durations = _apply_anchor_constrained_timing(
        durations=[5.0, 31.0, 5.0, 30.0, 10.0],
        word_boundaries=[5],
        group_anchor_frames=[{"start_frame": 0, "end_frame": 56, "note_index": 0}],
        vowel_flags=[False, True, False, True, False],
        phoneme_timing_rules=[rule],
    )

    assert durations[:3] == [1, 1, 1]
    assert all(value >= 1 for value in durations[3:])
    assert sum(durations) == 56


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
