from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from src.pipeline import Pipeline


class TestPipelineSteps(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.debug_output_dir = self.output_dir / "output"

        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")

    def test_linguistic_stage(self) -> None:
        pipeline = Pipeline(self.voicebank_path)
        pipeline.infer(
            self.score_path,
            output_path=None,
            debug_dir=self.output_dir,
            stop_after="linguistic",
        )
        tokens = np.load(self.debug_output_dir / "tokens.npy")
        encoder_out = np.load(self.debug_output_dir / "encoder_out.npy")
        x_masks = np.load(self.debug_output_dir / "x_masks.npy")
        self.assertEqual(tokens.shape[0], 1)
        self.assertEqual(encoder_out.shape[0], 1)
        self.assertEqual(tokens.shape[1], encoder_out.shape[1])
        self.assertEqual(x_masks.shape[1], tokens.shape[1])

    def test_duration_stage(self) -> None:
        pipeline = Pipeline(self.voicebank_path)
        pipeline.infer(
            self.score_path,
            output_path=None,
            debug_dir=self.output_dir,
            stop_after="duration",
        )
        ph_dur = np.load(self.debug_output_dir / "ph_durations.npy")
        word_dur = np.load(self.debug_output_dir / "word_dur.npy")[0]
        word_div = np.load(self.debug_output_dir / "word_div.npy")[0]
        self.assertEqual(ph_dur.ndim, 1)
        self.assertTrue((ph_dur > 0).all())
        self.assertEqual(int(ph_dur.sum()), int(word_dur.sum()))
        self.assertEqual(int(word_div.sum()), int(ph_dur.shape[0]))

    def test_acoustic_stage(self) -> None:
        pipeline = Pipeline(self.voicebank_path)
        pipeline.infer(
            self.score_path,
            output_path=None,
            debug_dir=self.output_dir,
            stop_after="acoustic",
        )
        mel = np.load(self.debug_output_dir / "mel.npy")
        f0 = np.load(self.debug_output_dir / "f0.npy")
        self.assertEqual(mel.shape[0], 1)
        self.assertEqual(f0.shape[0], 1)
        self.assertEqual(mel.shape[1], f0.shape[1])


if __name__ == "__main__":
    unittest.main()
