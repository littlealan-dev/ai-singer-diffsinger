from __future__ import annotations

import unittest

from src.api.synthesize import _apply_anchor_constrained_timing


class TestAnchorTiming(unittest.TestCase):
    def test_conserves_each_group_budget(self) -> None:
        durations = [2, 5, 2, 7, 1]
        word_boundaries = [2, 3]
        anchors = [
            {"start_frame": 10, "end_frame": 20},  # 10 frames
            {"start_frame": 20, "end_frame": 35},  # 15 frames
        ]

        out = _apply_anchor_constrained_timing(
            durations=durations,
            word_boundaries=word_boundaries,
            group_anchor_frames=anchors,
        )

        self.assertEqual(len(out), len(durations))
        self.assertEqual(sum(out[:2]), 10)
        self.assertEqual(sum(out[2:]), 15)
        self.assertTrue(all(v >= 1 for v in out))

    def test_raises_on_insufficient_anchor_budget(self) -> None:
        durations = [3, 2, 1]
        word_boundaries = [3]
        anchors = [
            {"start_frame": 100, "end_frame": 102},  # 2 frames for 3 phonemes
        ]
        with self.assertRaisesRegex(ValueError, "insufficient_anchor_budget"):
            _apply_anchor_constrained_timing(
                durations=durations,
                word_boundaries=word_boundaries,
                group_anchor_frames=anchors,
            )

    def test_largest_remainder_distribution(self) -> None:
        # Scenario: 3 phonemes, predicted [10, 10, 10]. Total anchor = 10.
        # Reserves 3 frames (1 each). Remainder 7 distributed by weights [9, 9, 9].
        # Should result in [3, 3, 4] or similar sum=10.
        durations = [10, 10, 10]
        word_boundaries = [3]
        anchors = [{"start_frame": 0, "end_frame": 10}]
        
        out = _apply_anchor_constrained_timing(
            durations=durations,
            word_boundaries=word_boundaries,
            group_anchor_frames=anchors,
        )
        self.assertEqual(sum(out), 10)
        self.assertTrue(all(v >= 3 for v in out)) # evenly distributed

        # Scenario: Skewed weights. [2, 8]. Anchor = 6.
        # Reserves 2. Remainder 4. Weights [1, 7].
        # Quotas: 1 -> 4 * (1/8) = 0.5. 7 -> 4 * (7/8) = 3.5.
        # Floors: 0, 3. Remainders: 0.5, 0.5.
        # Total floors = 3. Missing 1.
        # Both have 0.5 remainder. Tie-break usually first index? Or sorted index?
        # If tie-break is first: [1, 3] + [1, 0] -> [2, 3]? Wait.
        # Weights: 1, 7. Sum=8. Total to distribute=4.
        # 1 -> 0.5. Floor 0.
        # 7 -> 3.5. Floor 3.
        # Remainder 1 constraint.
        # Sort by remainder: both 0.5.
        # stable sort usually keeps original order?
        # If index 0 gets it: 0+1=1. Total output: 1+1=2.
        # If index 1 gets it: 3+1=4. Total output: 1+4=5.
        # Wait, 2+5=7 != 6.
        # Let's retrace.
        # Base: [1, 1]. Remaining budget: 6-2=4.
        # Weights: [1, 7].
        # Quotas: 0.5, 3.5.
        # Floors: 0, 3. Sum=3. Need 1 more.
        # Remainders: 0.5, 0.5.
        # Largest remainder picks one.
        # If item 0 picks: final extras [1, 3]. Output [2, 4]. Sum=6. Correct.
        # If item 1 picks: final extras [0, 4]. Output [1, 5]. Sum=6. Correct.
        # Let's see what implementation does.
        
        durations = [2, 8]
        word_boundaries = [2]
        anchors = [{"start_frame": 0, "end_frame": 6}]
        out = _apply_anchor_constrained_timing(
            durations=durations,
            word_boundaries=word_boundaries,
            group_anchor_frames=anchors,
        )
        self.assertEqual(sum(out), 6)
        self.assertTrue(out[0] >= 1)
        self.assertTrue(out[1] >= 1)
        
    def test_handles_empty_durations(self) -> None:
        self.assertEqual(_apply_anchor_constrained_timing(
            durations=[], word_boundaries=[], group_anchor_frames=[]
        ), [])

    def test_partitioned_scaling_keeps_onset_consonant_stiff_on_long_note(self) -> None:
        durations = [8.0, 12.0]  # consonant, vowel raw prediction
        word_boundaries = [2]
        anchors = [{"start_frame": 0, "end_frame": 200}]
        vowel_flags = [False, True]

        out = _apply_anchor_constrained_timing(
            durations=durations,
            word_boundaries=word_boundaries,
            group_anchor_frames=anchors,
            vowel_flags=vowel_flags,
        )
        self.assertEqual(sum(out), 200)
        self.assertLessEqual(out[0], 12)  # onset consonant should stay short
        self.assertGreater(out[1], 180)  # vowel absorbs long-note elasticity

if __name__ == "__main__":
    unittest.main()
