from __future__ import annotations

from contextlib import ExitStack
import unittest
from unittest.mock import patch

from src.api.voice_part_lint_rules import LINT_RULE_SPECS
from src.api.voice_parts import _run_preflight_plan_lint


def _note(
    *,
    measure: int,
    offset: float,
    pitch: float,
    voice: str = "1",
    lyric: str | None = None,
    extended: bool = False,
    staff: str = "1",
) -> dict:
    return {
        "offset_beats": offset,
        "duration_beats": 1.0,
        "pitch_midi": pitch,
        "lyric": lyric,
        "syllabic": None if lyric in {None, "+"} else "single",
        "lyric_is_extended": extended,
        "is_rest": False,
        "voice": voice,
        "staff": staff,
        "measure_number": measure,
    }


def _part(part_id: str, part_name: str, notes: list[dict]) -> dict:
    return {
        "part_id": part_id,
        "part_name": part_name,
        "notes": notes,
    }


class VoicePartLintRegressionTests(unittest.TestCase):
    def _assert_rule_fires(
        self,
        rule_code: str,
        *,
        score: dict,
        plan: dict,
        patches: list[patch] | None = None,
    ) -> dict:
        with ExitStack() as stack:
            for active_patch in patches or []:
                stack.enter_context(active_patch)
            result = _run_preflight_plan_lint(score, plan)
        self.assertFalse(result.get("ok"), msg=f"expected {rule_code} to fail lint")
        findings = result.get("findings") or []
        finding = next((item for item in findings if item.get("rule") == rule_code), None)
        self.assertIsNotNone(
            finding,
            msg=f"expected lint finding for {rule_code}, got {[item.get('rule') for item in findings]}",
        )
        self.assertEqual(finding.get("rule_name"), LINT_RULE_SPECS[rule_code].name)
        self.assertIsInstance(finding.get("failing_attributes"), dict)
        self.assertTrue(finding.get("message"))
        return finding

    def test_every_registered_rule_has_regression_coverage(self) -> None:
        expected = {
            "plan_requires_sections",
            "mixed_region_requires_sections",
            "section_timeline_contiguous_no_gaps",
            "trivial_method_requires_equal_chord_voice_part_count",
            "cross_staff_melody_source_when_local_available",
            "cross_staff_lyric_source_with_stronger_local_alternative",
            "extension_only_lyric_source_with_word_alternative",
            "empty_lyric_source_with_word_alternative",
            "weak_lyric_source_with_better_alternative",
            "lyric_source_without_target_notes",
            "no_rest_when_target_has_native_notes",
            "same_clef_claim_coverage",
            "same_part_target_completeness",
        }
        self.assertEqual(set(LINT_RULE_SPECS), expected)

    def test_plan_requires_sections(self) -> None:
        score = {"parts": [_part("P1", "Lead", [_note(measure=1, offset=0.0, pitch=60.0)])]}
        plan = {"targets": [{"target": {"part_index": 0, "voice_part_id": "voice part 1"}}]}
        finding = self._assert_rule_fires(
            "plan_requires_sections",
            score=score,
            plan=plan,
            patches=[
                patch(
                    "src.api.voice_parts._analyze_part_voice_parts",
                    return_value={
                        "voice_parts": [
                            {"voice_part_id": "voice part 1", "source_voice_id": "1"}
                        ]
                    },
                ),
                patch(
                    "src.api.voice_parts._build_part_region_indices",
                    return_value={
                        "chord_regions": [{"start": 1, "end": 1}],
                        "default_voice_regions": [],
                        "target_resolution_by_voice_part": {
                            "voice part 1": [
                                {
                                    "status": "RESOLVED",
                                    "start_measure": 1,
                                    "end_measure": 1,
                                }
                            ]
                        },
                    },
                ),
            ],
        )
        self.assertTrue(finding["details"]["has_chords"])

    def test_mixed_region_requires_sections(self) -> None:
        score = {"parts": [_part("P1", "Lead", [_note(measure=1, offset=0.0, pitch=60.0)])]}
        plan = {"targets": [{"target": {"part_index": 0, "voice_part_id": "voice part 1"}}]}
        finding = self._assert_rule_fires(
            "mixed_region_requires_sections",
            score=score,
            plan=plan,
            patches=[
                patch(
                    "src.api.voice_parts._analyze_part_voice_parts",
                    return_value={
                        "voice_parts": [
                            {"voice_part_id": "voice part 1", "source_voice_id": "1"}
                        ]
                    },
                ),
                patch(
                    "src.api.voice_parts._build_part_region_indices",
                    return_value={
                        "chord_regions": [],
                        "default_voice_regions": [{"start": 2, "end": 2}],
                        "target_resolution_by_voice_part": {
                            "voice part 1": [
                                {
                                    "status": "RESOLVED",
                                    "start_measure": 1,
                                    "end_measure": 1,
                                },
                                {
                                    "status": "UNASSIGNED_SOURCE",
                                    "start_measure": 2,
                                    "end_measure": 2,
                                },
                            ]
                        },
                    },
                ),
            ],
        )
        self.assertEqual(
            finding["details"]["region_statuses"], ["RESOLVED", "UNASSIGNED_SOURCE"]
        )

    def test_section_timeline_contiguous_no_gaps(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "Lead",
                    [
                        _note(measure=1, offset=0.0, pitch=60.0),
                        _note(measure=3, offset=0.0, pitch=62.0),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [
                        {"start_measure": 1, "end_measure": 1, "mode": "derive"},
                        {"start_measure": 3, "end_measure": 3, "mode": "rest"},
                    ],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "section_timeline_contiguous_no_gaps",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["details"]["issue"], "gap_or_overlap")

    def test_trivial_method_requires_equal_chord_voice_part_count(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=72.0, voice="1"),
                        _note(measure=1, offset=0.0, pitch=69.0, voice="1"),
                        _note(measure=1, offset=0.0, pitch=65.0, voice="1"),
                        _note(measure=1, offset=0.0, pitch=55.0, voice="2"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                            "method": "trivial",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                            "method": "trivial",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                },
            ]
        }
        finding = self._assert_rule_fires(
            "trivial_method_requires_equal_chord_voice_part_count",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["failing_attributes"]["target_lane_count"], 2)
        self.assertEqual(
            finding["failing_attributes"]["expected_simultaneous_note_count"], 3
        )

    def test_cross_staff_melody_source_when_local_available(self) -> None:
        score = {
            "parts": [
                _part("P1", "Choir A", [_note(measure=1, offset=0.0, pitch=60.0)]),
                _part("P2", "Choir B", [_note(measure=1, offset=0.0, pitch=67.0)]),
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 1, "voice_part_id": "voice part 1"},
                        }
                    ],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "cross_staff_melody_source_when_local_available",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["source_part_index"], 1)

    def test_cross_staff_lyric_source_with_stronger_local_alternative(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "Choir A",
                    [
                        _note(measure=1, offset=0.0, pitch=60.0, lyric="local"),
                        _note(measure=1, offset=1.0, pitch=60.0, lyric="words"),
                        _note(measure=1, offset=2.0, pitch=60.0, lyric="stay"),
                        _note(measure=1, offset=3.0, pitch=60.0, lyric="here"),
                    ],
                ),
                _part(
                    "P2",
                    "Choir B",
                    [
                        _note(measure=1, offset=0.0, pitch=67.0, lyric="cross"),
                        _note(measure=1, offset=1.0, pitch=67.0, lyric="+", extended=True),
                        _note(measure=1, offset=2.0, pitch=67.0),
                        _note(measure=1, offset=3.0, pitch=67.0),
                    ],
                ),
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                            "lyric_source": {"part_index": 1, "voice_part_id": "voice part 1"},
                        }
                    ],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "cross_staff_lyric_source_with_stronger_local_alternative",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["source_part_index"], 1)

    def test_extension_only_lyric_source_with_word_alternative(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=67.0, voice="1", lyric="+", extended=True),
                        _note(measure=1, offset=1.0, pitch=67.0, voice="1", lyric="+", extended=True),
                        _note(measure=1, offset=0.0, pitch=60.0, voice="2", lyric="God"),
                        _note(measure=1, offset=1.0, pitch=60.0, voice="2", lyric="is"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
                        }
                    ],
                },
            ]
        }
        finding = self._assert_rule_fires(
            "extension_only_lyric_source_with_word_alternative",
            score=score,
            plan=plan,
        )
        self.assertEqual(
            finding["suggested_lyric_source"]["voice_part_id"],
            "alto",
        )

    def test_empty_lyric_source_with_word_alternative(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=67.0, voice="1"),
                        _note(measure=1, offset=1.0, pitch=67.0, voice="1"),
                        _note(measure=1, offset=0.0, pitch=60.0, voice="2", lyric="God"),
                        _note(measure=1, offset=1.0, pitch=60.0, voice="2", lyric="is"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
                        }
                    ],
                },
            ]
        }
        finding = self._assert_rule_fires(
            "empty_lyric_source_with_word_alternative",
            score=score,
            plan=plan,
        )
        self.assertEqual(
            finding["selected_lyric_source"]["stats"]["lyric_note_count"],
            0,
        )

    def test_weak_lyric_source_with_better_alternative(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=67.0, voice="1", lyric="be"),
                        _note(measure=1, offset=1.0, pitch=67.0, voice="1", lyric="+", extended=True),
                        _note(measure=1, offset=2.0, pitch=67.0, voice="1", lyric="+", extended=True),
                        _note(measure=1, offset=3.0, pitch=67.0, voice="1", lyric="+", extended=True),
                        _note(measure=1, offset=0.0, pitch=60.0, voice="2", lyric="God"),
                        _note(measure=1, offset=1.0, pitch=60.0, voice="2", lyric="the"),
                        _note(measure=1, offset=2.0, pitch=60.0, voice="2", lyric="Fa"),
                        _note(measure=1, offset=3.0, pitch=60.0, voice="2", lyric="ther"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
                        }
                    ],
                },
            ]
        }
        finding = self._assert_rule_fires(
            "weak_lyric_source_with_better_alternative",
            score=score,
            plan=plan,
        )
        self.assertLess(
            finding["selected_lyric_source"]["stats"]["word_lyric_coverage_ratio"],
            finding["suggested_lyric_source"]["stats"]["word_lyric_coverage_ratio"],
        )

    def test_lyric_source_without_target_notes(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=2, offset=0.0, pitch=60.0, voice="1"),
                        _note(measure=1, offset=0.0, pitch=55.0, voice="2", lyric="src"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "alto"},
                        },
                        {
                            "start_measure": 2,
                            "end_measure": 2,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                        },
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 2,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
                        }
                    ],
                },
            ]
        }
        finding = self._assert_rule_fires(
            "lyric_source_without_target_notes",
            score=score,
            plan=plan,
        )
        self.assertFalse(finding["details"]["native_sung_measure_overlap"])

    def test_no_rest_when_target_has_native_notes(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "Lead",
                    [
                        _note(measure=1, offset=0.0, pitch=64.0, lyric="la"),
                        _note(measure=2, offset=0.0, pitch=65.0, lyric="la"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [{"start_measure": 1, "end_measure": 2, "mode": "rest"}],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "no_rest_when_target_has_native_notes",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["failing_attributes"]["overlap_measure_count"], 2)

    def test_same_clef_claim_coverage(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=64.0, voice="1", lyric="A"),
                        _note(measure=2, offset=0.0, pitch=55.0, voice="2"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                        },
                        {"start_measure": 2, "end_measure": 2, "mode": "rest"},
                    ],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "same_clef_claim_coverage",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["missing_ranges"], [{"start": 2, "end": 2}])

    def test_same_part_target_completeness(self) -> None:
        score = {
            "parts": [
                _part(
                    "P1",
                    "SOPRANO ALTO",
                    [
                        _note(measure=1, offset=0.0, pitch=67.0, voice="1", lyric="top"),
                        _note(measure=1, offset=0.0, pitch=60.0, voice="2", lyric="low"),
                    ],
                )
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
                        }
                    ],
                }
            ]
        }
        finding = self._assert_rule_fires(
            "same_part_target_completeness",
            score=score,
            plan=plan,
        )
        self.assertEqual(finding["missing_voice_part_ids"], ["soprano"])
