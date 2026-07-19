from pathlib import Path
from unittest import mock

import numpy as np

from src.api import inference
from src.api.synthesize import _apply_openutau_breathiness, _apply_openutau_voicing


class _CapturingAcousticModel:
    def __init__(self) -> None:
        self.inputs = None

    def run(self, inputs):
        self.inputs = inputs
        return [np.zeros((1, 2, 3), dtype=np.float32)]


def test_openutau_voic_applies_the_native_additive_offset():
    assert _apply_openutau_voicing([-24.0, -30.0], 100.0) == [-24.0, -30.0]
    assert _apply_openutau_voicing([-24.0, -30.0], 70.0) == [-27.6, -33.6]
    assert _apply_openutau_voicing([-24.0, -30.0], 0.0) == [-36.0, -42.0]
    assert _apply_openutau_voicing([-24.0, -30.0], 170.0) == [-15.6, -21.6]


def test_openutau_brec_applies_the_native_additive_offset():
    assert _apply_openutau_breathiness([-48.0, -54.0], 0.0) == [-48.0, -54.0]
    assert _apply_openutau_breathiness([-48.0, -54.0], 35.0) == [-43.8, -49.8]
    assert _apply_openutau_breathiness([-48.0, -54.0], -100.0) == [-60.0, -66.0]


def test_openutau_genc_uses_the_voicebank_training_range():
    config = {
        "use_key_shift_embed": True,
        "augmentation_args": {"random_pitch_shifting": {"range": [-5.0, 5.0]}},
    }

    assert inference._openutau_gender_to_model_value(100.0, config) == -2.4
    assert inference._openutau_gender_to_model_value(-100.0, config) == 2.4
    assert inference._openutau_gender_to_model_value(0.0, config) == 0.0


def test_gender_control_is_converted_before_the_acoustic_model():
    acoustic = _CapturingAcousticModel()
    config = {
        "sample_rate": 44100,
        "hop_size": 512,
        "use_energy_embed": False,
        "use_lang_id": False,
        "use_key_shift_embed": True,
        "augmentation_args": {"random_pitch_shifting": {"range": [-5.0, 5.0]}},
        "steps": 10,
    }
    with mock.patch.object(inference, "load_voicebank_config", return_value=config), \
        mock.patch.object(inference, "_load_variance_config", return_value={}), \
        mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
        mock.patch.object(inference, "load_speaker_embed", return_value=None):
        inference.synthesize_mel(
            phoneme_ids=[1, 2],
            durations=[1, 1],
            f0=[220.0, 220.0],
            voicebank=Path("/tmp/fake-bank"),
            gender=-20.833333333333332,
        )

    np.testing.assert_allclose(acoustic.inputs["gender"], [[0.5, 0.5]])
