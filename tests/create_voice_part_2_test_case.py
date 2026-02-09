import unittest
from pathlib import Path

from src.api import parse_score, save_audio, synthesize


class TestVoicePart2(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.output_dir = self.root_dir / "tests/output"
        self.output_dir.mkdir(exist_ok=True)
        self.output_wav = self.output_dir / "amazing_grace_voice_part_2.wav"
        self.output_wav_3 = self.output_dir / "amazing_grace_voice_part_3.wav"
        self.output_wav_4 = self.output_dir / "amazing_grace_voice_part_4.wav"

    def test_synthesize_voice_part_2(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Score not found at {self.score_path}")

        print(f"Synthesizing voice part 2 from {self.score_path.name}...")
        score = parse_score(self.score_path, part_index=0, verse_number=1)
        print(f"Parsed {len(score['parts'][0]['notes'])} notes in selected part.")

        # For this score, part_index=0 is "SOPRANO ALTO".
        # Voice part 2 corresponds to alto and has no direct lyrics, so we
        # explicitly confirm lyric propagation from soprano.
        result = synthesize(
            score,
            self.voicebank_path,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )

        if result.get("status") == "action_required":
            self.fail(f"Unexpected action_required: {result}")

        print(f"Generated {result['duration_seconds']:.2f}s of audio.")
        save_result = save_audio(
            result["waveform"],
            self.output_wav,
            sample_rate=result["sample_rate"],
        )
        print(f"Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)

    def test_synthesize_voice_part_3(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Score not found at {self.score_path}")

        print(f"Synthesizing voice part 3 from {self.score_path.name}...")
        upper_score = parse_score(self.score_path, part_index=0, verse_number=1)
        lower_score = parse_score(self.score_path, part_index=1, verse_number=1)
        score = dict(upper_score)
        score["parts"] = [upper_score["parts"][0], lower_score["parts"][0]]
        print(
            f"Parsed {len(score['parts'][1]['notes'])} notes in selected part."
        )

        # part_index=1 is "TENOR BASS". This reference score has no direct lyrics
        # there, so we explicitly propagate from soprano in part_index=0.
        result = synthesize(
            score,
            self.voicebank_path,
            part_index=1,
            voice_part_id="tenor",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )

        if result.get("status") == "action_required":
            self.fail(f"Unexpected action_required: {result}")

        print(f"Generated {result['duration_seconds']:.2f}s of audio.")
        save_result = save_audio(
            result["waveform"],
            self.output_wav_3,
            sample_rate=result["sample_rate"],
        )
        print(f"Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)

    def test_synthesize_voice_part_4(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Score not found at {self.score_path}")

        print(f"Synthesizing voice part 4 from {self.score_path.name}...")
        upper_score = parse_score(self.score_path, part_index=0, verse_number=1)
        lower_score = parse_score(self.score_path, part_index=1, verse_number=1)
        score = dict(upper_score)
        score["parts"] = [upper_score["parts"][0], lower_score["parts"][0]]
        print(
            f"Parsed {len(score['parts'][1]['notes'])} notes in selected part."
        )

        result = synthesize(
            score,
            self.voicebank_path,
            part_index=1,
            voice_part_id="bass",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )

        if result.get("status") == "action_required":
            self.fail(f"Unexpected action_required: {result}")

        print(f"Generated {result['duration_seconds']:.2f}s of audio.")
        save_result = save_audio(
            result["waveform"],
            self.output_wav_4,
            sample_rate=result["sample_rate"],
        )
        print(f"Saved to: {save_result['path']}")

        self.assertTrue(Path(save_result["path"]).exists())
        self.assertGreater(Path(save_result["path"]).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
