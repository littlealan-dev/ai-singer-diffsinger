import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from src.api import parse_score, save_audio, synthesize
from src.api.voice_parts import preprocess_voice_parts


class VoicePartEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.output_dir = self.root_dir / "tests/output/voice_parts_e2e"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = "1"
        self.skip_synthesis = os.environ.get("VOICE_PART_E2E_SKIP_SYNTHESIS", "0").strip() in {
            "1",
            "true",
            "yes",
        }

    def _load_full_score(self) -> dict:
        score = parse_score(self.score_path, verse_number=1)
        score["source_musicxml_path"] = str(self.score_path.resolve())
        return score

    def _load_plan_bundle_from_env(self) -> dict[str, Any]:
        plan_path = os.environ.get("VOICE_PART_E2E_PLAN_PATH", "").strip()
        plan_json = os.environ.get("VOICE_PART_E2E_PLAN_JSON", "").strip()
        if plan_path:
            payload = json.loads(Path(plan_path).read_text())
        elif plan_json:
            payload = json.loads(plan_json)
        else:
            self.skipTest(
                "Provide VOICE_PART_E2E_PLAN_PATH or VOICE_PART_E2E_PLAN_JSON for external plan input."
            )
        if not isinstance(payload, dict):
            self.fail("Plan bundle must be a JSON object.")
        targets = payload.get("targets")
        if not isinstance(targets, list) or not targets:
            self.fail("Plan bundle must include a non-empty 'targets' list.")
        return payload

    def _build_plan(
        self,
        *,
        part_index: int,
        voice_part_id: str,
        source_ref: dict | None,
        strategy: str = "overlap_best_match",
        copy_all_verses: bool = False,
    ) -> dict:
        actions = [{"type": "split_voice_part", "split_shared_note_policy": "duplicate_to_all"}]
        if source_ref is not None:
            actions.append(
                {
                    "type": "propagate_lyrics",
                    "verse_number": "1",
                    "copy_all_verses": copy_all_verses,
                    "source_priority": [source_ref],
                    "strategy": strategy,
                    "policy": "fill_missing_only",
                    "section_overrides": [],
                }
            )
        return {
            "targets": [
                {
                    "target": {"part_index": part_index, "voice_part_id": voice_part_id},
                    "actions": actions,
                    "confidence": 0.9,
                    "notes": "auto-generated plan for e2e test",
                }
            ]
        }

    def _run_flow(
        self,
        *,
        label: str,
        part_index: int,
        voice_part_id: str,
        source_ref: dict | None,
        strategy: str = "overlap_best_match",
        copy_all_verses: bool = False,
        plan_override: dict | None = None,
    ) -> None:
        score = self._load_full_score()
        plan = plan_override or self._build_plan(
            part_index=part_index,
            voice_part_id=voice_part_id,
            source_ref=source_ref,
            strategy=strategy,
            copy_all_verses=copy_all_verses,
        )

        preprocess_result = preprocess_voice_parts(score, plan=plan)
        self.assertIn(
            preprocess_result.get("status"),
            {"ready", "ready_with_warnings"},
            msg=f"{label} preprocess failed: {json.dumps(preprocess_result, default=str)[:1200]}",
        )
        self.assertTrue(preprocess_result.get("modified_musicxml_path"))

        plan_path = self.output_dir / f"{label}_plan.json"
        result_path = self.output_dir / f"{label}_execution_result.json"
        musicxml_path = self.output_dir / f"{label}_derived.xml"
        audio_path = self.output_dir / f"{label}.wav"

        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True))
        result_path.write_text(
            json.dumps(preprocess_result, indent=2, sort_keys=True, default=str)
        )
        shutil.copyfile(preprocess_result["modified_musicxml_path"], musicxml_path)
        self.assertTrue(musicxml_path.exists())

        if self.skip_synthesis:
            return

        derived_score = parse_score(musicxml_path, verse_number=1)
        derived_part_index = len(derived_score.get("parts", [])) - 1
        self.assertGreaterEqual(
            derived_part_index,
            0,
            msg=f"{label}: unable to resolve derived part index for synthesis.",
        )
        synth_result = synthesize(
            derived_score,
            self.voicebank_path,
            part_index=derived_part_index,
            skip_voice_part_preprocess=True,
        )
        if synth_result.get("status") == "action_required":
            self.fail(f"Unexpected action_required for {label}: {synth_result}")

        save_audio(
            synth_result["waveform"],
            audio_path,
            sample_rate=synth_result["sample_rate"],
        )
        self.assertTrue(audio_path.exists())

    def _find_derived_part(self, parsed_score: dict) -> dict:
        derived_parts = [
            part
            for part in parsed_score.get("parts", [])
            if str(part.get("part_name") or "").endswith("(Derived)")
        ]
        self.assertTrue(
            derived_parts,
            msg="No derived part found after re-parsing derived MusicXML.",
        )
        return derived_parts[-1]

    def _assert_derived_output(
        self,
        *,
        label: str,
        min_sung_measure: int,
        max_sung_measure: int,
        lyric_presence_ranges: list[tuple[int, int]],
    ) -> None:
        derived_path = self.output_dir / f"{label}_derived.xml"
        self.assertTrue(derived_path.exists(), msg=f"Missing derived output: {derived_path}")
        parsed = parse_score(derived_path, verse_number=1)
        derived_part = self._find_derived_part(parsed)
        notes = derived_part.get("notes") or []
        sung = [note for note in notes if not note.get("is_rest")]
        self.assertTrue(sung, msg=f"{label}: derived part has no sung notes.")
        sung_measures = sorted(
            {
                int(note.get("measure_number") or 0)
                for note in sung
                if int(note.get("measure_number") or 0) > 0
            }
        )
        self.assertTrue(sung_measures, msg=f"{label}: no sung measure numbers detected.")
        self.assertEqual(
            sung_measures[0],
            min_sung_measure,
            msg=f"{label}: unexpected first sung measure.",
        )
        self.assertEqual(
            sung_measures[-1],
            max_sung_measure,
            msg=f"{label}: unexpected last sung measure.",
        )
        for start_measure, end_measure in lyric_presence_ranges:
            lyric_count = sum(
                1
                for note in sung
                if (
                    start_measure <= int(note.get("measure_number") or 0) <= end_measure
                    and isinstance(note.get("lyric"), str)
                    and bool(str(note.get("lyric")).strip())
                )
            )
            self.assertGreater(
                lyric_count,
                0,
                msg=(
                    f"{label}: expected lyrics in measures {start_measure}-{end_measure}, "
                    "but none were found."
                ),
            )

    def _find_viable_source_ref(
        self,
        *,
        score: dict,
        part_index: int,
        voice_part_id: str,
        candidates: list[dict],
    ) -> tuple[dict, str, bool] | None:
        strategies = ["overlap_best_match", "strict_onset", "syllable_flow"]
        for candidate in candidates:
            source_ref = {
                "part_index": candidate.get("source_part_index"),
                "voice_part_id": candidate.get("source_voice_part_id"),
            }
            for strategy in strategies:
                for copy_all in (False, True):
                    plan = self._build_plan(
                        part_index=part_index,
                        voice_part_id=voice_part_id,
                        source_ref=source_ref,
                        strategy=strategy,
                        copy_all_verses=copy_all,
                    )
                    result = preprocess_voice_parts(score, plan=plan)
                    if result.get("status") in {"ready", "ready_with_warnings"}:
                        return source_ref, strategy, copy_all
        return None

    def test_amazing_grace_all_voice_parts(self) -> None:
        self.skipTest("Skipping amazing-grace flow; only run my-tribute in this test file.")

    def test_my_tribute_all_voice_parts(self) -> None:
        tribute_path = self.root_dir / "assets/test_data/my-tribute.mxl/score.xml"
        if not tribute_path.exists():
            self.skipTest(f"Score not found at {tribute_path}")

        original_score_path = self.score_path
        try:
            self.score_path = tribute_path
            score_snapshot = self._load_full_score()
            snapshot_path = self.output_dir / "my_tribute_full_score.json"
            snapshot_path.write_text(
                json.dumps(score_snapshot, indent=2, sort_keys=True, default=str)
            )
            plan_bundle = self._load_plan_bundle_from_env()
            for target in plan_bundle.get("targets", []):
                if not isinstance(target, dict):
                    self.fail("Each entry in plan bundle targets must be an object.")
                label = str(target.get("label") or "").strip()
                if not label:
                    self.fail("Each plan target must include a non-empty 'label'.")
                part_index = int(target.get("part_index"))
                voice_part_id = str(target.get("voice_part_id"))
                plan = target.get("plan")
                if not isinstance(plan, dict):
                    self.fail(f"{label}: target.plan must be a JSON object.")
                self._run_flow(
                    label=label,
                    part_index=part_index,
                    voice_part_id=voice_part_id,
                    source_ref=None,
                    plan_override=plan,
                )
                expect = target.get("expect")
                if expect is None:
                    continue
                if not isinstance(expect, dict):
                    self.fail(f"{label}: target.expect must be a JSON object when provided.")
                ranges = [
                    (int(row[0]), int(row[1]))
                    for row in (expect.get("lyric_presence_ranges") or [])
                ]
                self._assert_derived_output(
                    label=label,
                    min_sung_measure=int(expect.get("min_sung_measure")),
                    max_sung_measure=int(expect.get("max_sung_measure")),
                    lyric_presence_ranges=ranges,
                )
        finally:
            self.score_path = original_score_path


if __name__ == "__main__":
    unittest.main()
