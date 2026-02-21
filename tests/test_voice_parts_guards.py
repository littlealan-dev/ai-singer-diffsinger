from __future__ import annotations
import unittest
from typing import Any, Dict
from src.api.voice_parts import preprocess_voice_parts

class VoicePartGuardTests(unittest.TestCase):
    def test_reject_action_plan_for_score_with_chords(self) -> None:
        # Score with two notes at the same offset in Measure 1 (Chord)
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Choir",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 64.0,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        }
                    ],
                }
            ]
        }
        
        # Simple action plan (split)
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "actions": [{"type": "split_voice_part"}],
                }
            ]
        }
        
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "plan_requires_sections" for f in findings),
            f"Expected plan_requires_sections rule, got {findings}"
        )

    def test_reject_action_plan_for_mixed_region_statuses(self) -> None:
        # Score with:
        # Measure 1: Resolved (has lyrics)
        # Measure 2: Unassigned (no lyrics, default voice)
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Choir",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": "la",
                            "is_rest": False,
                            "voice": "1",  # Non-default
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 4.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "is_rest": False,
                            "voice": "_default",
                            "measure_number": 2,
                        }
                    ],
                }
            ]
        }
        
        # Simple action plan
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "actions": [{"type": "split_voice_part"}],
                }
            ]
        }
        
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "mixed_region_requires_sections" for f in findings),
            f"Expected mixed_region_requires_sections rule, got {findings}"
        )

    def test_accept_action_plan_for_simple_clean_score(self) -> None:
        # Simple score, all resolved
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Solo",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": "la",
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        }
                    ],
                }
            ]
        }
        
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "actions": [{"type": "split_voice_part"}],
                }
            ]
        }
        
        result = preprocess_voice_parts(score, plan=plan)
        # Should NOT fail lint
        self.assertNotEqual(result.get("action"), "plan_lint_failed")
        # Might be 'ready' or 'ready_with_warnings' depending on other logic
        self.assertIn(result.get("status"), {"ready", "ready_with_warnings"})

if __name__ == "__main__":
    unittest.main()
