from __future__ import annotations

import os
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from pprint import pformat

from src.api import parse_score, synthesize
from src.api.voice_parts import (
    parse_voice_part_plan,
    prepare_score_for_voice_part,
    preprocess_voice_parts,
    validate_voice_part_status,
)


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
        if result.get("status") == "action_required":
            print("\n[action_required] test_synthesize_returns_action_required_for_missing_lyrics")
            print(pformat(result, width=120))
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
        if result.get("status") == "action_required":
            print("\n[action_required] test_synthesize_requires_source_voice_part_when_enabled")
            print(pformat(result, width=120))
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


class VoicePartAnalysisAndPlanTests(unittest.TestCase):
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
    def _run_alto_preprocess(self):
        score = parse_score(TEST_XML, part_index=0, verse_number=1)
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
