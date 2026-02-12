
import unittest
from pathlib import Path
from src.api import parse_score, synthesize, save_audio

class TestSopranoVerse1(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace_unzipped.mxl/lg-3126027.xml"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.output_wav = self.output_dir / "soprano_verse_1.wav"

    def test_synthesize_soprano_verse_1(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Score not found at {self.score_path}")

        print(f"Synthesizing Soprano Verse 1 from {self.score_path.name}...")

        # Parse Score - Targeting Part 0 (Soprano) and Verse 1
        # Note: parse_score arguments might need adjustment based on implementation
        # Checking implementation of parse_score...
        # It takes part_index (int) and verse_number (str/int)
        
        # We know Soprano is likely the first part (index 0) from the file inspection (P1)
        score = parse_score(self.score_path, verse_number=1)
        
        print(f"Parsed {len(score['parts'][0]['notes'])} notes for Soprano Verse 1.")

        # Synthesize
        result = synthesize(score, self.voicebank_path)
        print(f"Generated {result['duration_seconds']:.2f}s of audio.")

        # Save
        save_result = save_audio(
            result["waveform"],
            self.output_wav,
            sample_rate=result["sample_rate"]
        )
        print(f"Saved to: {save_result['path']}")
        
        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)

if __name__ == "__main__":
    unittest.main()
