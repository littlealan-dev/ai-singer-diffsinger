from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from src.api import parse_score, synthesize
from src.api.voice_parts import (
    _choose_notes_trivial_ranked,
    _run_preflight_plan_lint,
    parse_voice_part_plan,
    prepare_score_for_voice_part,
    preprocess_voice_parts,
    validate_voice_part_status,
)


ROOT_DIR = Path(__file__).parent.parent
TEST_XML = ROOT_DIR / "assets/test_data/amazing-grace-satb-verse1.xml"


class VoicePartFlowTests(unittest.TestCase):
    def test_synthesize_returns_action_required_for_complex_raw_part(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = synthesize(
            score,
            "missing_voicebank_path",
            part_index=0,
            voice_part_id="alto",
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "preprocessing_required")
        self.assertEqual(result.get("code"), "preprocessing_required")
        self.assertEqual(
            result.get("reason"),
            "complex_score_multi_voice_and_missing_lyrics_without_derived_target",
        )
        failed_rules = result.get("failed_validation_rules") or []
        self.assertIn("complexity_signal.multi_voice_part", failed_rules)
        self.assertIn("complexity_signal.missing_lyric_voice_parts", failed_rules)
        self.assertIn("derived_detection.index_delta_not_met", failed_rules)
        self.assertIn("derived_detection.transform_metadata_not_found", failed_rules)
        diagnostics = result.get("diagnostics") or {}
        self.assertEqual(diagnostics.get("input_part_index"), 0)
        self.assertTrue(diagnostics.get("part_index_in_range"))
        self.assertFalse(diagnostics.get("is_derived_target"))
        self.assertTrue(diagnostics.get("voice_part_signals_present"))
        self.assertTrue(diagnostics.get("signal_parts_present"))
        self.assertTrue(diagnostics.get("target_signal_found"))
        self.assertTrue(diagnostics.get("has_multi_voice_part_signal"))
        self.assertTrue(diagnostics.get("has_missing_lyric_voice_parts_signal"))

    def test_synthesize_requires_preprocess_even_when_propagation_enabled(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = synthesize(
            score,
            "missing_voicebank_path",
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "preprocessing_required")

    def test_synthesize_preflight_reports_invalid_part_index(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = synthesize(
            score,
            "missing_voicebank_path",
            part_index=999,
            voice_part_id="alto",
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "preprocessing_required")
        self.assertEqual(result.get("reason"), "target_part_not_found_for_preflight")
        self.assertIn(
            "input_validation.part_index_out_of_range",
            result.get("failed_validation_rules") or [],
        )

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

    def test_synthesize_preflight_guides_derived_target_when_preprocessed(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        prep = prepare_score_for_voice_part(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        self.assertEqual(prep.get("status"), "ready")
        transformed = prep["score"]

        result = synthesize(
            transformed,
            "missing_voicebank_path",
            part_index=0,  # Intentionally raw/original part.
            voice_part_id="alto",
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "preprocessing_required")
        self.assertEqual(
            result.get("reason"),
            "preprocessed_score_without_derived_target_selection",
        )
        failed_rules = result.get("failed_validation_rules") or []
        self.assertIn(
            "derived_detection.derived_target_not_selected_after_preprocess",
            failed_rules,
        )
        diagnostics = result.get("diagnostics") or {}
        self.assertTrue(diagnostics.get("preprocessed_score_detected"))
        suggested = diagnostics.get("suggested_derived_targets_for_input_part") or []
        self.assertTrue(suggested)
        self.assertTrue(any(isinstance(item.get("part_index"), int) for item in suggested))
        self.assertIn("targeted an original complex part", str(result.get("message") or ""))


class VoicePartAnalysisAndPlanTests(unittest.TestCase):
    def test_trivial_chord_split_maps_rank_to_rank_when_counts_match(self) -> None:
        grouped = {
            (1, 0.0): [
                {"pitch_midi": 60.0},
                {"pitch_midi": 67.0},
            ],
            (1, 1.0): [
                {"pitch_midi": 62.0},
                {"pitch_midi": 69.0},
            ],
        }
        upper = _choose_notes_trivial_ranked(
            grouped,
            target_voice_rank=0,
            target_voice_part_count=2,
            split_selector="upper",
        )
        lower = _choose_notes_trivial_ranked(
            grouped,
            target_voice_rank=1,
            target_voice_part_count=2,
            split_selector="lower",
        )
        self.assertEqual([int(n["pitch_midi"]) for n in upper], [67, 69])
        self.assertEqual([int(n["pitch_midi"]) for n in lower], [60, 62])

    def test_trivial_chord_split_falls_back_when_counts_mismatch(self) -> None:
        grouped = {
            (1, 0.0): [
                {"pitch_midi": 52.0},
                {"pitch_midi": 60.0},
                {"pitch_midi": 67.0},
            ],
        }
        picked_upper = _choose_notes_trivial_ranked(
            grouped,
            target_voice_rank=0,
            target_voice_part_count=2,
            split_selector="upper",
        )
        picked_lower = _choose_notes_trivial_ranked(
            grouped,
            target_voice_rank=0,
            target_voice_part_count=2,
            split_selector="lower",
        )
        self.assertEqual(int(picked_upper[0]["pitch_midi"]), 67)
        self.assertEqual(int(picked_lower[0]["pitch_midi"]), 52)

    def test_preflight_rejects_trivial_method_when_chord_size_mismatch(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "1",
                            "pitch_midi": 72.0,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "1",
                            "pitch_midi": 69.0,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "1",
                            "pitch_midi": 65.0,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "2",
                            "pitch_midi": 55.0,
                        },
                    ],
                }
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
        lint_result = _run_preflight_plan_lint(score, plan)
        self.assertFalse(lint_result.get("ok"))
        self.assertTrue(
            any(
                finding.get("rule")
                == "trivial_method_requires_equal_chord_voice_part_count"
                for finding in (lint_result.get("findings") or [])
            )
        )

    def test_preflight_allows_trivial_method_when_chord_size_matches(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "1",
                            "pitch_midi": 72.0,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "1",
                            "pitch_midi": 69.0,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "measure_number": 1,
                            "is_rest": False,
                            "voice": "2",
                            "pitch_midi": 55.0,
                        },
                    ],
                }
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
        lint_result = _run_preflight_plan_lint(score, plan)
        failing_rules = {
            finding.get("rule") for finding in (lint_result.get("findings") or [])
        }
        self.assertNotIn(
            "trivial_method_requires_equal_chord_voice_part_count",
            failing_rules,
        )

    def test_parse_score_note_events_include_extended_fact_fields(self) -> None:
        score = parse_score(TEST_XML, verse_number=1)
        note = next(
            n
            for p in score["parts"]
            for n in p.get("notes", [])
            if not n.get("is_rest")
        )
        self.assertIn("staff", note)
        self.assertIn("chord_group_id", note)
        self.assertIn("lyric_line_index", note)

    def test_analyze_includes_coverage_map_and_verse_metadata(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        signals = score["voice_part_signals"]
        self.assertEqual(signals.get("requested_verse_number"), "1")
        self.assertIn("full_score_analysis", signals)
        part_signal = signals["parts"][0]
        self.assertIn("measure_lyric_coverage", part_signal)
        self.assertTrue(part_signal["measure_lyric_coverage"])
        measure_row = part_signal["measure_lyric_coverage"][0]
        self.assertIn("measure_number", measure_row)
        self.assertIn("voice_parts", measure_row)

    def test_analyze_includes_source_candidates_hints(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        part_signal = score["voice_part_signals"]["parts"][0]
        self.assertIn("source_candidate_hints", part_signal)
        hints = part_signal["source_candidate_hints"]
        self.assertTrue(hints)
        hint = hints[0]
        self.assertIn("target_voice_part_id", hint)
        self.assertIn("ranked_sources", hint)

    def test_parse_score_includes_structured_measure_direction_entries(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1">
      <part-name>Choir</part-name>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <direction placement="above">
        <direction-type>
          <words>Choir in Unison</words>
        </direction-type>
        <staff>1</staff>
      </direction>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>whole</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_path = Path(tmpdir) / "annotation-test.xml"
            xml_path.write_text(xml, encoding="utf-8")
            score = parse_score(xml_path, verse_number=1)
        annotations = score["voice_part_signals"]["measure_annotations"]
        self.assertTrue(annotations)
        first_part = annotations[0]
        self.assertEqual(first_part.get("part_id"), "P1")
        self.assertTrue(first_part.get("measures"))
        first_measure = first_part["measures"][0]
        self.assertIn("Choir in Unison", first_measure.get("directions", []))
        entries = first_measure.get("direction_entries", [])
        self.assertTrue(entries)
        self.assertEqual(entries[0].get("text"), "Choir in Unison")
        self.assertEqual(entries[0].get("staff"), "1")
        self.assertEqual(entries[0].get("placement"), "above")

    def test_status_taxonomy_alignment(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        taxonomy = score["voice_part_signals"]["full_score_analysis"]["status_taxonomy"]
        self.assertIn("ready", taxonomy)
        self.assertIn("ready_with_warnings", taxonomy)
        self.assertIn("action_required", taxonomy)
        self.assertIn("error", taxonomy)
        self.assertTrue(validate_voice_part_status("ready_with_warnings"))
        self.assertFalse(validate_voice_part_status("not_a_status"))

    def test_preprocess_normalizes_deprecated_voice_id(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = preprocess_voice_parts(score, part_index=0, voice_id="soprano")
        self.assertEqual(result.get("status"), "ready")
        metadata = result.get("metadata", {})
        self.assertIn("deprecated_inputs", metadata)
        self.assertIn("voice_id", metadata.get("deprecated_inputs", []))

    def test_plan_parser_accepts_copy_all_verses(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "actions": [
                        {"type": "split_voice_part", "split_shared_note_policy": "duplicate_to_all"},
                        {
                            "type": "propagate_lyrics",
                            "verse_number": "1",
                            "copy_all_verses": True,
                            "strategy": "strict_onset",
                            "policy": "fill_missing_only",
                            "source_priority": [
                                {"part_index": 0, "voice_part_id": "soprano"},
                            ],
                            "section_overrides": [
                                {
                                    "start_measure": 1,
                                    "end_measure": 2,
                                    "source": {"part_index": 0, "voice_part_id": "soprano"},
                                    "strategy": "strict_onset",
                                    "policy": "fill_missing_only",
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        parsed = parse_voice_part_plan(plan, score=score)
        self.assertTrue(parsed.get("ok"))
        target = parsed["plan"]["targets"][0]
        action = next(a for a in target["actions"] if a["type"] == "propagate_lyrics")
        self.assertTrue(action["copy_all_verses"])
        self.assertEqual(action["section_overrides"][0]["precedence"], 0)

    def test_plan_parser_rejects_unknown_action(self) -> None:
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "actions": [{"type": "explode_voice_part"}],
                }
            ]
        }
        parsed = parse_voice_part_plan(plan)
        self.assertFalse(parsed.get("ok"))
        self.assertEqual(parsed["error"]["action"], "unknown_plan_action_type")

    def test_plan_parser_rejects_invalid_override_range(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "actions": [
                        {"type": "split_voice_part"},
                        {
                            "type": "propagate_lyrics",
                            "source_priority": [{"part_index": 0, "voice_part_id": "soprano"}],
                            "section_overrides": [
                                {
                                    "start_measure": 5,
                                    "end_measure": 4,
                                    "source": {"part_index": 0, "voice_part_id": "soprano"},
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        parsed = parse_voice_part_plan(plan, score=score)
        self.assertFalse(parsed.get("ok"))
        self.assertEqual(parsed["error"]["action"], "invalid_section_override")

    def test_preprocess_plan_executes_and_propagates(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "actions": [
                        {"type": "split_voice_part"},
                        {
                            "type": "propagate_lyrics",
                            "source_priority": [{"part_index": 0, "voice_part_id": "soprano"}],
                        },
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "ready")
        transformed_notes = result["score"]["parts"][0]["notes"]
        self.assertTrue(any(note.get("lyric") for note in transformed_notes if not note.get("is_rest")))

    def test_preprocess_plan_invalid_payload_action_required(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        result = preprocess_voice_parts(score, plan={"targets": "not-a-list"})
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "invalid_plan_payload")

    def test_plan_parser_accepts_timeline_sections(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        max_measure = max(
            int(note.get("measure_number") or 0)
            for note in score["parts"][0]["notes"]
            if int(note.get("measure_number") or 0) > 0
        )
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                    "verse_number": "1",
                    "copy_all_verses": False,
                }
            ]
        }
        parsed = parse_voice_part_plan(plan, score=score)
        self.assertTrue(parsed.get("ok"))
        target = parsed["plan"]["targets"][0]
        self.assertIn("sections", target)
        self.assertEqual(target["sections"][0]["mode"], "derive")

    def test_plan_parser_rejects_timeline_sections_non_contiguous(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        max_measure = max(
            int(note.get("measure_number") or 0)
            for note in score["parts"][0]["notes"]
            if int(note.get("measure_number") or 0) > 0
        )
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 2,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                        }
                    ],
                }
            ]
        }
        parsed = parse_voice_part_plan(plan, score=score)
        self.assertFalse(parsed.get("ok"))
        self.assertEqual(parsed["error"]["action"], "non_contiguous_sections")

    def test_preprocess_plan_executes_timeline_sections(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        max_measure = max(
            int(note.get("measure_number") or 0)
            for note in score["parts"][0]["notes"]
            if int(note.get("measure_number") or 0) > 0
        )
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "melody_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                },
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertNotEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertFalse(
            any(f.get("rule") == "lyric_source_without_target_notes" for f in findings)
        )
        metadata = result.get("metadata", {})
        self.assertEqual(metadata.get("plan_mode"), "timeline_sections")

    def test_preflight_guard_requires_all_same_part_sibling_targets(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        max_measure = max(
            int(note.get("measure_number") or 0)
            for note in score["parts"][0]["notes"]
            if int(note.get("measure_number") or 0) > 0
        )
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "alto"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "soprano"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "same_part_target_completeness" for f in findings)
        )

    def test_preflight_lint_flags_rest_on_native_target_notes(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 64.0,
                            "lyric": "la",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 1.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 65.0,
                            "lyric": "la",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 2,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 55.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 1.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 56.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 2,
                        },
                    ],
                }
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "sections": [
                        {"start_measure": 1, "end_measure": 2, "mode": "rest"},
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "no_rest_when_target_has_native_notes" for f in findings)
        )

    def test_preflight_lint_flags_same_part_claim_coverage(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 64.0,
                            "lyric": "A",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 55.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 2,
                        },
                    ],
                }
            ]
        }
        # voice part 1 has native singing only in measure 1; measure 2 singing is in sibling line.
        # A rest at measure 2 should not hit rule 2, but should fail group claim coverage.
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
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "same_clef_claim_coverage" for f in findings)
        )

    def test_preflight_lint_flags_cross_staff_lyric_source_when_local_available(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Choir A",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "pitch_hz": 261.6,
                            "pitch_step": "C",
                            "pitch_alter": 0,
                            "pitch_octave": 4,
                            "lyric": "local",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "tie_type": None,
                            "voice": "1",
                            "staff": "1",
                            "chord_group_id": None,
                            "lyric_line_index": "1",
                            "measure_number": 1,
                        }
                    ],
                },
                {
                    "part_id": "P2",
                    "part_name": "Choir B",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 65.0,
                            "pitch_hz": 349.2,
                            "pitch_step": "F",
                            "pitch_alter": 0,
                            "pitch_octave": 4,
                            "lyric": "cross",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "tie_type": None,
                            "voice": "1",
                            "staff": "1",
                            "chord_group_id": None,
                            "lyric_line_index": "1",
                            "measure_number": 1,
                        }
                    ],
                },
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
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(
                f.get("rule") == "cross_staff_lyric_source_when_local_available"
                for f in findings
            )
        )

    def test_preflight_lint_flags_lyric_source_without_target_notes(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 2,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 55.0,
                            "lyric": "src",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 1,
                        },
                    ],
                }
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
                            "lyric_strategy": "overlap_best_match",
                            "lyric_policy": "fill_missing_only",
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
        result = preprocess_voice_parts(score, plan=plan)
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertTrue(
            any(f.get("rule") == "lyric_source_without_target_notes" for f in findings)
        )

    def test_preflight_allows_lyric_only_when_target_notes_exist(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 55.0,
                            "lyric": "src",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 1,
                        },
                    ],
                }
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
                            "lyric_strategy": "overlap_best_match",
                            "lyric_policy": "fill_missing_only",
                        },
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
        result = preprocess_voice_parts(score, plan=plan)
        self.assertNotEqual(result.get("action"), "plan_lint_failed")
        findings = result.get("lint_findings") or []
        self.assertFalse(
            any(f.get("rule") == "lyric_source_without_target_notes" for f in findings)
        )

    def test_section_results_include_dropped_source_lyrics_diagnostics(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Lead",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 62.0,
                            "lyric": "God",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": None,
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 1.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 64.0,
                            "lyric": "be",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": None,
                            "measure_number": 1,
                        },
                    ],
                }
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 2"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "fill_missing_only",
                        }
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertIn(result.get("status"), {"ready", "ready_with_warnings"})
        section_results = (result.get("metadata") or {}).get("section_results") or []
        self.assertTrue(section_results)
        first = section_results[0]
        self.assertEqual(first.get("source_lyric_candidates_count"), 2)
        self.assertEqual(first.get("mapped_source_lyrics_count"), 1)
        self.assertEqual(first.get("dropped_source_lyrics_count"), 1)
        dropped = first.get("dropped_source_lyrics") or []
        self.assertTrue(any(item.get("lyric") == "be" for item in dropped))

    def test_section_results_include_zero_dropped_when_fully_mapped(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "Lead",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 60.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 62.0,
                            "lyric": "God",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": None,
                            "measure_number": 1,
                        },
                    ],
                }
            ]
        }
        plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 2"},
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 1,
                            "mode": "derive",
                            "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "fill_missing_only",
                        }
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(score, plan=plan)
        self.assertIn(result.get("status"), {"ready", "ready_with_warnings"})
        section_results = (result.get("metadata") or {}).get("section_results") or []
        self.assertTrue(section_results)
        first = section_results[0]
        self.assertEqual(first.get("source_lyric_candidates_count"), 1)
        self.assertEqual(first.get("mapped_source_lyrics_count"), 1)
        self.assertEqual(first.get("dropped_source_lyrics_count"), 0)
        self.assertEqual(first.get("dropped_source_lyrics"), [])


class VoicePartPropagationStrategyTests(unittest.TestCase):
    def _note(
        self,
        *,
        offset: float,
        duration: float,
        pitch: float,
        voice: str,
        measure: int,
        lyric: str | None = None,
        is_rest: bool = False,
    ) -> dict:
        return {
            "offset_beats": offset,
            "duration_beats": duration,
            "pitch_midi": pitch,
            "lyric": lyric,
            "syllabic": "single" if lyric else None,
            "lyric_is_extended": False,
            "is_rest": is_rest,
            "voice": voice,
            "measure_number": measure,
        }

    def test_split_shared_note_policy_assign_primary_only(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        self._note(offset=0.0, duration=1.0, pitch=60.0, voice="1", measure=1, lyric="A"),
                        self._note(offset=0.0, duration=1.0, pitch=60.0, voice="2", measure=1),
                        self._note(offset=1.0, duration=1.0, pitch=65.0, voice="1", measure=1, lyric="B"),
                        self._note(offset=1.0, duration=1.0, pitch=55.0, voice="2", measure=1),
                    ],
                }
            ]
        }
        duplicate_result = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            split_shared_note_policy="duplicate_to_all",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        primary_only_result = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            split_shared_note_policy="assign_primary_only",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        duplicate_notes = [n for n in duplicate_result["score"]["parts"][0]["notes"] if not n.get("is_rest")]
        primary_notes = [n for n in primary_only_result["score"]["parts"][0]["notes"] if not n.get("is_rest")]
        self.assertGreater(len(duplicate_notes), len(primary_notes))

    def test_deterministic_split_output(self) -> None:
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
        first = preprocess_voice_parts(score, part_index=0, voice_part_id="soprano")
        second = preprocess_voice_parts(score, part_index=0, voice_part_id="soprano")
        first_offsets = [n["offset_beats"] for n in first["score"]["parts"][0]["notes"]]
        second_offsets = [n["offset_beats"] for n in second["score"]["parts"][0]["notes"]]
        self.assertEqual(first_offsets, second_offsets)

    def test_overlap_best_match_tie_break(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        self._note(offset=0.0, duration=1.0, pitch=62.0, voice="1", measure=1, lyric="A"),
                        self._note(offset=0.25, duration=1.0, pitch=63.0, voice="1", measure=1, lyric="B"),
                        self._note(offset=0.5, duration=1.0, pitch=57.0, voice="2", measure=1),
                    ],
                }
            ]
        }
        result = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
            propagation_strategy="overlap_best_match",
        )
        notes = [n for n in result["score"]["parts"][0]["notes"] if not n.get("is_rest")]
        self.assertEqual(notes[0].get("lyric"), "B")

    def test_syllable_flow_phrase_boundary(self) -> None:
        original = os.environ.get("VOICE_PART_SYLLABLE_FLOW_ENABLED")
        os.environ["VOICE_PART_SYLLABLE_FLOW_ENABLED"] = "1"
        try:
            score = {
                "parts": [
                    {
                        "part_id": "P1",
                        "part_name": "SOPRANO ALTO",
                        "notes": [
                            self._note(offset=0.0, duration=1.0, pitch=64.0, voice="1", measure=1, lyric="I"),
                            self._note(offset=1.0, duration=1.0, pitch=64.0, voice="1", measure=1, lyric="sing"),
                            self._note(offset=4.0, duration=1.0, pitch=64.0, voice="1", measure=2, lyric="now"),
                            self._note(offset=0.0, duration=1.0, pitch=55.0, voice="2", measure=1),
                            self._note(offset=1.0, duration=1.0, pitch=55.0, voice="2", measure=1),
                            self._note(offset=4.0, duration=1.0, pitch=55.0, voice="2", measure=2),
                        ],
                    }
                ]
            }
            result = preprocess_voice_parts(
                score,
                part_index=0,
                voice_part_id="alto",
                allow_lyric_propagation=True,
                source_part_index=0,
                source_voice_part_id="soprano",
                propagation_strategy="syllable_flow",
            )
            notes = [n for n in result["score"]["parts"][0]["notes"] if not n.get("is_rest")]
            self.assertEqual([n.get("lyric") for n in notes], ["I", "sing", "now"])
        finally:
            if original is None:
                os.environ.pop("VOICE_PART_SYLLABLE_FLOW_ENABLED", None)
            else:
                os.environ["VOICE_PART_SYLLABLE_FLOW_ENABLED"] = original

    def test_verse_number_and_copy_all_verses(self) -> None:
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        self._note(offset=0.0, duration=1.0, pitch=64.0, voice="1", measure=1, lyric="1.Hal"),
                        self._note(offset=1.0, duration=1.0, pitch=64.0, voice="1", measure=1, lyric="2.le"),
                        self._note(offset=0.0, duration=1.0, pitch=55.0, voice="2", measure=1),
                        self._note(offset=1.0, duration=1.0, pitch=55.0, voice="2", measure=1),
                    ],
                }
            ]
        }
        verse1_only = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
            verse_number="1",
            copy_all_verses=False,
        )
        copy_all = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
            verse_number="1",
            copy_all_verses=True,
        )
        copy_all_lyrics = [
            n.get("lyric")
            for n in copy_all["score"]["parts"][0]["notes"]
            if not n.get("is_rest")
        ]
        self.assertEqual(verse1_only.get("status"), "action_required")
        self.assertEqual(verse1_only.get("action"), "validation_failed_needs_review")
        self.assertEqual(copy_all_lyrics, ["1.Hal", "2.le"])

    def test_validate_ready_with_warnings_coverage_threshold(self) -> None:
        notes = []
        for idx in range(10):
            notes.append(
                self._note(
                    offset=float(idx),
                    duration=1.0,
                    pitch=64.0,
                    voice="1",
                    measure=1 + (idx // 4),
                    lyric=f"A{idx}" if idx < 9 else None,
                )
            )
            notes.append(
                self._note(
                    offset=float(idx),
                    duration=1.0,
                    pitch=55.0,
                    voice="2",
                    measure=1 + (idx // 4),
                )
            )
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": notes,
                }
            ]
        }
        result = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        self.assertEqual(result.get("status"), "ready_with_warnings")
        self.assertEqual(result["validation"]["lyric_coverage_ratio"], 0.9)
        self.assertEqual(result["validation"]["lyric_exempt_note_count"], 0)


class VoicePartRepairLoopTests(unittest.TestCase):
    def _make_score(self, *, target_offset: float) -> dict:
        return {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": [
                        {
                            "offset_beats": 0.0,
                            "duration_beats": 1.0,
                            "pitch_midi": 64.0,
                            "lyric": "A",
                            "syllabic": "single",
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "1",
                            "measure_number": 1,
                        },
                        {
                            "offset_beats": target_offset,
                            "duration_beats": 1.0,
                            "pitch_midi": 55.0,
                            "lyric": None,
                            "syllabic": None,
                            "lyric_is_extended": False,
                            "is_rest": False,
                            "voice": "2",
                            "measure_number": 1 if target_offset < 4 else 4,
                        },
                    ],
                }
            ]
        }

    def test_repair_loop_retry_success(self) -> None:
        original = os.environ.get("VOICE_PART_REPAIR_LOOP_ENABLED")
        os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = "1"
        try:
            score = self._make_score(target_offset=0.5)
            plan = {
                "targets": [
                    {
                        "target": {"part_index": 0, "voice_part_id": "alto"},
                        "actions": [
                            {"type": "split_voice_part"},
                            {
                                "type": "propagate_lyrics",
                                "strategy": "strict_onset",
                                "source_priority": [
                                    {"part_index": 0, "voice_part_id": "soprano"}
                                ],
                            },
                        ],
                    }
                ]
            }
            result = preprocess_voice_parts(score, plan=plan)
            self.assertEqual(result.get("status"), "ready")
            repair_loop = result.get("metadata", {}).get("repair_loop", {})
            self.assertTrue(repair_loop.get("attempted"))
            self.assertEqual(repair_loop.get("attempt_count"), 1)
        finally:
            if original is None:
                os.environ.pop("VOICE_PART_REPAIR_LOOP_ENABLED", None)
            else:
                os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = original

    def test_repair_loop_escalation_after_retries(self) -> None:
        original = os.environ.get("VOICE_PART_REPAIR_LOOP_ENABLED")
        os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = "1"
        try:
            score = self._make_score(target_offset=10.0)
            plan = {
                "targets": [
                    {
                        "target": {"part_index": 0, "voice_part_id": "alto"},
                        "actions": [
                            {"type": "split_voice_part"},
                            {
                                "type": "propagate_lyrics",
                                "strategy": "strict_onset",
                                "source_priority": [
                                    {"part_index": 0, "voice_part_id": "soprano"}
                                ],
                            },
                        ],
                    }
                ]
            }
            result = preprocess_voice_parts(score, plan=plan)
            self.assertEqual(result.get("status"), "action_required")
            self.assertEqual(result.get("action"), "validation_failed_needs_review")
            repair_loop = result.get("repair_loop", {})
            self.assertTrue(repair_loop.get("attempted"))
            self.assertTrue(repair_loop.get("escalated"))
        finally:
            if original is None:
                os.environ.pop("VOICE_PART_REPAIR_LOOP_ENABLED", None)
            else:
                os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = original


class VoicePartMaterializeAndPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._output_dir = ROOT_DIR / "tests/output/voice_parts_materialize"
        cls._output_dir.mkdir(parents=True, exist_ok=True)
        cls._score_copy = cls._output_dir / TEST_XML.name
        shutil.copyfile(TEST_XML, cls._score_copy)

    def _run_alto_preprocess(self):
        score = parse_score(self._score_copy, part_index=0, verse_number=1)
        return preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )

    def test_materialize_append_modified_musicxml(self) -> None:
        result = self._run_alto_preprocess()
        self.assertIn(result.get("status"), {"ready", "ready_with_warnings"})
        modified_path = result.get("modified_musicxml_path")
        self.assertIsInstance(modified_path, str)
        self.assertTrue(Path(modified_path).exists())
        part_ref = result.get("appended_part_ref", {})
        self.assertTrue(str(part_ref.get("part_id", "")).startswith("P_DERIVED_"))
        root = ET.parse(modified_path).getroot()
        part_id = part_ref["part_id"]
        score_part_nodes = root.findall(f".//score-part[@id='{part_id}']")
        part_nodes = root.findall(f".//part[@id='{part_id}']")
        self.assertTrue(score_part_nodes)
        self.assertTrue(part_nodes)
        self.assertGreaterEqual(int(result["part_index"]), 1)

    def test_golden_musicxml_deterministic_appended_id(self) -> None:
        first = self._run_alto_preprocess()
        second = self._run_alto_preprocess()
        self.assertEqual(first["transform_id"], second["transform_id"])
        self.assertEqual(
            first["appended_part_ref"]["part_id"],
            second["appended_part_ref"]["part_id"],
        )
        first_path = Path(first["modified_musicxml_path"])
        second_path = Path(second["modified_musicxml_path"])
        self.assertEqual(first_path, second_path)
        self.assertEqual(first["transform_hash"], second["transform_hash"])

    def test_fingerprint_idempotent_reuse(self) -> None:
        first = self._run_alto_preprocess()
        second = self._run_alto_preprocess()
        self.assertEqual(first["score_fingerprint"], second["score_fingerprint"])
        self.assertEqual(first["transform_hash"], second["transform_hash"])
        self.assertTrue(bool(second.get("reused_transform")))

    def test_e2e_idempotent_reuse(self) -> None:
        first = self._run_alto_preprocess()
        second = self._run_alto_preprocess()
        self.assertEqual(first["transform_id"], second["transform_id"])
        self.assertTrue(bool(second.get("reused_transform")))

    def test_concurrency_lock_best_effort(self) -> None:
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                results.append(self._run_alto_preprocess())
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join(timeout=20.0)
        t2.join(timeout=20.0)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["transform_id"], results[1]["transform_id"])
        self.assertTrue(Path(results[0]["modified_musicxml_path"]).exists())


if __name__ == "__main__":
    unittest.main()
