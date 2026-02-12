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
        self.assertIn(preprocess_result.get("status"), {"ready", "ready_with_warnings"})
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
        self.assertTrue(musicxml_path.exists())

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
                                "actions": [
                                    {
                                        "type": "split_voice_part",
                                        "split_shared_note_policy": "duplicate_to_all",
                                    },
                                    {
                                        "type": "propagate_lyrics",
                                        "verse_number": "1",
                                        "copy_all_verses": False,
                                        "source_priority": [
                                            {
                                                "part_index": 0,
                                                "voice_part_id": "voice part 1",
                                            }
                                        ],
                                        "strategy": "strict_onset",
                                        "policy": "fill_missing_only",
                                        "section_overrides": [
                                            {
                                                "start_measure": 1,
                                                "end_measure": 24,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 3",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            },
                                            {
                                                "start_measure": 25,
                                                "end_measure": 29,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 2",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            }
                                        ],
                                    },
                                ],
                                "confidence": 0.9,
                                "notes": "Women voice part 1 with lyric overrides for m25-29.",
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
                                "actions": [
                                    {
                                        "type": "split_voice_part",
                                        "split_shared_note_policy": "duplicate_to_all",
                                    },
                                    {
                                        "type": "propagate_lyrics",
                                        "verse_number": "1",
                                        "copy_all_verses": False,
                                        "source_priority": [
                                            {
                                                "part_index": 0,
                                                "voice_part_id": "voice part 2",
                                            }
                                        ],
                                        "strategy": "strict_onset",
                                        "policy": "fill_missing_only",
                                        "section_overrides": [
                                            {
                                                "start_measure": 5,
                                                "end_measure": 24,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            },
                                            {
                                                "start_measure": 31,
                                                "end_measure": 36,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            },
                                            {
                                                "start_measure": 45,
                                                "end_measure": 48,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            },
                                            {
                                                "start_measure": 52,
                                                "end_measure": 54,
                                                "source": {
                                                    "part_index": 0,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            },
                                        ],
                                    },
                                ],
                                "confidence": 0.9,
                                "notes": "Women voice part 2 uses voice part 2 except P1 overrides.",
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
                                "actions": [
                                    {
                                        "type": "split_voice_part",
                                        "split_shared_note_policy": "duplicate_to_all",
                                    },
                                    {
                                        "type": "propagate_lyrics",
                                        "verse_number": "1",
                                        "copy_all_verses": False,
                                        "source_priority": [
                                            {
                                                "part_index": 0,
                                                "voice_part_id": "voice part 1",
                                            }
                                        ],
                                        "strategy": "overlap_best_match",
                                        "policy": "fill_missing_only",
                                        "section_overrides": [
                                            {
                                                "start_measure": 36,
                                                "end_measure": 41,
                                                "source": {
                                                    "part_index": 1,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            }
                                        ],
                                    },
                                ],
                                "confidence": 0.7,
                                "notes": "Men voice part 1 uses P1 lyrics except m36-41.",
                            }
                        ]
                    },
                },
                {
                    "label": "my_tribute_men_voice_part_2",
                    "part_index": 1,
                    "voice_part_id": "voice part 2",
                    "source_ref": {"part_index": 0, "voice_part_id": "voice part 3"},
                    "plan": {
                        "targets": [
                            {
                                "target": {"part_index": 1, "voice_part_id": "voice part 2"},
                                "actions": [
                                    {
                                        "type": "split_voice_part",
                                        "split_shared_note_policy": "duplicate_to_all",
                                    },
                                    {
                                        "type": "propagate_lyrics",
                                        "verse_number": "1",
                                        "copy_all_verses": False,
                                        "source_priority": [
                                            {
                                                "part_index": 0,
                                                "voice_part_id": "voice part 3",
                                            }
                                        ],
                                        "strategy": "overlap_best_match",
                                        "policy": "fill_missing_only",
                                        "section_overrides": [
                                            {
                                                "start_measure": 36,
                                                "end_measure": 41,
                                                "source": {
                                                    "part_index": 1,
                                                    "voice_part_id": "voice part 1",
                                                },
                                                "strategy": "strict_onset",
                                                "policy": "fill_missing_only",
                                            }
                                        ],
                                    },
                                ],
                                "confidence": 0.7,
                                "notes": "Men voice part 2 uses P1 lyrics except m36-41.",
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
        finally:
            self.score_path = original_score_path


if __name__ == "__main__":
    unittest.main()
