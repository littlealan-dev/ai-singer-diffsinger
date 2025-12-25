import unittest
from pathlib import Path
import os
import shutil
from src.pipeline import Pipeline

class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.output_wav = self.output_dir / "output.wav"
        
        # Verify voicebank exists (it should in this environment)
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")

    def test_pipeline_inference(self):
        print(f"Testing Pipeline with Voicebank: {self.voicebank_path}")
        pipeline = Pipeline(self.voicebank_path)
        
        # Verify components loaded
        self.assertIsNotNone(pipeline.phonemizer)
        self.assertIsNotNone(pipeline.linguistic)
        self.assertIsNotNone(pipeline.duration)
        self.assertIsNotNone(pipeline.acoustic)
        self.assertIsNotNone(pipeline.vocoder)
        # Pitch might be None if using fallback, but Raine has it
        # Actually in Raine it is in dspitch/
        # My pipeline discovery logic for Pitch checks dspitch
        # So it should be loaded
        self.assertIsNotNone(pipeline.pitch)
        
        print("Running Inference...")
        pipeline.infer(self.score_path, self.output_wav)
        self.assertTrue(self.output_wav.exists())
        self.assertGreater(self.output_wav.stat().st_size, 1000, "Output WAV file is too small")
        print(f"Output generated at {self.output_wav}")

if __name__ == '__main__':
    unittest.main()
