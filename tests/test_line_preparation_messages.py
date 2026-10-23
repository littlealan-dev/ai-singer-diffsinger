from __future__ import annotations

from types import SimpleNamespace

from src.backend.orchestrator import Orchestrator, _line_preparation_message_score_summary


def _summary() -> dict:
    return {
        "title": "Song",
        "selected_verse_number": "1",
        "available_verses": ["1", "2"],
        "duration_seconds": 61.0,
        "instrument_program_resolution": {"unresolved_score_instrument_ids": []},
        "parts": [
            {
                "part_id": "P1",
                "part_name": "Choir",
                "is_derived_part": False,
                "has_lyrics": True,
                "lyric_selections": [{"id": "lyr_1", "number": "1", "name": ""}],
                "instruments": [{"instrument_name": "Voice"}],
                "pitch_range": {"lowest_note": "C4"},
            }
        ],
    }


def test_message_score_summary_keeps_title_verses_and_part_list_only() -> None:
    assert _line_preparation_message_score_summary(_summary()) == {
        "title": "Song",
        "selected_verse_number": "1",
        "available_verses": ["1", "2"],
        "parts": [
            {"part_id": "P1", "part_name": "Choir", "is_derived_part": False, "has_lyrics": True}
        ],
    }


def test_line_preparation_message_prompt_carries_parts_and_mapping_only() -> None:
    mapping = {"derived_mapping": {"targets": [{"derived_part_id": "P_DERIVED_A"}]}}
    stub = SimpleNamespace(
        _settings=SimpleNamespace(synthesis_max_duration_seconds=300.0),
        _build_preprocess_mapping_context=lambda score, *, score_summary: mapping,
    )
    snapshot = {"score_summary": _summary(), "solfege_settings": {"system": "fixed_do"}}

    bundle = Orchestrator._build_line_preparation_message_prompt(
        stub, snapshot, {"parts": [], "voice_part_signals": {"parts": [{"part_id": "P1"}]}}
    )

    dynamic = bundle.dynamic_prompt_text
    assert '"part_name": "Choir"' in dynamic
    assert "P_DERIVED_A" in dynamic
    for left_out in ("lyric_selections", "pitch_range", "instrument_program_resolution", "fixed_do"):
        assert left_out not in dynamic
    assert "Voice-part planning signals (if available):\nnone" in dynamic
    assert "Available voicebanks (IDs): none" in dynamic
