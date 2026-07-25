from __future__ import annotations

from src.api.voice_parts import analyze_score_voice_parts, preprocess_voice_parts


def _same_voice_chord_score() -> dict:
    return {
        "parts": [
            {
                "part_id": "Soprano",
                "part_name": "Soprano",
                "notes": [
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 72.0,
                        "voice": "1",
                        "is_rest": False,
                        "lyric": "la",
                        "lyric_is_extended": False,
                    },
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 60.0,
                        "voice": "1",
                        "is_rest": False,
                        "lyric": "la",
                        "lyric_is_extended": False,
                    },
                ],
            }
        ]
    }


def _mixed_chord_and_unison_score() -> dict:
    score = _same_voice_chord_score()
    score["parts"][0]["notes"].extend(
        [
            {
                "measure_number": 1,
                "offset_beats": 1.0,
                "duration_beats": 1.0,
                "pitch_midi": 65.0,
                "voice": "1",
                "is_rest": False,
                "lyric": "la",
                "lyric_is_extended": False,
            },
            {
                "measure_number": 1,
                "offset_beats": 2.0,
                "duration_beats": 1.0,
                "pitch_midi": 70.0,
                "voice": "1",
                "is_rest": False,
                "lyric": "la",
                "lyric_is_extended": False,
            },
            {
                "measure_number": 1,
                "offset_beats": 2.0,
                "duration_beats": 1.0,
                "pitch_midi": 58.0,
                "voice": "1",
                "is_rest": False,
                "lyric": "la",
                "lyric_is_extended": False,
            },
        ]
    )
    return score


def _target(rank_index: int) -> dict:
    return {
        "source": {"part_index": 0, "voice_part_id": "voice part 1"},
        "output": {"mode": "append_new_derived_lane"},
        "split_coverage": "complete",
        "sections": [
            {
                "start_measure": 1,
                "end_measure": 1,
                "mode": "derive",
                "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                "method": "ranked",
                "rank_index": rank_index,
                "rank_fallback": "skip",
                "melody_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                "lyric_strategy": "strict_onset",
                "lyric_policy": "replace_all",
            }
        ],
    }


def test_source_analysis_reports_same_voice_chord_density_as_facts() -> None:
    analysis = analyze_score_voice_parts(_same_voice_chord_score())
    density = analysis["parts"][0]["part_region_indices"]["source_voice_chord_density"][
        "voice part 1"
    ]

    assert density["has_same_voice_simultaneous_notes"] is True
    assert density["max_simultaneous_notes_by_measure"] == {"1": 2}
    assert density["chord_ranges"] == [{"start": 1, "end": 1}]
    scope_density = analysis["parts"][0]["part_region_indices"][
        "staff_scope_simultaneous_density"
    ]
    assert scope_density["1"]["max_simultaneous_notes"] == 2


def test_complete_split_requires_all_same_source_chord_ranks() -> None:
    result = preprocess_voice_parts(_same_voice_chord_score(), plan={"targets": [_target(0)]})

    assert result["status"] == "action_required"
    assert result["action"] == "plan_lint_failed"
    assert result["phase"] == "preprocess_repair_planning"
    assert result["repair_scope"]["require_complete_full_song_plan"] is True
    assert any(
        finding["rule"] == "complete_split_source_underclaimed"
        for finding in result["lint_findings"]
    )


def test_complete_split_rejects_duplicate_chord_rank_selection() -> None:
    result = preprocess_voice_parts(
        _same_voice_chord_score(),
        plan={"targets": [_target(0), _target(0)]},
    )

    assert result["status"] == "action_required"
    assert any(
        finding["rule"] == "complete_split_duplicate_rank"
        for finding in result["lint_findings"]
    )


def test_complete_split_materializes_distinct_lanes_for_one_source_voice() -> None:
    result = preprocess_voice_parts(
        _same_voice_chord_score(),
        plan={"targets": [_target(0), _target(1)]},
    )

    assert result["status"] == "ready"
    targets = result["targets"]
    assert [target["derived_lane"]["slot"] for target in targets] == [1, 2]
    assert len({target["appended_part_ref"]["part_id"] for target in targets}) == 2
    assert [target["appended_part_ref"]["part_name"] for target in targets] == [
        "Soprano - voice part 1 (Derived)",
        "Soprano - voice part 1 - split 2 (Derived)",
    ]
    assert [target["derived_lane"]["display_name"] for target in targets] == [
        "Soprano - voice part 1 (Derived)",
        "Soprano - voice part 1 - split 2 (Derived)",
    ]
    derived = result["score"]["parts"][1:]
    assert [[note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")] for part in derived] == [
        [72.0],
        [60.0],
    ]


def test_different_source_lanes_keep_their_source_voice_names() -> None:
    score = {
        "parts": [
            {
                "part_id": "Women",
                "part_name": "Women",
                "notes": [
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 72.0,
                        "voice": "1",
                        "is_rest": False,
                        "lyric": "la",
                    },
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 60.0,
                        "voice": "2",
                        "is_rest": False,
                        "lyric": "la",
                    },
                ],
            }
        ]
    }

    def target(voice_part_id: str) -> dict:
        source = {"part_id": "Women", "voice_part_id": voice_part_id}
        return {
            "source": source,
            "output": {"mode": "append_new_derived_lane"},
            "split_coverage": "complete",
            "sections": [
                {
                    "start_measure": 1,
                    "end_measure": 1,
                    "mode": "derive",
                    "decision_type": "EXTRACT_FROM_VOICE",
                    "melody_source": source,
                    "lyric_source": source,
                    "lyric_strategy": "strict_onset",
                    "lyric_policy": "replace_all",
                }
            ],
        }

    result = preprocess_voice_parts(
        score,
        plan={"targets": [target("voice part 1"), target("voice part 2")]},
    )

    assert result["status"] == "ready"
    assert [target["appended_part_ref"]["part_name"] for target in result["targets"]] == [
        "Women - voice part 1 (Derived)",
        "Women - voice part 2 (Derived)",
    ]


def test_duplicate_source_labels_are_qualified_with_the_visible_part_id() -> None:
    score = {
        "parts": [
            {
                "part_id": "Women-Staff-1",
                "part_name": "Women",
                "notes": [
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 72.0,
                        "voice": "1",
                        "is_rest": False,
                        "lyric": "la",
                    }
                ],
            },
            {
                "part_id": "Women-Staff-2",
                "part_name": "Women",
                "notes": [
                    {
                        "measure_number": 1,
                        "offset_beats": 0.0,
                        "duration_beats": 1.0,
                        "pitch_midi": 60.0,
                        "voice": "1",
                        "is_rest": False,
                        "lyric": "la",
                    }
                ],
            },
        ]
    }

    def target(part_id: str) -> dict:
        source = {"part_id": part_id, "voice_part_id": "voice part 1"}
        return {
            "source": source,
            "output": {"mode": "append_new_derived_lane"},
            "split_coverage": "selective",
            "sections": [
                {
                    "start_measure": 1,
                    "end_measure": 1,
                    "mode": "derive",
                    "decision_type": "EXTRACT_FROM_VOICE",
                    "melody_source": source,
                    "lyric_source": source,
                    "lyric_strategy": "strict_onset",
                    "lyric_policy": "replace_all",
                }
            ],
        }

    result = preprocess_voice_parts(
        score,
        plan={"targets": [target("Women-Staff-1"), target("Women-Staff-2")]},
    )

    assert result["status"] == "ready"
    assert [target["appended_part_ref"]["part_name"] for target in result["targets"]] == [
        "Women [Women-Staff-1] - voice part 1 (Derived)",
        "Women [Women-Staff-2] - voice part 1 (Derived)",
    ]


def test_complete_split_duplicates_unison_onsets_in_mixed_chord_sections() -> None:
    result = preprocess_voice_parts(
        _mixed_chord_and_unison_score(),
        plan={"targets": [_target(0), _target(1)]},
    )

    assert result["status"] == "ready"
    assert [
        [note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")]
        for part in result["score"]["parts"][1:]
    ] == [[72.0, 65.0, 70.0], [60.0, 65.0, 58.0]]


def test_assign_primary_only_omits_unison_onsets_from_secondary_lane() -> None:
    high = _target(0)
    low = _target(1)
    high["split_shared_note_policy"] = "assign_primary_only"
    low["split_shared_note_policy"] = "assign_primary_only"

    result = preprocess_voice_parts(
        _mixed_chord_and_unison_score(),
        plan={"targets": [high, low]},
    )

    assert result["status"] == "ready"
    assert [
        [note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")]
        for part in result["score"]["parts"][1:]
    ] == [[72.0, 65.0, 70.0], [60.0, 58.0]]


def test_hidden_lane_cleanup_does_not_remove_another_parts_derived_lane() -> None:
    score = _same_voice_chord_score()
    score["parts"].append(
        {
            "part_id": "Piano",
            "part_name": "Piano",
            "notes": [
                {
                    "measure_number": 1,
                    "offset_beats": 0.0,
                    "duration_beats": 1.0,
                    "pitch_midi": 72.0,
                    "voice": "1",
                    "is_rest": False,
                },
                {
                    "measure_number": 1,
                    "offset_beats": 0.0,
                    "duration_beats": 1.0,
                    "pitch_midi": 60.0,
                    "voice": "",
                    "is_rest": False,
                },
            ],
        }
    )

    result = preprocess_voice_parts(score, plan={"targets": [_target(0), _target(1)]})

    assert result["status"] == "ready"
    assert [target["appended_part_ref"]["part_name"] for target in result["targets"]] == [
        "Soprano - voice part 1 (Derived)",
        "Soprano - voice part 1 - split 2 (Derived)",
    ]
    assert [part["part_name"] for part in result["score"]["parts"]][-2:] == [
        "Soprano - voice part 1 (Derived)",
        "Soprano - voice part 1 - split 2 (Derived)",
    ]


def test_later_append_from_the_same_source_keeps_the_existing_derived_lane() -> None:
    first_target = _target(0)
    first_target["split_coverage"] = "selective"
    first = preprocess_voice_parts(_same_voice_chord_score(), plan={"targets": [first_target]})

    second_target = _target(1)
    second_target["split_coverage"] = "selective"
    second = preprocess_voice_parts(first["score"], plan={"targets": [second_target]})

    assert first["status"] == "ready"
    assert second["status"] == "ready"
    assert first["targets"][0]["derived_lane"]["slot"] == 1
    assert second["targets"][0]["derived_lane"]["slot"] == 2
    assert [target["appended_part_ref"]["part_name"] for target in (first["targets"] + second["targets"])] == [
        "Soprano - voice part 1 (Derived)",
        "Soprano - voice part 1 - split 2 (Derived)",
    ]
    assert [
        [note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")]
        for part in second["score"]["parts"][1:]
    ] == [[72.0], [60.0]]


def test_one_plan_handles_native_siblings_and_same_source_chords() -> None:
    score = _same_voice_chord_score()
    score["parts"][0]["part_name"] = "Soprano Alto"
    score["parts"][0]["notes"].append(
        {
            "measure_number": 1,
            "offset_beats": 0.0,
            "duration_beats": 1.0,
            "pitch_midi": 50.0,
            "voice": "2",
            "is_rest": False,
            "lyric": "la",
            "lyric_is_extended": False,
        }
    )
    alto_target = _target(0)
    alto_target["source"] = {"part_index": 0, "voice_part_id": "alto"}
    alto_target["split_coverage"] = "complete"
    alto_target["sections"][0].update(
        {
            "decision_type": "EXTRACT_FROM_VOICE",
            "melody_source": {"part_index": 0, "voice_part_id": "alto"},
            "lyric_source": {"part_index": 0, "voice_part_id": "alto"},
        }
    )
    alto_target["sections"][0].pop("method")
    alto_target["sections"][0].pop("rank_index")
    alto_target["sections"][0].pop("rank_fallback")
    soprano_high = _target(0)
    soprano_low = _target(1)
    for target in (soprano_high, soprano_low):
        target["source"] = {"part_index": 0, "voice_part_id": "soprano"}
        target["sections"][0]["melody_source"] = {
            "part_index": 0,
            "voice_part_id": "soprano",
        }
        target["sections"][0]["lyric_source"] = {
            "part_index": 0,
            "voice_part_id": "soprano",
        }

    result = preprocess_voice_parts(
        score,
        plan={
            "targets": [
                soprano_high,
                soprano_low,
                alto_target,
            ]
        },
    )

    assert result["status"] == "ready"
    targets = result["targets"]
    assert len(targets) == 3
    assert len({target["derived_lane"]["derived_lane_id"] for target in targets}) == 3
    assert len({target["appended_part_ref"]["part_id"] for target in targets}) == 3
    assert len({target["appended_part_ref"]["part_name"] for target in targets}) == 3
    assert [target["appended_part_ref"]["part_name"] for target in targets] == [
        "Soprano Alto - soprano (Derived)",
        "Soprano Alto - soprano - split 2 (Derived)",
        "Soprano Alto - alto (Derived)",
    ]
    assert [
        [note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")]
        for part in result["score"]["parts"][1:]
    ] == [[72.0], [60.0], [50.0]]


def test_complete_staff_scope_requires_lane_count_at_peak_density() -> None:
    score = _same_voice_chord_score()
    score["parts"][0]["part_name"] = "Soprano Alto"
    score["parts"][0]["notes"].append(
        {
            "measure_number": 1,
            "offset_beats": 0.0,
            "duration_beats": 1.0,
            "pitch_midi": 50.0,
            "voice": "2",
            "is_rest": False,
            "lyric": "la",
            "lyric_is_extended": False,
        }
    )
    soprano_high = _target(0)
    soprano_low = _target(1)
    for target in (soprano_high, soprano_low):
        target["source"] = {"part_index": 0, "voice_part_id": "soprano"}
        target["sections"][0]["melody_source"] = {
            "part_index": 0,
            "voice_part_id": "soprano",
        }
        target["sections"][0]["lyric_source"] = {
            "part_index": 0,
            "voice_part_id": "soprano",
        }

    result = preprocess_voice_parts(score, plan={"targets": [soprano_high, soprano_low]})

    assert result["status"] == "action_required"
    assert any(
        finding["rule"] == "complete_split_scope_lane_count_mismatch"
        for finding in result["lint_findings"]
    )
