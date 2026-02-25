from __future__ import annotations

from src.backend.llm_prompt import build_system_prompt


def test_build_system_prompt_includes_voice_part_signals() -> None:
    voice_part_signals = {
        "analysis_version": "v2",
        "parts": [
            {
                "part_index": 0,
                "part_id": "Women",
                "voice_part_measure_spans": {
                    "voice part 1": [{"start": 7, "end": 11}, {"start": 13, "end": 18}],
                    "voice part 2": [{"start": 7, "end": 18}],
                },
                "voice_part_id_to_source_voice_id": {
                    "voice part 1": "1",
                    "voice part 2": "2",
                },
            }
        ],
    }
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        voice_part_signals=voice_part_signals,
        voicebank_details=None,
    )
    assert "Voice-part planning signals (if available):" in prompt
    assert '"score_summary": {' not in prompt
    assert '"voice part 1"' in prompt
    assert '"voice part 2"' in prompt


def test_build_system_prompt_includes_preprocess_mapping_context() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        voice_part_signals={"parts": []},
        preprocess_mapping_context={
            "original_parse": {"score_summary": {"title": "My Tribute"}},
            "derived_mapping": {
                "targets": [
                    {
                        "derived_part_index": 4,
                        "derived_part_id": "P_DERIVED_ABC",
                        "target_voice_part_id": "voice part 1",
                    }
                ]
            },
        },
        voicebank_details=None,
    )
    assert "Preprocess-derived mapping context (if available):" in prompt
    assert '"derived_part_id": "P_DERIVED_ABC"' in prompt
