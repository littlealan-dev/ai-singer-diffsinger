import unittest
from pathlib import Path

from src.api.backing_track_prompt import (
    build_elevenlabs_backing_track_prompt,
    extract_backing_track_metadata,
)


ROOT_DIR = Path(__file__).parent.parent
TEST_HAPPY_BIRTHDAY_MXL = ROOT_DIR / "assets/test_data/happy-birthday-to-you-44-key-c.mxl"


class TestBackingTrackPrompt(unittest.TestCase):
    def test_extracts_happy_birthday_backing_track_metadata(self):
        metadata = extract_backing_track_metadata(TEST_HAPPY_BIRTHDAY_MXL)

        self.assertEqual(metadata["title"], "Happy Birthday (4/4 key C)")
        self.assertEqual(metadata["key_signature"], "C major")
        self.assertEqual(metadata["time_signature"], "4/4")
        self.assertEqual(metadata["bpm"], 120)
        self.assertEqual(metadata["measure_count"], 17)
        self.assertAlmostEqual(metadata["duration_seconds"], 34.0)
        self.assertEqual(metadata["duration_mmss"], "0:34")

        pickup = metadata["pickup"]
        self.assertTrue(pickup["has_pickup"])
        self.assertEqual(pickup["starts_on_beat"], 4)
        self.assertEqual(pickup["second_bar_downbeat_note"], "A4")

        progression = metadata["chord_progression"]
        self.assertEqual(progression[0], {"measure": 2, "chords": ["C"]})
        self.assertEqual(progression[1], {"measure": 3, "chords": ["G"]})
        self.assertEqual(progression[-2], {"measure": 16, "chords": ["C", "G"]})
        self.assertEqual(progression[-1], {"measure": 17, "chords": ["C"]})

    def test_builds_elevenlabs_backing_track_prompt_for_happy_birthday(self):
        prompt = build_elevenlabs_backing_track_prompt(TEST_HAPPY_BIRTHDAY_MXL)

        self.assertIn("Key signature: C major.", prompt)
        self.assertIn("Time signature: 4/4.", prompt)
        self.assertIn("Tempo: 120 BPM.", prompt)
        self.assertIn("Duration: 17 measures, about 0:34.", prompt)
        self.assertIn("Pickup notes begin on beat 4 of measure 1.", prompt)
        self.assertIn("The first downbeat of measure 2 should land on A4.", prompt)
        self.assertIn("Chord progression: m2: C; m3: G;", prompt)
        self.assertIn("m16: C -> G; m17: C.", prompt)


if __name__ == "__main__":
    unittest.main()
