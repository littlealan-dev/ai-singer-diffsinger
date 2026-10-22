from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay" / "run_preprocess_replay.py"
_spec = importlib.util.spec_from_file_location("run_preprocess_replay", _SCRIPT)
replay = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(replay)


def _note(measure, offset, pitch, lyric=None, *, rest=False):
    return {
        "measure_number": measure,
        "offset_beats": offset,
        "pitch_midi": pitch,
        "duration_beats": 1.0,
        "lyric": lyric,
        "is_rest": rest,
    }


class FindingFromIssueTests(unittest.TestCase):
    def test_lint_finding_uses_rule_severity_and_attribute_measures(self) -> None:
        issue = {
            "rule": "melody_source_requires_lyric_source",
            "rule_severity": "P0",
            "rule_domain": "LYRIC",
            "severity": "error",
            "part_index": 0,
            "target_voice_part_id": "voice part 2",
            "failing_attributes": {"start_measure": 3, "end_measure": 5},
        }
        self.assertEqual(
            replay._finding_from_issue(issue),
            {
                "rule": "melody_source_requires_lyric_source",
                "severity": "P0",
                "domain": "LYRIC",
                "target": "0:voice part 2",
                "measures": [3, 5],
            },
        )

    def test_postflight_finding_uses_its_severity_and_domain(self) -> None:
        issue = {"rule": "partial_lyric_coverage", "severity": "P2", "domain": "LYRIC"}
        finding = replay._finding_from_issue(issue)
        self.assertEqual(finding["severity"], "P2")
        self.assertEqual(finding["domain"], "LYRIC")
        self.assertIsNone(finding["target"])
        self.assertIsNone(finding["measures"])


class LanesTests(unittest.TestCase):
    def test_unchanged_parts_are_skipped_and_derived_lanes_are_named(self) -> None:
        original = {"parts": [{"part_id": "P1", "part_name": "Choir", "notes": [_note(1, 0.0, 60, "la")]}]}
        final = {
            "parts": [
                {"part_id": "P1", "part_name": "Choir", "notes": [_note(1, 0.0, 60, "la")]},
                {
                    "part_id": "P_DERIVED_ABC",
                    "part_name": "Choir - voice part 2 (Derived)",
                    "notes": [_note(1, 0.0, 55, "la"), _note(1, 1.0, None, rest=True)],
                },
            ]
        }
        lanes = replay._lanes(original, final)
        self.assertEqual([lane["lane_id"] for lane in lanes], ["Choir - voice part 2 (Derived)"])
        self.assertEqual(
            lanes[0]["notes"],
            [{"measure": 1, "offset_beats": 0.0, "pitch_midi": 55, "duration_beats": 1.0, "lyric": "la"}],
        )

    def test_changed_original_part_and_duplicate_names(self) -> None:
        original = {"parts": [{"part_id": "P1", "part_name": "Alto", "notes": [_note(1, 0.0, 60)]}]}
        final = {
            "parts": [
                {"part_id": "P1", "part_name": "Alto", "notes": [_note(1, 0.0, 60, "la")]},
                {"part_id": "P_DERIVED_A", "part_name": "X (Derived)", "notes": [_note(1, 0.0, 62)]},
                {"part_id": "P_DERIVED_B", "part_name": "X (Derived)", "notes": [_note(1, 0.0, 64)]},
            ]
        }
        self.assertEqual(
            [lane["lane_id"] for lane in replay._lanes(original, final)],
            ["P1|Alto", "X (Derived)", "X (Derived) #2"],
        )


class RecorderTests(unittest.TestCase):
    def test_phases_follow_executed_attempts_and_roles(self) -> None:
        recorder = replay.JobRecorder()
        recorder.record_llm_call("preprocess", {"prompt_tokens": 10}, 1.0)
        recorder.attempt_issues[1] = []
        recorder.record_llm_call("default", {}, 0.5)
        recorder.record_llm_call("preprocess", {"thinking_tokens": 7}, 2.0)
        self.assertEqual(
            [(call["role"], call["phase"]) for call in recorder.llm_calls],
            [("preprocess", "initial"), ("default", "message"), ("preprocess", "repair")],
        )
        self.assertEqual(recorder.llm_calls[0]["prompt_tokens"], 10)
        self.assertEqual(recorder.llm_calls[2]["thinking_tokens"], 7)

    def test_sequence_client_keeps_one_queue_per_role(self) -> None:
        class Role:
            def __init__(self, value):
                self.value = value

        client = replay.SequenceLlmClient({"preprocess": [{"n": 1}, {"n": 2}]})
        self.assertEqual(json.loads(client.generate("p", [], role=Role("preprocess"))), {"n": 1})
        self.assertEqual(json.loads(client.generate("p", [], role=Role("default"))), replay.DEFAULT_SMOKE_MESSAGE)
        self.assertEqual(json.loads(client.generate("p", [], role=Role("preprocess"))), {"n": 2})
        self.assertEqual(json.loads(client.generate("p", [], role=Role("preprocess"))), {"n": 2})


class CorpusTests(unittest.TestCase):
    def test_corpus_job_ids_need_both_files_and_respect_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corpus = Path(temp_dir)
            for job_id in ("a", "b", "c"):
                (corpus / job_id).mkdir()
                (corpus / job_id / "job.json").write_text("{}", encoding="utf-8")
                if job_id != "b":
                    (corpus / job_id / "input.xml").write_text("<x/>", encoding="utf-8")
            self.assertEqual(replay._corpus_job_ids(corpus, [], 0), ["a", "c"])
            self.assertEqual(replay._corpus_job_ids(corpus, ["c"], 0), ["c"])
            self.assertEqual(replay._corpus_job_ids(corpus, [], 1), ["a"])


if __name__ == "__main__":
    unittest.main()
