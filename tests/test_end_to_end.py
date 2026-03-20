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
        self.voicebank_reizo_path = self.root_dir / "assets/voicebanks/Raine_Reizo_2.01"
        self.voicebank_apollo_path = self.root_dir / "assets/voicebanks/Apollo DS 1.0"
        self.voicebank_katyusha_path = self.root_dir / "assets/voicebanks/Katyusha_v170/configs"
        self.voicebank_commissions_path = self.root_dir / "assets/voicebanks/commissionsv1noka/configs"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace.mxl"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.output_wav = self.output_dir / "api_output.wav"
        self.output_wav_reizo = self.output_dir / "api_output_reizo.wav"
        self.output_wav_apollo = self.output_dir / "api_output_apollo.wav"
        self.output_wav_apollo_tenor = self.output_dir / "api_output_apollo_tenor.wav"
        self.output_wav_katyusha = self.output_dir / "api_output_katyusha.wav"
        self.output_wav_commissions = self.output_dir / "api_output_commissionsv1noka.wav"
        
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Test score not found at {self.score_path}")

    def _run_full_synthesis_case(
        self,
        voicebank_path: Path,
        output_wav: Path,
        *,
        label: str,
        synth_kwargs: dict | None = None,
    ) -> None:
        if not voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {voicebank_path}")

        print(f"\n=== Full Synthesis Test ({label}) ===")
        print(f"Voicebank: {voicebank_path}")
        print(f"Score: {self.score_path.name}")

        print("Step 1: Parsing score...")
        score = parse_score(self.score_path)
        print(f"  Parsed {len(score['parts'][0]['notes'])} notes")

        print("Step 2: Synthesizing audio...")
        result = synthesize(score, voicebank_path, **(synth_kwargs or {}))
        print(f"  Generated {result['duration_seconds']:.2f}s of audio")

        print("Step 3: Saving audio...")
        save_result = save_audio(
            result["waveform"],
            output_wav,
            sample_rate=result["sample_rate"],
        )
        print(f"  Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)
        print("  ✓ Test passed!")

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
        self._run_full_synthesis_case(
            self.voicebank_path,
            self.output_wav,
            label="Raine_Rena_2.01",
        )

    def test_full_synthesis_reizo(self):
        """Test full pipeline with Raine_Reizo_2.01."""
        self._run_full_synthesis_case(
            self.voicebank_reizo_path,
            self.output_wav_reizo,
            label="Raine_Reizo_2.01",
            synth_kwargs={"voice_id": "soprano"},
        )

    def test_full_synthesis_apollo(self):
        """Test full pipeline with Apollo DS 1.0."""
        self._run_full_synthesis_case(
            self.voicebank_apollo_path,
            self.output_wav_apollo,
            label="Apollo DS 1.0",
        )

    def test_full_synthesis_katyusha(self):
        """Test full pipeline with Katyusha_v170/configs."""
        self._run_full_synthesis_case(
            self.voicebank_katyusha_path,
            self.output_wav_katyusha,
            label="Katyusha_v170/configs",
        )

    def test_full_synthesis_commissionsv1noka(self):
        """Test full pipeline with commissionsv1noka/configs."""
        self._run_full_synthesis_case(
            self.voicebank_commissions_path,
            self.output_wav_commissions,
            label="commissionsv1noka/configs",
        )

    def test_full_synthesis_apollo_tenor_amazing_grace(self):
        """Test tenor verse 1 of Amazing Grace with Apollo DS 1.0."""
        if not self.voicebank_apollo_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_apollo_path}")

        print(f"\n=== Full Synthesis Test (Tenor, Apollo) ===")
        print(f"Voicebank: {self.voicebank_apollo_path.name}")
        print(f"Score: {self.score_path.name}")

        print("Step 1: Parsing score summary...")
        summary_score = parse_score(self.score_path)
        parts = summary_score.get("score_summary", {}).get("parts", [])
        tenor_part = next(
            (part for part in parts if "tenor" in (part.get("part_name") or "").lower()),
            None,
        )
        if tenor_part is None:
            self.skipTest("Tenor part not found in score summary.")

        tenor_index = tenor_part["part_index"]

        print("Step 2: Parsing tenor verse 1...")
        score = parse_score(self.score_path, verse_number="1")
        print(f"  Parsed {len(score['parts'][tenor_index]['notes'])} notes")

        print("Step 3: Synthesizing audio...")
        result = synthesize(score, self.voicebank_apollo_path, part_index=tenor_index)
        print(f"  Generated {result['duration_seconds']:.2f}s of audio")

        print("Step 4: Saving audio...")
        save_result = save_audio(
            result["waveform"],
            self.output_wav_apollo_tenor,
            sample_rate=result["sample_rate"],
        )
        print(f"  Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)
        print("  ✓ Test passed!")

    def test_full_synthesis_tenor_male_amazing_grace(self):
        """Test tenor verse 1 with a male voicebank."""
        if not self.voicebank_reizo_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_reizo_path}")

        print(f"\n=== Full Synthesis Test (Tenor, Male) ===")
        print(f"Voicebank: {self.voicebank_reizo_path.name}")
        print(f"Score: {self.score_path.name}")

        print("Step 1: Parsing score summary...")
        summary_score = parse_score(self.score_path)
        parts = summary_score.get("score_summary", {}).get("parts", [])
        tenor_part = next(
            (part for part in parts if (part.get("part_name") or "").lower().find("tenor") != -1),
            None,
        )
        if tenor_part is None:
            self.skipTest("Tenor part not found in score summary.")

        tenor_index = tenor_part["part_index"]

        print("Step 2: Parsing tenor verse 1...")
        score = parse_score(self.score_path, verse_number="1")
        print(f"  Parsed {len(score['parts'][tenor_index]['notes'])} notes")

        print("Step 3: Synthesizing audio...")
        result = synthesize(score, self.voicebank_reizo_path, part_index=tenor_index)
        print(f"  Generated {result['duration_seconds']:.2f}s of audio")

        print("Step 4: Saving audio...")
        output_wav = self.output_dir / "api_output_tenor_male.wav"
        save_result = save_audio(
            result["waveform"],
            output_wav,
            sample_rate=result["sample_rate"],
        )
        print(f"  Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)
        print("  ✓ Test passed!")

    def test_full_synthesis_christmas_song(self):
        """Test full synthesis with the Christmas Song score."""
        christmas_score = self.root_dir / "assets/test_data/the-christmas-song.xml"
        if not christmas_score.exists():
            self.skipTest(f"Test score not found at {christmas_score}")
        output_wav = self.output_dir / "api_output_christmas.wav"

        print(f"\n=== Full Synthesis Test (Christmas Song) ===")
        print(f"Voicebank: {self.voicebank_path.name}")
        print(f"Score: {christmas_score.name}")

        print("Step 1: Parsing score...")
        score = parse_score(christmas_score)
        print(f"  Parsed {len(score['parts'][0]['notes'])} notes")

        print("Step 2: Synthesizing audio...")
        result = synthesize(score, self.voicebank_path)
        print(f"  Generated {result['duration_seconds']:.2f}s of audio")

        print("Step 3: Saving audio...")
        save_result = save_audio(
            result["waveform"],
            output_wav,
            sample_rate=result["sample_rate"],
        )
        print(f"  Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)
        print("  ✓ Test passed!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
