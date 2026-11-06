from types import SimpleNamespace
from importlib import import_module

import pytest

synthesis = import_module("src.api.synthesize")


@pytest.mark.parametrize("stop_stage", ["align", "durations", "pitch", "variance", "mel"])
def test_stop_between_synthesis_stages_skips_later_stage(monkeypatch, stop_stage):
    calls = []
    monkeypatch.setattr(synthesis, "_estimated_score_duration_seconds", lambda score: 1.0)
    monkeypatch.setattr(synthesis, "_synthesis_max_duration_seconds", lambda: 100.0)
    monkeypatch.setattr(synthesis, "synthesize_preflight_action_required", lambda *a, **k: None)
    monkeypatch.setattr(synthesis, "load_voicebank_config", lambda path: {"sample_rate": 44100, "hop_size": 512})
    monkeypatch.setattr(synthesis, "resolve_manifest_synthesis_control_defaults", lambda path: {
        "airiness": 0.0, "intensity": 0.5, "clarity": 100.0, "gender": 0.0,
    })
    monkeypatch.setattr(synthesis, "resolve_default_voice_color", lambda path: None)
    monkeypatch.setattr(synthesis, "resolve_voice_color_speaker", lambda path, color: None)
    monkeypatch.setattr(synthesis, "build_stage_token_bundle", lambda path, phonemes: SimpleNamespace(
        dur_ids=[1], pitch_ids=[1], variance_ids=[1], root_ids=[1],
    ))

    def align(*args, **kwargs):
        calls.append("align")
        return {
            "phonemes": ["la"], "phoneme_ids": [1], "word_boundaries": [0, 1],
            "word_durations": [1.0], "word_pitches": [60], "language_ids": [0],
            "note_durations": [1], "note_pitches": [60], "note_rests": [False],
        }

    monkeypatch.setattr(synthesis, "align_phonemes_to_notes", align)
    monkeypatch.setattr(synthesis, "predict_durations", lambda **k: calls.append("durations") or {
        "durations": [1], "encoder_out": None,
    })
    monkeypatch.setattr(synthesis, "predict_pitch", lambda **k: calls.append("pitch") or {
        "f0": [440.0],
    })
    monkeypatch.setattr(synthesis, "predict_variance", lambda **k: calls.append("variance") or {
        "energy": [1.0], "breathiness": [0.0], "tension": [0.0], "voicing": [1.0],
    })
    monkeypatch.setattr(synthesis, "synthesize_audio", lambda **k: calls.append("mel") or {
        "waveform": [0.0], "sample_rate": 44100,
    })

    result = synthesis.synthesize(
        {"parts": []}, "unused", pitch_expression=0.5,
        stop_check=lambda stage: stage == stop_stage,
    )
    assert result == {"status": "stopped", "stage": stop_stage}
    assert stop_stage not in calls
