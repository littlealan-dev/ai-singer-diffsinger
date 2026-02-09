from __future__ import annotations

import unittest
from pathlib import Path

from src.api import parse_score, synthesize
from src.api.voice_parts import prepare_score_for_voice_part


ROOT_DIR = Path(__file__).parent.parent
TEST_XML = ROOT_DIR / "assets/test_data/amazing-grace-satb-verse1.xml"


class VoicePartFlowTests(unittest.TestCase):
    def test_synthesize_returns_action_required_for_missing_lyrics(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = synthesize(
            score,
            "missing_voicebank_path",
            part_index=0,
            voice_part_id="alto",
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "confirm_lyric_propagation")
        options = result.get("source_voice_part_options", [])
        self.assertTrue(
            any(
                opt.get("source_part_index") == 0 and opt.get("source_voice_part_id") == "soprano"
                for opt in options
                if isinstance(opt, dict)
            )
        )

    def test_synthesize_requires_source_voice_part_when_enabled(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = synthesize(
            score,
            "missing_voicebank_path",
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "select_source_voice_part")

    def test_prepare_score_propagates_lyrics_when_confirmed(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = prepare_score_for_voice_part(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        self.assertEqual(result.get("status"), "ready")
        transformed = result["score"]
        notes = transformed["parts"][0]["notes"]
        self.assertTrue(any(note.get("lyric") for note in notes if not note.get("is_rest")))
        self.assertIn("voice_part_transforms", transformed)


if __name__ == "__main__":
    unittest.main()
