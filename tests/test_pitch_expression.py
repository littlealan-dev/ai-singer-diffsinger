import json
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from src.api import inference
from src.api import voicebank_cache
from src.mcp import handlers
from src.pipeline import Pipeline, PitchContext


class _CapturingPitchModel:
    def __init__(self) -> None:
        self.inputs = None

    def run(self, inputs):
        self.inputs = inputs
        return [np.full_like(inputs["pitch"], 60.0)]


def _write_manifest(path, pitch_expression=1.0, *, include_expression=True):
    entry = {
        "id": "TestBank",
        "enabled": True,
    }
    if include_expression:
        entry["pitch_expression"] = pitch_expression
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "voicebanks": [entry],
            }
        ),
        encoding="utf-8",
    )


def test_manifest_pitch_expression_resolves_from_nested_path(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, 0.5)
    voicebank_path = tmp_path / "TestBank" / "configs"
    voicebank_path.mkdir(parents=True)
    monkeypatch.setenv("VOICEBANK_MANIFEST_PATH", str(manifest_path))

    assert voicebank_cache.resolve_manifest_pitch_expression(voicebank_path) == 0.5


def test_manifest_pitch_expression_defaults_to_one(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, include_expression=False)
    monkeypatch.setenv("VOICEBANK_MANIFEST_PATH", str(manifest_path))

    assert voicebank_cache.resolve_manifest_pitch_expression("TestBank") == 1.0


@pytest.mark.parametrize("value", [-0.1, 1.1, "0.5", True])
def test_manifest_rejects_invalid_pitch_expression(tmp_path, value):
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, value)

    with pytest.raises(ValueError, match="pitch_expression"):
        voicebank_cache._load_voicebank_manifest_for_path(str(manifest_path))


def test_predict_pitch_passes_expression_to_model(tmp_path):
    pitch_model = _CapturingPitchModel()

    with mock.patch.object(inference, "load_voicebank_config", return_value={}), \
        mock.patch.object(inference, "_load_pitch_model", return_value=pitch_model), \
        mock.patch.object(inference, "_load_pitch_linguistic_model", return_value=None), \
        mock.patch.object(inference, "load_speaker_embed", return_value=None):
        inference.predict_pitch(
            phoneme_ids=[1],
            durations=[4],
            word_boundaries=[1],
            word_durations=[4],
            note_pitches=[60.0],
            note_durations=[4],
            note_rests=[False],
            voicebank=tmp_path,
            encoder_out=np.zeros((1, 1, 4), dtype=np.float32),
            expression=0.5,
        )

    np.testing.assert_array_equal(
        pitch_model.inputs["expr"],
        np.full((1, 4), 0.5, dtype=np.float32),
    )


def test_predict_pitch_defaults_to_full_expression(tmp_path):
    pitch_model = _CapturingPitchModel()

    with mock.patch.object(inference, "load_voicebank_config", return_value={}), \
        mock.patch.object(inference, "_load_pitch_model", return_value=pitch_model), \
        mock.patch.object(inference, "_load_pitch_linguistic_model", return_value=None), \
        mock.patch.object(inference, "load_speaker_embed", return_value=None):
        inference.predict_pitch(
            phoneme_ids=[1],
            durations=[2],
            word_boundaries=[1],
            word_durations=[2],
            note_pitches=[60.0],
            note_durations=[2],
            note_rests=[False],
            voicebank=tmp_path,
            encoder_out=np.zeros((1, 1, 4), dtype=np.float32),
        )

    np.testing.assert_array_equal(
        pitch_model.inputs["expr"],
        np.ones((1, 2), dtype=np.float32),
    )


def test_legacy_pipeline_passes_configured_expression_to_model():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.pitch_expression = 0.5
    pipeline.spk_embed = np.zeros((4,), dtype=np.float32)
    pipeline.pitch = _CapturingPitchModel()
    pipeline.pitch_linguistic = None
    pipeline.config = SimpleNamespace(use_lang_id=False, steps=10)
    pitch_context = PitchContext(
        pitch_tokens=np.array([[1]], dtype=np.int64),
        pitch_languages=np.array([[0]], dtype=np.int64),
        ph_durations=np.array([2], dtype=np.int64),
        note_dur=[2],
        note_midi=np.array([60.0], dtype=np.float32),
        note_rest=np.array([False]),
        base_midi=np.array([60.0, 60.0], dtype=np.float32),
        n_frames=2,
    )

    pipeline._predict_pitch(
        pitch_context,
        encoder_out=np.zeros((1, 1, 4), dtype=np.float32),
        ph_midi_list=[60],
    )

    np.testing.assert_array_equal(
        pipeline.pitch.inputs["expr"],
        np.full((1, 2), 0.5, dtype=np.float32),
    )


def test_mcp_handler_passes_manifest_expression_to_synthesis(tmp_path):
    with mock.patch.object(
        handlers,
        "get_manifest_voicebank_metadata",
        return_value={"pitch_expression": 0.5},
    ), mock.patch.object(
        handlers,
        "resolve_voicebank_id",
        return_value=tmp_path,
    ), mock.patch.object(
        handlers,
        "synthesize",
        return_value={"waveform": [0.0], "sample_rate": 44100},
    ) as synthesize_mock:
        handlers.handle_synthesize(
            {"score": {"parts": []}, "voicebank": "TestBank"},
            device="cpu",
        )

    assert synthesize_mock.call_args.kwargs["pitch_expression"] == 0.5
