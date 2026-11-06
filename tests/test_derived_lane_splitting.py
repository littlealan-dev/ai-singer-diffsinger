from __future__ import annotations

import pytest

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



def _split_pitches(high: dict, low: dict) -> list[list[float]]:
    result = preprocess_voice_parts(_mixed_chord_and_unison_score(), plan={"targets": [high, low]})
    assert result["status"] == "ready"
    return [
        [note["pitch_midi"] for note in part["notes"] if not note.get("is_rest")]
        for part in result["score"]["parts"][1:]
    ]


def test_section_shared_note_policy_overrides_the_target_policy() -> None:
    # Beat 2 is a unison (65). A section override decides who sings it, whatever the fallback.
    high, low = _target(0), _target(1)
    for section in (high["sections"][0], low["sections"][0]):
        section["rank_fallback"] = "greedy"
        section["split_shared_note_policy"] = "assign_primary_only"
    assert _split_pitches(high, low) == [[72.0, 65.0, 70.0], [60.0, 58.0]]

    high, low = _target(0), _target(1)
    for target in (high, low):
        target["split_shared_note_policy"] = "assign_primary_only"
        target["sections"][0]["split_shared_note_policy"] = "duplicate_to_all"
    assert _split_pitches(high, low) == [[72.0, 65.0, 70.0], [60.0, 65.0, 58.0]]


def test_assign_primary_only_rests_lower_lanes_at_unisons_even_with_greedy() -> None:
    high, low = _target(0), _target(1)
    for target in (high, low):
        target["split_shared_note_policy"] = "assign_primary_only"
        target["sections"][0]["rank_fallback"] = "greedy"
    assert _split_pitches(high, low) == [[72.0, 65.0, 70.0], [60.0, 58.0]]


def test_section_shared_note_policy_is_validated() -> None:
    low = _target(1)
    low["sections"][0]["split_shared_note_policy"] = "every_lane"
    result = preprocess_voice_parts(_mixed_chord_and_unison_score(), plan={"targets": [_target(0), low]})
    assert result["action"] == "invalid_plan_enum", result

    low = _target(1)
    low["sections"][0].update({
        "decision_type": "EXTRACT_FROM_VOICE", "method": "trivial",
        "split_shared_note_policy": "assign_primary_only",
    })
    result = preprocess_voice_parts(_mixed_chord_and_unison_score(), plan={"targets": [_target(0), low]})
    assert result["action"] == "invalid_plan_payload", result

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

# Synthetic one-staff MusicXML cases for complete-split lane bounds.
def _lane_bounds_score(tmp_path, kind: str) -> dict:
    from src.api import parse_score

    examples = {
        "turns": [((("C", 5),), ()), ((("D", 5),), ()), ((), (("G", 4),)), ((), (("A", 4),))],
        "chord_turns": [((("C", 5), ("E", 5)), ()), ((("D", 5), ("F", 5)), ()), ((), (("G", 4), ("B", 4))), ((), (("A", 4), ("C", 5)))],
        "mixed": [((("C", 5),), (("G", 4),)), ((("E", 5), ("G", 5)), ()), ((("F", 5),), (("A", 4),))],
        "solo": [((("C", 5),),), ((("D", 5),),)],
        "solo_chord": [((("E", 5), ("G", 5)),), ((("F", 5),),)],
    }
    bars = examples[kind]
    xml_bars = []
    for number, pitches in enumerate(bars, 1):
        voices = pitches
        notes = []
        for voice_number, voice_pitches in enumerate(voices, 1):
            if voice_number > 1:
                notes.append("<backup><duration>4</duration></backup>")
            sounding = [pitch for pitch in voice_pitches if pitch is not None]
            if not sounding:
                notes.append(f'<note print-object="no"><rest/><duration>4</duration><voice>{voice_number}</voice><type>whole</type></note>')
            for chord_index, (step, octave) in enumerate(sounding):
                notes.append(
                    "<note>" + ("<chord/>" if chord_index else "")
                    + f"<pitch><step>{step}</step><octave>{octave}</octave></pitch>"
                    + f"<duration>4</duration><voice>{voice_number}</voice><type>whole</type>"
                    + ('<lyric number="1"><syllabic>single</syllabic><text>nava</text></lyric>' if chord_index == 0 else "")
                    + "</note>"
                )
        attributes = (
            "<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>"
            "<clef><sign>G</sign><line>2</line></clef></attributes>"
            if number == 1 else ""
        )
        xml_bars.append(f'<measure number="{number}">{attributes}{"".join(notes)}</measure>')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1">'
        '<part-list><score-part id="P1"><part-name>Synthetic</part-name></score-part></part-list>'
        f'<part id="P1">{"".join(xml_bars)}</part></score-partwise>'
    )
    path = tmp_path / f"{kind}.xml"
    path.write_text(xml, encoding="utf-8")
    return parse_score(path, verse_number=1)


def _bounds_lane(source_voice: str, steps: list, *, complete: bool = True) -> dict:
    source = {"part_index": 0, "voice_part_id": f"voice part {source_voice}"}
    sections = []
    for measure, step in enumerate(steps, 1):
        section = {"start_measure": measure, "end_measure": measure}
        if step is None:
            section["mode"] = "rest"
        else:
            voice, rank, decision = step
            selected = {"part_index": 0, "voice_part_id": f"voice part {voice}"}
            section.update({
                "mode": "derive",
                "decision_type": decision,
                "melody_source": selected,
                "lyric_source": selected,
                "lyric_strategy": "strict_onset",
                "lyric_policy": "replace_all",
            })
            if rank is not None:
                section.update({"method": "ranked", "rank_index": rank, "rank_fallback": "skip"})
        sections.append(section)
    return {
        "source": source,
        "output": {"mode": "append_new_derived_lane"},
        "split_coverage": "complete" if complete else "selective",
        "sections": sections,
        "split_shared_note_policy": "assign_primary_only",
    }


def _bounds_plan(kind: str, case: int) -> dict:
    extract = "EXTRACT_FROM_VOICE"
    rank = "SPLIT_CHORDS_SELECT_NOTES"
    if kind == "turns":
        first = _bounds_lane("1", [("1", None, extract)] * 2 + [None] * 2)
        second = _bounds_lane("2", [None] * 2 + [("2", None, extract)] * 2)
        if case == 1:
            lanes = [first, second]
        elif case == 6:
            lanes = [first, second, first.copy()]
        else:
            lanes = [_bounds_lane("1", [("1", None, extract)] * 2 + [("2", None, extract)] * 2)]
    elif kind == "mixed":
        high = _bounds_lane("1", [("1", 0, rank)] * 3)
        low = _bounds_lane("1", [("1", 1, rank)] * 3)
        other = _bounds_lane("2", [("2", None, extract), None, ("2", None, extract)])
        if case == 2:
            lanes = [high, low, other]
        else:
            lanes = [high, _bounds_lane("2", [("2", None, extract), ("1", 1, rank), ("2", None, extract)])]
    elif kind == "chord_turns":
        if case == 4:
            lanes = [
                _bounds_lane(voice, [(voice, rank_index, rank) if (measure < 2) == (voice == "1") else None for measure in range(4)])
                for voice in ("1", "2") for rank_index in (0, 1)
            ]
        else:
            lanes = [
                _bounds_lane("1" if rank_index == 0 else "2", [("1" if measure < 2 else "2", rank_index, rank) for measure in range(4)])
                for rank_index in (0, 1)
            ]
    elif kind == "solo":
        first = _bounds_lane("1", [("1", 0 if case == 10 else None, rank if case == 10 else extract)] * 2)
        lanes = [first, _bounds_lane("1", [("1", 1 if case == 10 else None, rank if case == 10 else extract)] * 2)]
    else:
        first = _bounds_lane("1", [("1", 0, rank), ("1", 0, rank)])
        second = _bounds_lane("1", [("1", 0 if case == 12 else 1, rank), None])
        if case == 11:
            lanes = [first]
        elif case == 14:
            lanes = [_bounds_lane("1", [("1", 0, rank), ("1", 0, rank)], complete=False)]
        elif case == 8:
            lanes = [first, second, _bounds_lane("1", [("1", None, extract)] * 2)]
        elif case == 9:
            lanes = [first, second, _bounds_lane("1", [("1", 2, rank), None])]
        else:
            lanes = [first, second]
    return {"targets": lanes}


@pytest.mark.parametrize(
    "case,kind,status,note_counts",
    [
        (1, "turns", "ready", [2, 2]),
        (2, "mixed", "ready", [3, 1, 2]),
        (3, "mixed", "ready", [3, 2]),
        (4, "chord_turns", "ready", [2, 2, 2, 2]),
        (5, "chord_turns", "ready", [2, 2]),
        (6, "turns", "action_required", None),
        (7, "solo", "action_required", None),
        (8, "solo_chord", "action_required", None),
        (9, "solo_chord", "action_required", None),
        (10, "solo", "action_required", None),
        (11, "solo_chord", "action_required", None),
        (12, "solo_chord", "action_required", None),
        (13, "turns", "action_required", None),
        (14, "solo_chord", "ready", [2]),
    ],
)
def test_complete_split_staff_lane_bounds(tmp_path, case, kind, status, note_counts) -> None:
    score = _lane_bounds_score(tmp_path, kind)
    plan = _bounds_plan(kind, case)
    result = preprocess_voice_parts(score, plan=plan)
    assert result["status"] == status, result
    findings = {finding["rule"]: finding for finding in result.get("lint_findings", [])}
    if status == "action_required":
        if case in (6, 7, 8, 9, 10, 11):
            assert "complete_split_scope_lane_count_mismatch" in findings
        elif case == 12:
            assert "complete_split_duplicate_rank" in findings
            assert "complete_split_scope_lane_count_mismatch" not in findings
        elif case == 13:
            assert "same_part_target_completeness" in findings
    else:
        derived = result["score"]["parts"][1:]
        sounding = [[n for n in part["notes"] if not n.get("is_rest")] for part in derived]
        assert [len(notes) for notes in sounding] == note_counts
        if case in (1, 4):
            source_notes = score["parts"][0]["notes"]
            for part_notes, target in zip(sounding, plan["targets"]):
                voice = target["source"]["voice_part_id"].split()[-1]
                assert all(n.get("voice") == voice for n in part_notes)
                assert len({(n["measure_number"], n["offset_beats"]) for n in part_notes}) == len(part_notes)
                assert all(any(n["pitch_midi"] == src["pitch_midi"] and n["measure_number"] == src["measure_number"] and str(src.get("voice")) == voice for src in source_notes) for n in part_notes)
        if case == 2:
            from collections import Counter
            from src.api import parse_score
            original = Counter((n["measure_number"], n["pitch_midi"]) for n in score["parts"][0]["notes"] if not n.get("is_rest"))
            output = Counter((n["measure_number"], n["pitch_midi"]) for notes in sounding for n in notes)
            assert output == original
            reparsed = parse_score(result["modified_musicxml_path"], verse_number=1)
            written = Counter((n["measure_number"], n["pitch_midi"]) for part in reparsed["parts"][1:] for n in part["notes"] if not n.get("is_rest"))
            assert written == original
    if case == 6:
        attributes = findings["complete_split_scope_lane_count_mismatch"]["failing_attributes"]
        assert (attributes["minimum_lane_count"], attributes["maximum_lane_count"], attributes["visible_derived_lane_count"]) == (1, 2, 3)
    if case == 11:
        assert "complete_split_source_underclaimed" in findings
    if case == 14:
        assert "complete_split_scope_lane_count_mismatch" not in findings


def test_sheet_lane_counts_excludes_default_voice_from_upper_bound(tmp_path) -> None:
    from dataclasses import FrozenInstanceError
    from src.api.voice_parts import sheet_lane_counts

    turns = _lane_bounds_score(tmp_path, "turns")
    counts = sheet_lane_counts(turns, part_index=0, staff_scope="1")
    assert (counts.peak, counts.real_voices, counts.most) == (1, 2, 2)
    turns["parts"][0]["notes"].append({
        "measure_number": 5,
        "offset_beats": 0.0,
        "duration_beats": 4.0,
        "pitch_midi": 60.0,
        "voice": None,
        "staff": 1,
        "is_rest": False,
    })
    assert sheet_lane_counts(turns, part_index=0, staff_scope="1") == counts
    with pytest.raises(FrozenInstanceError):
        counts.most = 3

    mixed = _lane_bounds_score(tmp_path, "mixed")
    assert sheet_lane_counts(mixed, part_index=0, staff_scope="1").most == 3
