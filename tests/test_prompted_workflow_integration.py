from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.api import parse_score, preprocess_voice_parts, save_audio, synthesize


ROOT_DIR = Path(__file__).resolve().parents[1]
VOICEBANK_ID = "Raine_Rena_2.01"
VOICEBANK_PATH = ROOT_DIR / "assets/voicebanks" / VOICEBANK_ID

SIMPLE_SCORE = ROOT_DIR / "assets/test_data/amazing-grace.mxl"
COMPLEX_SCORE = ROOT_DIR / "assets/test_data/my-tribute-bars19-36.xml"

SYSTEM_PROMPT_PATH = ROOT_DIR / "src/backend/config/system_prompt.txt"
LESSONS_PROMPT_PATH = ROOT_DIR / "src/backend/config/system_prompt_lessons.txt"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _summarize_synth_result(result: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(result)
    waveform = out.pop("waveform", None)
    if waveform is not None:
        try:
            out["waveform_len"] = len(waveform)
        except TypeError:
            out["waveform_len"] = None
    return out


class PromptedWorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not VOICEBANK_PATH.exists():
            raise unittest.SkipTest(f"Voicebank not found: {VOICEBANK_PATH}")
        if not SIMPLE_SCORE.exists():
            raise unittest.SkipTest(f"Simple score not found: {SIMPLE_SCORE}")
        if not COMPLEX_SCORE.exists():
            raise unittest.SkipTest(f"Complex score not found: {COMPLEX_SCORE}")
        if not SYSTEM_PROMPT_PATH.exists():
            raise unittest.SkipTest(f"System prompt not found: {SYSTEM_PROMPT_PATH}")
        if not LESSONS_PROMPT_PATH.exists():
            raise unittest.SkipTest(f"Lessons prompt not found: {LESSONS_PROMPT_PATH}")

    def setUp(self) -> None:
        run_id = f"{_utc_stamp()}_{uuid4().hex[:8]}"
        self.run_dir = ROOT_DIR / "tests/output/prompted_workflow_integration" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        self.lessons_prompt = LESSONS_PROMPT_PATH.read_text(encoding="utf-8")
        self.combined_prompt = f"{self.system_prompt}\n\n---\n\n{self.lessons_prompt}"

        (self.run_dir / "system_prompt.txt").write_text(self.system_prompt, encoding="utf-8")
        (self.run_dir / "system_prompt_lessons.txt").write_text(self.lessons_prompt, encoding="utf-8")
        (self.run_dir / "combined_system_prompt.txt").write_text(self.combined_prompt, encoding="utf-8")

    def _resolve_part_measure_span(self, parsed_score: Dict[str, Any], part_index: int) -> Tuple[int, int]:
        parts = parsed_score.get("parts") or []
        if part_index < 0 or part_index >= len(parts):
            raise AssertionError(f"Invalid part index: {part_index}")
        measures = [
            int(note.get("measure_number") or 0)
            for note in (parts[part_index].get("notes") or [])
            if int(note.get("measure_number") or 0) > 0
        ]
        if not measures:
            raise AssertionError(f"No measurable notes found for part_index={part_index}")
        return min(measures), max(measures)

    def _pick_llm_target_part(self, parsed_score: Dict[str, Any]) -> int:
        signals = parsed_score.get("voice_part_signals") or {}
        part_signals = signals.get("parts") or []
        for part_signal in part_signals:
            missing = part_signal.get("missing_lyric_voice_parts") or []
            if missing:
                return int(part_signal["part_index"])
        for part_signal in part_signals:
            if part_signal.get("multi_voice_part"):
                return int(part_signal["part_index"])
        return 0

    def _pick_same_part_source_voice_id(
        self, part_signal: Dict[str, Any], target_voice_part_id: str
    ) -> str:
        vp_map = part_signal.get("voice_part_id_to_source_voice_id") or {}
        candidates = [
            str(vp_id)
            for vp_id, source_voice_id in vp_map.items()
            if str(source_voice_id) != "_default" and str(vp_id) != str(target_voice_part_id)
        ]
        if not candidates:
            raise AssertionError(f"No same-part source candidate for {target_voice_part_id}")
        return sorted(candidates)[0]

    def _llm_generate_plan_from_parse(self, parsed_score: Dict[str, Any]) -> Dict[str, Any]:
        """Simulated LLM step: read parse analysis and author a concrete timeline-sections plan."""
        signals = parsed_score.get("voice_part_signals") or {}
        part_signals = signals.get("parts") or []
        target_part_index = self._pick_llm_target_part(parsed_score)
        target_signal = next(
            (
                p
                for p in part_signals
                if isinstance(p, dict) and int(p.get("part_index", -1)) == int(target_part_index)
            ),
            None,
        )
        if not isinstance(target_signal, dict):
            raise AssertionError(f"Missing part signal for target part {target_part_index}")

        min_measure, max_measure = self._resolve_part_measure_span(parsed_score, target_part_index)
        vp_map = target_signal.get("voice_part_id_to_source_voice_id") or {}
        target_voice_part_ids = sorted(
            [
                str(vp_id)
                for vp_id, source_voice_id in vp_map.items()
                if str(source_voice_id) != "_default"
            ]
        )
        if not target_voice_part_ids:
            raise AssertionError(f"No non-default voice parts on part {target_part_index}")

        targets: List[Dict[str, Any]] = []
        for target_voice_part_id in target_voice_part_ids:
            source_voice_part_id = self._pick_same_part_source_voice_id(
                target_signal, target_voice_part_id
            )
            targets.append(
                {
                    "target": {
                        "part_index": target_part_index,
                        "voice_part_id": target_voice_part_id,
                    },
                    "sections": [
                        {
                            "start_measure": min_measure,
                            "end_measure": max_measure,
                            "mode": "derive",
                            "lyric_source": {
                                "part_index": target_part_index,
                                "voice_part_id": source_voice_part_id,
                            },
                            "lyric_strategy": "overlap_best_match",
                            "lyric_policy": "fill_missing_only",
                        }
                    ],
                    "verse_number": "1",
                    "copy_all_verses": False,
                    "confidence": 0.8,
                    "notes": "LLM-authored from parse_score voice_part_signals + system prompt guidance.",
                }
            )
        return {"targets": targets}

    def _llm_revise_plan_from_lint(
        self,
        plan: Dict[str, Any],
        lint_findings: List[Dict[str, Any]],
        parsed_score: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Simulated LLM repair step: revise failed sections based on lint findings."""
        revised = deepcopy(plan)
        signals = parsed_score.get("voice_part_signals") or {}
        part_signals = {
            int(p.get("part_index", -1)): p
            for p in (signals.get("parts") or [])
            if isinstance(p, dict)
        }
        targets = revised.get("targets") or []

        for finding in lint_findings:
            rule = str(finding.get("rule") or "")
            target_index = finding.get("target_index")
            if not isinstance(target_index, int) or not (0 <= target_index < len(targets)):
                continue
            target_entry = targets[target_index]
            target = target_entry.get("target") or {}
            part_index = int(target.get("part_index", -1))
            voice_part_id = str(target.get("voice_part_id") or "")
            part_signal = part_signals.get(part_index) or {}

            if rule == "cross_staff_lyric_source_when_local_available":
                replacement = self._pick_same_part_source_voice_id(part_signal, voice_part_id)
                for section in target_entry.get("sections") or []:
                    if section.get("mode") != "derive":
                        continue
                    lyric_source = section.get("lyric_source") or {}
                    lyric_source["part_index"] = part_index
                    lyric_source["voice_part_id"] = replacement
                    section["lyric_source"] = lyric_source

            if rule in {
                "empty_lyric_source_with_word_alternative",
                "extension_only_lyric_source_with_word_alternative",
            }:
                suggested = finding.get("suggested_lyric_source") or {}
                section_scope = finding.get("section") or {}
                for section in target_entry.get("sections") or []:
                    if (
                        int(section.get("start_measure", -1)) == int(section_scope.get("start_measure", -2))
                        and int(section.get("end_measure", -1)) == int(section_scope.get("end_measure", -2))
                        and section.get("mode") == "derive"
                    ):
                        section["lyric_source"] = {
                            "part_index": int(suggested.get("part_index")),
                            "voice_part_id": str(suggested.get("voice_part_id")),
                        }

        return revised

    def _pick_cross_staff_source(
        self,
        parsed_score: Dict[str, Any],
        *,
        target_part_index: int,
        target_voice_part_id: str,
    ) -> Optional[Dict[str, Any]]:
        signals = parsed_score.get("voice_part_signals") or {}
        for part_signal in signals.get("parts") or []:
            if int(part_signal.get("part_index", -1)) != int(target_part_index):
                continue
            for hint in part_signal.get("source_candidate_hints") or []:
                if str(hint.get("target_voice_part_id")) != str(target_voice_part_id):
                    continue
                for row in hint.get("ranked_sources") or []:
                    part_idx = row.get("part_index")
                    vp_id = row.get("voice_part_id")
                    if isinstance(part_idx, int) and isinstance(vp_id, str) and part_idx != target_part_index:
                        return {"part_index": int(part_idx), "voice_part_id": str(vp_id)}
        return None

    def _llm_revise_plan_from_validation(
        self,
        plan: Dict[str, Any],
        preprocess_result: Dict[str, Any],
        parsed_score: Dict[str, Any],
    ) -> Dict[str, Any]:
        revised = deepcopy(plan)
        target_part_index = int(preprocess_result.get("part_index", -1))
        target_voice_part_id = str(preprocess_result.get("target_voice_part") or "")
        failing_ranges = preprocess_result.get("failing_ranges") or []
        if target_part_index < 0 or not target_voice_part_id or not failing_ranges:
            return revised

        cross_staff_source = self._pick_cross_staff_source(
            parsed_score,
            target_part_index=target_part_index,
            target_voice_part_id=target_voice_part_id,
        )
        if cross_staff_source is None:
            return revised

        for target_entry in revised.get("targets") or []:
            target = target_entry.get("target") or {}
            if (
                int(target.get("part_index", -1)) != target_part_index
                or str(target.get("voice_part_id") or "") != target_voice_part_id
            ):
                continue
            sections = target_entry.get("sections") or []
            if not sections:
                continue
            base = sections[0]
            old_source = dict(base.get("lyric_source") or {})
            start_all = int(sections[0].get("start_measure", 1))
            end_all = int(sections[-1].get("end_measure", start_all))
            new_sections: List[Dict[str, Any]] = []
            cursor = start_all
            normalized = sorted(
                [
                    (int(r.get("start", 0)), int(r.get("end", 0)))
                    for r in failing_ranges
                    if isinstance(r, dict)
                ]
            )
            for start, end in normalized:
                if start > end:
                    continue
                if cursor < start:
                    s = deepcopy(base)
                    s["start_measure"] = cursor
                    s["end_measure"] = start - 1
                    s["lyric_source"] = dict(old_source)
                    new_sections.append(s)
                s = deepcopy(base)
                s["start_measure"] = max(start, start_all)
                s["end_measure"] = min(end, end_all)
                s["lyric_source"] = dict(cross_staff_source)
                new_sections.append(s)
                cursor = end + 1
            if cursor <= end_all:
                s = deepcopy(base)
                s["start_measure"] = cursor
                s["end_measure"] = end_all
                s["lyric_source"] = dict(old_source)
                new_sections.append(s)
            target_entry["sections"] = [s for s in new_sections if s["start_measure"] <= s["end_measure"]]
            break
        return revised

    def _run_case(self, *, label: str, score_path: Path) -> None:
        case_dir = self.run_dir / label
        case_dir.mkdir(parents=True, exist_ok=True)

        parsed = parse_score(score_path, verse_number=1)
        _write_json(case_dir / f"{label}.parsed.json", parsed)

        signals = parsed.get("voice_part_signals") or {}
        is_complex = bool(signals.get("has_multi_voice_parts")) or bool(
            signals.get("has_missing_lyric_voice_parts")
        )
        decision = {
            "workflow": "parse -> preprocess_if_needed -> synthesize",
            "complexity_detected": bool(is_complex),
            "voicebank_id": VOICEBANK_ID,
            "prompt_injected": {
                "system_prompt_loaded": True,
                "lessons_prompt_loaded": True,
            },
        }
        _write_json(case_dir / f"{label}.llm_decision.json", decision)

        working_score = parsed
        effective_part_index = 0
        preprocess_result: Optional[Dict[str, Any]] = None

        if decision["complexity_detected"]:
            llm_plan = self._llm_generate_plan_from_parse(parsed)
            _write_json(case_dir / f"{label}.generated_plan.attempt1.json", llm_plan)
            for attempt in range(1, 4):
                preprocess_result = preprocess_voice_parts(parsed, plan=llm_plan)
                _write_json(case_dir / f"{label}.preprocess.attempt{attempt}.json", preprocess_result)
                if preprocess_result.get("status") in {"ready", "ready_with_warnings"}:
                    break
                action = preprocess_result.get("action")
                if action == "plan_lint_failed":
                    llm_plan = self._llm_revise_plan_from_lint(
                        llm_plan,
                        preprocess_result.get("lint_findings") or [],
                        parsed,
                    )
                elif action == "validation_failed_needs_review":
                    llm_plan = self._llm_revise_plan_from_validation(
                        llm_plan,
                        preprocess_result,
                        parsed,
                    )
                else:
                    self.fail(f"Unexpected preprocess failure for {label}: {preprocess_result}")
                _write_json(case_dir / f"{label}.generated_plan.attempt{attempt + 1}.json", llm_plan)

            if not preprocess_result or preprocess_result.get("status") not in {"ready", "ready_with_warnings"}:
                self.fail(f"Preprocess did not produce ready score for {label}: {preprocess_result}")
            working_score = preprocess_result["score"]
            effective_part_index = int(preprocess_result.get("part_index", effective_part_index))

        synth_result = synthesize(
            working_score,
            VOICEBANK_PATH,
            part_index=effective_part_index,
            device="cpu",
        )
        _write_json(case_dir / f"{label}.synthesize.json", _summarize_synth_result(synth_result))

        if synth_result.get("status") == "action_required":
            self.fail(f"Unexpected action_required for {label}: {synth_result}")
        self.assertIn("waveform", synth_result)
        self.assertIn("sample_rate", synth_result)

        audio_rel = Path("tests/output/prompted_workflow_integration") / self.run_dir.name / label / (
            f"{label}.{_utc_stamp()}.wav"
        )
        saved = save_audio(
            synth_result["waveform"],
            ROOT_DIR / audio_rel,
            sample_rate=int(synth_result["sample_rate"]),
            format="wav",
        )
        _write_json(case_dir / f"{label}.audio_meta.json", saved)

    def test_simple_score_prompted_workflow(self) -> None:
        self._run_case(label="simple_amazing_grace", score_path=SIMPLE_SCORE)

    def test_complex_score_prompted_workflow(self) -> None:
        self._run_case(label="complex_my_tribute_bars19_36", score_path=COMPLEX_SCORE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
