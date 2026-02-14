import json
import os
import shutil
import unittest
from pathlib import Path

from src.api import parse_score, save_audio, synthesize
from src.api.voice_parts import preprocess_voice_parts


class VoicePartEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.score_path = self.root_dir / "assets/test_data/amazing-grace-satb-verse1.xml"
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.output_dir = self.root_dir / "tests/output/voice_parts_e2e"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = "0"
        self.skip_synthesis = os.environ.get("VOICE_PART_E2E_SKIP_SYNTHESIS", "0").strip() in {
            "1",
            "true",
            "yes",
        }

    def _load_full_score(self) -> dict:
        score = parse_score(self.score_path, verse_number=1)
        score["source_musicxml_path"] = str(self.score_path.resolve())
        return score

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

        synth_result = synthesize(
            score,
            self.voicebank_path,
            part_index=part_index,
            voice_part_id=voice_part_id,
            allow_lyric_propagation=bool(source_ref),
            source_part_index=(source_ref or {}).get("part_index"),
            source_voice_part_id=(source_ref or {}).get("voice_part_id"),
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
            max_measure = max(
                int(note.get("measure_number") or 0)
                for part in score_snapshot.get("parts", [])
                for note in (part.get("notes") or [])
                if int(note.get("measure_number") or 0) > 0
            )
            snapshot_path = self.output_dir / "my_tribute_full_score.json"
            snapshot_path.write_text(
                json.dumps(score_snapshot, indent=2, sort_keys=True, default=str)
            )
            plans = [
                {
                    "label": "my_tribute_women_voice_part_1",
                    "part_index": 0,
                    "voice_part_id": "voice part 1",
                    "source_ref": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "plan": {
                        "targets": [
                            {
                                "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                                "sections": [
                                    {"start_measure": 1, "end_measure": 4, "mode": "rest"},
                                    {
                                        "start_measure": 5,
                                        "end_measure": 24,
                                        "mode": "derive",
                                        "melody_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 25,
                                        "end_measure": max_measure,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                ],
                                "verse_number": "1",
                                "copy_all_verses": False,
                                "split_shared_note_policy": "duplicate_to_all",
                                "confidence": 0.9,
                                "notes": "Timeline sections: women voice part 1",
                            }
                        ]
                    },
                },
                {
                    "label": "my_tribute_women_voice_part_2",
                    "part_index": 0,
                    "voice_part_id": "voice part 2",
                    "source_ref": {"part_index": 0, "voice_part_id": "voice part 2"},
                    "plan": {
                        "targets": [
                            {
                                "target": {"part_index": 0, "voice_part_id": "voice part 2"},
                                "sections": [
                                    {"start_measure": 1, "end_measure": 4, "mode": "rest"},
                                    {
                                        "start_measure": 5,
                                        "end_measure": 24,
                                        "mode": "derive",
                                        "melody_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 25,
                                        "end_measure": 30,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 31,
                                        "end_measure": 36,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 37,
                                        "end_measure": 41,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 1, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 42,
                                        "end_measure": 49,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 50,
                                        "end_measure": 51,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 52,
                                        "end_measure": max_measure,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                ],
                                "verse_number": "1",
                                "copy_all_verses": False,
                                "split_shared_note_policy": "duplicate_to_all",
                                "confidence": 0.9,
                                "notes": "Timeline sections: women voice part 2",
                            }
                        ]
                    },
                },
                {
                    "label": "my_tribute_men_voice_part_1",
                    "part_index": 1,
                    "voice_part_id": "voice part 1",
                    "source_ref": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "plan": {
                        "targets": [
                            {
                                "target": {"part_index": 1, "voice_part_id": "voice part 1"},
                                "sections": [
                                    {"start_measure": 1, "end_measure": 18, "mode": "rest"},
                                    {
                                        "start_measure": 19,
                                        "end_measure": 24,
                                        "mode": "derive",
                                        "melody_source": {"part_index": 1, "voice_part_id": "voice part 2"},
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 25,
                                        "end_measure": 30,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 31,
                                        "end_measure": 36,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 37,
                                        "end_measure": 41,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 1, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 42,
                                        "end_measure": 49,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 50,
                                        "end_measure": 51,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 52,
                                        "end_measure": max_measure,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                ],
                                "verse_number": "1",
                                "copy_all_verses": False,
                                "split_shared_note_policy": "duplicate_to_all",
                                "confidence": 0.7,
                                "notes": "Timeline sections: men voice part 1",
                            }
                        ]
                    },
                },
                {
                    "label": "my_tribute_men_voice_part_3",
                    "part_index": 1,
                    "voice_part_id": "voice part 3",
                    "source_ref": {"part_index": 0, "voice_part_id": "voice part 3"},
                    "plan": {
                        "targets": [
                            {
                                "target": {"part_index": 1, "voice_part_id": "voice part 3"},
                                "sections": [
                                    {"start_measure": 1, "end_measure": 18, "mode": "rest"},
                                    {
                                        "start_measure": 19,
                                        "end_measure": 24,
                                        "mode": "derive",
                                        "melody_source": {"part_index": 1, "voice_part_id": "voice part 2"},
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 3"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 25,
                                        "end_measure": 30,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 31,
                                        "end_measure": 36,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 37,
                                        "end_measure": 41,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 1, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 42,
                                        "end_measure": 49,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 50,
                                        "end_measure": 51,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 2"},
                                        "lyric_strategy": "strict_onset",
                                        "lyric_policy": "replace_all",
                                    },
                                    {
                                        "start_measure": 52,
                                        "end_measure": max_measure,
                                        "mode": "derive",
                                        "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                                        "lyric_strategy": "overlap_best_match",
                                        "lyric_policy": "replace_all",
                                    },
                                ],
                                "verse_number": "1",
                                "copy_all_verses": False,
                                "split_shared_note_policy": "duplicate_to_all",
                                "confidence": 0.7,
                                "notes": "Timeline sections: men voice part 3",
                            }
                        ]
                    },
                },
            ]

            for entry in plans:
                self._run_flow(
                    label=entry["label"],
                    part_index=entry["part_index"],
                    voice_part_id=entry["voice_part_id"],
                    source_ref=entry.get("source_ref"),
                    plan_override=entry["plan"],
                )
            self._assert_derived_output(
                label="my_tribute_women_voice_part_1",
                min_sung_measure=5,
                max_sung_measure=54,
                lyric_presence_ranges=[(5, 24), (25, max_measure)],
            )
            self._assert_derived_output(
                label="my_tribute_women_voice_part_2",
                min_sung_measure=5,
                max_sung_measure=max_measure,
                lyric_presence_ranges=[(5, 24), (25, max_measure)],
            )
            self._assert_derived_output(
                label="my_tribute_men_voice_part_1",
                min_sung_measure=19,
                max_sung_measure=54,
                lyric_presence_ranges=[(19, 24), (25, 35), (42, max_measure)],
            )
            self._assert_derived_output(
                label="my_tribute_men_voice_part_3",
                min_sung_measure=19,
                max_sung_measure=max_measure,
                lyric_presence_ranges=[(19, 24), (25, 35), (42, max_measure)],
            )
        finally:
            self.score_path = original_score_path


if __name__ == "__main__":
    unittest.main()
