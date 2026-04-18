import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from src.api import inference


class _FakeVarianceModel:
    def __init__(self, output_names, outputs):
        self.output_names = list(output_names)
        self.outputs = list(outputs)
        self.last_inputs = None

    def run(self, inputs):
        self.last_inputs = inputs
        return self.outputs


class _FakeAcousticModel:
    def __init__(self):
        self.last_inputs = None

    def run(self, inputs):
        self.last_inputs = inputs
        return [np.zeros((1, 2, 3), dtype=np.float32)]


class TestEnergyVarianceParity(unittest.TestCase):
    def test_predict_variance_extracts_energy_by_output_name(self):
        variance_model = _FakeVarianceModel(
            ["breathiness_pred", "energy_pred"],
            [
                np.array([[0.2, 0.3]], dtype=np.float32),
                np.array([[0.7, 0.9]], dtype=np.float32),
            ],
        )
        with mock.patch.object(inference, "load_voicebank_config", return_value={"steps": 10}), \
            mock.patch.object(inference, "_load_variance_config", return_value={
                "predict_energy": True,
                "predict_breathiness": True,
                "predict_voicing": False,
                "predict_tension": False,
            }), \
            mock.patch.object(inference, "_load_variance_model", return_value=variance_model), \
            mock.patch.object(inference, "_load_variance_linguistic_model", return_value=None), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            result = inference.predict_variance(
                phoneme_ids=[1, 2],
                durations=[1, 1],
                word_boundaries=[2],
                word_durations=[2],
                f0=[220.0, 220.0],
                voicebank=Path("/tmp/fake-bank"),
                encoder_out=np.zeros((1, 2, 4), dtype=np.float32),
            )

        np.testing.assert_allclose(result["energy"], [0.7, 0.9])
        np.testing.assert_allclose(result["breathiness"], [0.2, 0.3])
        self.assertEqual(result["tension"], [0.0, 0.0])
        self.assertEqual(result["voicing"], [0.0, 0.0])

    def test_predict_variance_raises_when_energy_pred_missing(self):
        variance_model = _FakeVarianceModel(
            ["breathiness_pred"],
            [np.array([[0.2, 0.3]], dtype=np.float32)],
        )
        with mock.patch.object(inference, "load_voicebank_config", return_value={"steps": 10}), \
            mock.patch.object(inference, "_load_variance_config", return_value={
                "predict_energy": True,
                "predict_breathiness": True,
            }), \
            mock.patch.object(inference, "_load_variance_model", return_value=variance_model), \
            mock.patch.object(inference, "_load_variance_linguistic_model", return_value=None), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            with self.assertRaisesRegex(ValueError, "energy_pred"):
                inference.predict_variance(
                    phoneme_ids=[1, 2],
                    durations=[1, 1],
                    word_boundaries=[2],
                    word_durations=[2],
                    f0=[220.0, 220.0],
                    voicebank=Path("/tmp/fake-bank"),
                    encoder_out=np.zeros((1, 2, 4), dtype=np.float32),
                )

    def test_predict_variance_omits_speaker_embed_when_voicebank_has_none(self):
        variance_model = _FakeVarianceModel(
            ["breathiness_pred"],
            [np.array([[0.2, 0.3]], dtype=np.float32)],
        )
        with mock.patch.object(inference, "load_voicebank_config", return_value={"steps": 10}), \
            mock.patch.object(inference, "_load_variance_config", return_value={
                "predict_energy": False,
                "predict_breathiness": True,
                "predict_voicing": False,
                "predict_tension": False,
            }), \
            mock.patch.object(inference, "_load_variance_model", return_value=variance_model), \
            mock.patch.object(inference, "_load_variance_linguistic_model", return_value=None), \
            mock.patch.object(inference, "load_speaker_embed", return_value=None):
            result = inference.predict_variance(
                phoneme_ids=[1, 2],
                durations=[1, 1],
                word_boundaries=[2],
                word_durations=[2],
                f0=[220.0, 220.0],
                voicebank=Path("/tmp/fake-bank"),
                encoder_out=np.zeros((1, 2, 4), dtype=np.float32),
            )

        self.assertNotIn("spk_embed", variance_model.last_inputs)
        np.testing.assert_allclose(result["breathiness"], [0.2, 0.3])

    def test_synthesize_mel_omits_energy_when_not_required(self):
        acoustic = _FakeAcousticModel()
        with mock.patch.object(inference, "load_voicebank_config", return_value={
            "sample_rate": 44100,
            "hop_size": 512,
            "use_energy_embed": False,
            "use_lang_id": False,
            "steps": 10,
        }), \
            mock.patch.object(inference, "_load_variance_config", return_value={}), \
            mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            inference.synthesize_mel(
                phoneme_ids=[1, 2],
                durations=[1, 1],
                f0=[220.0, 220.0],
                voicebank=Path("/tmp/fake-bank"),
                energy=[0.1, 0.2],
            )
        self.assertNotIn("energy", acoustic.last_inputs)

    def test_synthesize_mel_includes_energy_when_required(self):
        acoustic = _FakeAcousticModel()
        with mock.patch.object(inference, "load_voicebank_config", return_value={
            "sample_rate": 44100,
            "hop_size": 512,
            "use_energy_embed": True,
            "use_lang_id": False,
            "steps": 10,
        }), \
            mock.patch.object(inference, "_load_variance_config", return_value={"predict_energy": True}), \
            mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            inference.synthesize_mel(
                phoneme_ids=[1, 2],
                durations=[1, 1],
                f0=[220.0, 220.0],
                voicebank=Path("/tmp/fake-bank"),
                energy=[0.1, 0.2],
            )
        self.assertIn("energy", acoustic.last_inputs)
        np.testing.assert_allclose(acoustic.last_inputs["energy"], [[0.1, 0.2]])

    def test_synthesize_mel_raises_when_required_energy_missing(self):
        acoustic = _FakeAcousticModel()
        with mock.patch.object(inference, "load_voicebank_config", return_value={
            "sample_rate": 44100,
            "hop_size": 512,
            "use_energy_embed": True,
            "use_lang_id": False,
            "steps": 10,
        }), \
            mock.patch.object(inference, "_load_variance_config", return_value={"predict_energy": True}), \
            mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            with self.assertRaisesRegex(ValueError, "requires 'energy'"):
                inference.synthesize_mel(
                    phoneme_ids=[1, 2],
                    durations=[1, 1],
                    f0=[220.0, 220.0],
                    voicebank=Path("/tmp/fake-bank"),
                )

    def test_synthesize_mel_raises_on_root_variance_energy_mismatch(self):
        acoustic = _FakeAcousticModel()
        with mock.patch.object(inference, "load_voicebank_config", return_value={
            "sample_rate": 44100,
            "hop_size": 512,
            "use_energy_embed": True,
            "use_lang_id": False,
            "steps": 10,
        }), \
            mock.patch.object(inference, "_load_variance_config", return_value={"predict_energy": False}), \
            mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
            mock.patch.object(inference, "load_speaker_embed", return_value=np.zeros((4,), dtype=np.float32)):
            with self.assertRaisesRegex(ValueError, "does not enable predict_energy"):
                inference.synthesize_mel(
                    phoneme_ids=[1, 2],
                    durations=[1, 1],
                    f0=[220.0, 220.0],
                    voicebank=Path("/tmp/fake-bank"),
                    energy=[0.1, 0.2],
                )

    def test_synthesize_mel_omits_speaker_embed_when_voicebank_has_none(self):
        acoustic = _FakeAcousticModel()
        with mock.patch.object(inference, "load_voicebank_config", return_value={
            "sample_rate": 44100,
            "hop_size": 512,
            "use_energy_embed": False,
            "use_lang_id": False,
            "steps": 10,
        }), \
            mock.patch.object(inference, "_load_variance_config", return_value={}), \
            mock.patch.object(inference, "_load_acoustic_model", return_value=acoustic), \
            mock.patch.object(inference, "load_speaker_embed", return_value=None):
            inference.synthesize_mel(
                phoneme_ids=[1, 2],
                durations=[1, 1],
                f0=[220.0, 220.0],
                voicebank=Path("/tmp/fake-bank"),
                energy=[0.1, 0.2],
            )
        self.assertNotIn("spk_embed", acoustic.last_inputs)


if __name__ == "__main__":
    unittest.main()
