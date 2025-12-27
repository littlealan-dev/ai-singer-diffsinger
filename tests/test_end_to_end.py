"""
End-to-end test using the new API module.

Tests the full synthesis pipeline from MusicXML to WAV.
"""

import unittest
from pathlib import Path

from src.api import parse_score, synthesize, save_audio, get_voicebank_info


class TestEndToEndAPI(unittest.TestCase):
    """End-to-end synthesis test using the new API."""
    
    def setUp(self):
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.output_wav = self.output_dir / "api_output.wav"
        
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Test score not found at {self.score_path}")

    def test_voicebank_info(self):
        """Test that voicebank info can be retrieved."""
        info = get_voicebank_info(self.voicebank_path)
        
        print(f"Voicebank: {info['name']}")
        print(f"  Has pitch model: {info['has_pitch_model']}")
        print(f"  Has variance model: {info['has_variance_model']}")
        print(f"  Sample rate: {info['sample_rate']}")
        
        self.assertIn("name", info)
        self.assertTrue(info["has_duration_model"])

    def test_parse_score(self):
        """Test that score can be parsed."""
        score = parse_score(self.score_path)
        
        print(f"Score title: {score['title']}")
        print(f"  Parts: {len(score['parts'])}")
        print(f"  Notes in first part: {len(score['parts'][0]['notes'])}")
        
        self.assertGreater(len(score["parts"]), 0)
        self.assertGreater(len(score["parts"][0]["notes"]), 0)

    def test_full_synthesis(self):
        """Test full pipeline: parse → synthesize → save."""
        print(f"\n=== Full Synthesis Test ===")
        print(f"Voicebank: {self.voicebank_path.name}")
        print(f"Score: {self.score_path.name}")
        
        # Step 1: Parse score
        print("Step 1: Parsing score...")
        score = parse_score(self.score_path)
        print(f"  Parsed {len(score['parts'][0]['notes'])} notes")
        
        # Step 2: Synthesize
        print("Step 2: Synthesizing audio...")
        result = synthesize(score, self.voicebank_path, voice_id="soprano")
        print(f"  Generated {result['duration_seconds']:.2f}s of audio")
        
        # Step 3: Save
        print("Step 3: Saving audio...")
        save_result = save_audio(
            result["waveform"],
            self.output_wav,
            sample_rate=result["sample_rate"],
        )
        print(f"  Saved to: {save_result['path']}")
        
        # Verify
        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)
        print("  ✓ Test passed!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
