"""Deterministic Sing All row discovery on a made-up two-bar score."""

from copy import deepcopy
from pathlib import Path
import shutil

from src.api.preprocess_assembler import assemble_plan, first_option_choices
from src.api.preprocess_sheet import build_sheet
from src.api.score import parse_score
from src.api.sing_all_estimate import quote_facts
from src.api.voice_parts import LaneCounts, preprocess_voice_parts


_SCORE = Path(__file__).parent / "fixtures" / "sa_chord_gaps_two_bars.xml"
_SATB = Path(__file__).parent / "fixtures" / "split" / "satb_two_bars.xml"


def _score():
    return parse_score(_SCORE)


def _facts(score, *, verse=None):
    return quote_facts(score, score["score_summary"], [], verse_number=verse)


def test_split_rows_use_planner_lanes_and_estimate_ranges(capsys, tmp_path) -> None:
    source = tmp_path / "made-up-choir.xml"
    shutil.copyfile(_SCORE, source)
    score = parse_score(source)
    facts = _facts(score)
    assert len(facts["rows"]) == 2
    assert facts["open_staffs"] == []
    assert all(row["split"] and row["partId"] is None for row in facts["rows"])
    assert len({row["rowId"] for row in facts["rows"]}) == 2
    assert [row["name"] for row in facts["rows"]] == ["Soprano", "Alto"]
    assert all(":staff1:voice part 1:slot" in row["rowId"] for row in facts["rows"])
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    plan = assemble_plan(sheet, first_option_choices(sheet), score=score)
    result = preprocess_voice_parts(score, request={"plan": plan})
    assert result["status"] == "ready"
    split_score = parse_score(result["modified_musicxml_path"])
    actual_parts = split_score["score_summary"]["parts"][1:]
    assert len(actual_parts) == len(facts["rows"])
    for row, actual_part in zip(facts["rows"], actual_parts):
        estimated = row["pitchRange"]
        real = actual_part["pitch_range"]
        delta_low = estimated["lowest_midi"] - real["lowest_midi"]
        delta_high = estimated["highest_midi"] - real["highest_midi"]
        print(f"{row['rowId']}: estimate {estimated.get('lowest_note')}..{estimated.get('highest_note')}; "
              f"real {real['lowest_note']}..{real['highest_note']}; "
              f"delta low {delta_low:+g}, high {delta_high:+g} semitones")
        assert estimated["lowest_midi"] <= estimated["highest_midi"]
    assert "real" in capsys.readouterr().out


def test_quote_lane_ids_are_the_planners_on_a_real_two_staff_split(tmp_path) -> None:
    """Both staves of one choir part: the quote's laneId per row is the derived_lane_id the
    planner saves when it really splits them, so the runner can match on it."""
    source = tmp_path / "made-up-satb.xml"
    shutil.copyfile(_SATB, source)
    score = parse_score(source)
    rows = _facts(score)["rows"]
    assert len(rows) == 4 and all(row["split"] and row["laneId"] for row in rows)
    assert len({row["laneId"] for row in rows}) == 4
    for target in [part["part_id"] for part in score["parts"][:2]]:
        sheet = build_sheet(score, {"target": {"part_id": target}})
        plan = assemble_plan(sheet, first_option_choices(sheet), score=score)
        result = preprocess_voice_parts(score, request={"plan": plan})
        assert result["status"] == "ready", target
        score = result["score"]
    records = [value for value in score["voice_part_transforms"].values()
               if not (value.get("appended_part_ref") or {}).get("hidden_default_lane")]
    staff_ids = [part["part_id"] for part in score["parts"][:2]]
    for record in records:
        lane = record["derived_lane"]
        matching = [row for row in rows if row["laneId"] == lane["derived_lane_id"]]
        assert len(matching) == 1, lane["derived_lane_id"]
        assert matching[0]["sourcePartId"] == staff_ids[lane["source"]["part_index"]]
    assert {record["derived_lane"]["derived_lane_id"] for record in records} == {
        row["laneId"] for row in rows}


def _split_both_staves(score):
    for target in [part["part_id"] for part in score["parts"][:2]]:
        sheet = build_sheet(score, {"target": {"part_id": target}})
        plan = assemble_plan(sheet, first_option_choices(sheet), score=score)
        result = preprocess_voice_parts(score, request={"plan": plan})
        assert result["status"] == "ready", target
        score = result["score"]
    return score, result["modified_musicxml_path"]


def test_an_uploaded_split_score_offers_its_parts_and_the_staves_to_split_again(tmp_path) -> None:
    """A score SightSinger split before, uploaded as a new Song: each single line is a row
    as it is, and the two-voice staves are offered for splitting as well. Splitting them
    again makes new parts with ids of their own beside the uploaded ones, in the score
    record and in the MusicXML alike."""
    source = tmp_path / "made-up-satb.xml"
    shutil.copyfile(_SATB, source)
    _, exported = _split_both_staves(parse_score(source))
    uploaded = tmp_path / "uploaded-split.xml"
    shutil.copyfile(exported, uploaded)
    score = parse_score(uploaded)
    summary_parts = score["score_summary"]["parts"]
    assert len(summary_parts) == 6

    rows = _facts(score)["rows"]
    ready = [row for row in rows if not row["split"]]
    assert [row["partId"] for row in ready] == [part["part_id"] for part in summary_parts[2:]]
    assert all(row["hasWords"] for row in ready)
    assert len([row for row in rows if row["split"]]) == 4
    assert len({row["rowId"] for row in rows}) == 8

    split, path = _split_both_staves(score)
    assert len(split["parts"]) == 10
    resplit = parse_score(path)
    assert len(resplit["score_summary"]["parts"]) == 10
    raw_ids = [part.get("raw_part_id") or part["part_id"] for part in resplit["parts"][2:]]
    assert len(set(raw_ids)) == 8
    new_ids = [(record["appended_part_ref"] or {})["part_id"]
               for record in split["voice_part_transforms"].values()]
    assert len(new_ids) == 4 and all(part_id.endswith("_2") for part_id in new_ids)


def test_one_line_with_words_needs_no_split() -> None:
    score = _score()
    signal = score["voice_part_signals"]["parts"][0]
    signal["multi_voice_part"] = False
    signal["missing_lyric_voice_parts"] = []
    facts = _facts(score)
    assert len(facts["rows"]) == 1
    assert facts["rows"][0]["partId"] == score["score_summary"]["parts"][0]["part_id"]
    assert facts["rows"][0]["hasWords"] is True
    assert facts["rows"][0]["partName"] == score["score_summary"]["parts"][0]["part_name"]


def test_wordless_declared_voice_gets_one_solfege_row() -> None:
    score = _score()
    score["voice_part_signals"]["parts"][0]["multi_voice_part"] = False
    part = score["score_summary"]["parts"][0]
    part["has_lyrics"] = False
    part["instruments"][0]["is_explicit_vocal"] = True
    row = _facts(score)["rows"][0]
    assert row["hasWords"] is False
    assert row["note"] == "no_words"


def test_wordless_undeclared_part_is_asked_once_and_has_a_row() -> None:
    score = _score()
    score["voice_part_signals"]["parts"][0]["multi_voice_part"] = False
    part = score["score_summary"]["parts"][0]
    part["has_lyrics"] = False
    part["instruments"][0]["instrumental_candidate"] = True
    facts = _facts(score)
    assert len(facts["undecided_parts"]) == 1
    assert len(facts["rows"]) == 1
    assert facts["rows"][0]["note"] == "no_words"


def test_declared_instrument_waiting_for_sound_is_not_a_singing_row() -> None:
    score = _score()
    part = score["score_summary"]["parts"][0]
    part["has_lyrics"] = False
    route = part["instruments"][0]
    route.update({"synthetic": False, "instrumental_candidate": False,
                  "eligible_for_instrumental_midi": True})
    facts = _facts(score)
    assert facts["rows"] == []
    assert len(facts["instruments"]) == 1


def test_declared_instrument_with_usable_sound_needs_no_call() -> None:
    score = _score()
    route = score["score_summary"]["parts"][0]["instruments"][0]
    score["score_summary"]["parts"][0]["has_lyrics"] = False
    route.update({"synthetic": False, "instrumental_candidate": False,
                  "eligible_for_instrumental_midi": True,
                  "playback_preset": {"bank": 0, "kind": "melodic", "program": 0}})
    facts = _facts(score)
    assert facts["rows"] == facts["instruments"] == []


def test_chat_ruling_removes_role_question() -> None:
    score = _score()
    score["voice_part_signals"]["parts"][0]["multi_voice_part"] = False
    part = score["score_summary"]["parts"][0]
    part["has_lyrics"] = False
    route = part["instruments"][0]
    route["instrumental_candidate"] = True
    route["instrumental_role"] = "not_instrumental"
    vocal = _facts(score)
    assert vocal["undecided_parts"] == []
    assert len(vocal["rows"]) == 1
    route["instrumental_role"] = "instrumental"
    route["playback_preset"] = {"bank": 0, "kind": "melodic", "program": 0}
    instrument = _facts(score)
    assert instrument["undecided_parts"] == []
    assert instrument["rows"] == []


def test_no_lyrics_prevents_split_and_explains_it() -> None:
    score = _score()
    score["voice_part_signals"]["has_lyric_sources"] = False
    facts = _facts(score)
    assert facts["rows"] == []
    assert facts["notes"] == [f"{score['score_summary']['parts'][0]['part_id']}:cannot_split_without_lyrics"]


def test_each_part_is_quoted_by_its_own_music_whatever_its_name_or_history() -> None:
    """A split part goes through the same split check as any other part, with or without
    the record of the split that made it: here it needs no split, and its staff still does."""
    score = _score()
    original = score["score_summary"]["parts"][0]
    child = deepcopy(original)
    child.update({"part_index": 1, "part_id": "P_DERIVED_TEST",
                  "part_name": "Upper line (Derived)", "is_derived_part": True})
    score["score_summary"]["parts"].append(child)
    score["parts"].append({"part_id": "P_DERIVED_TEST", "part_name": "Upper line (Derived)",
                           "notes": []})
    without_record = _facts(score)
    score["voice_part_transforms"] = {
        "line": {"derived_lane": {"source": {"part_index": 0}},
                 "appended_part_ref": {"part_index": 1}}
    }
    for facts in (without_record, _facts(score)):
        assert [row["split"] for row in facts["rows"]] == [True, True, False]
        line = facts["rows"][2]
        assert (line["partId"], line["sourcePartId"]) == ("P_DERIVED_TEST", "P_DERIVED_TEST")


def test_open_staff_lists_both_planner_layouts_once(monkeypatch) -> None:
    # Force a genuine ambiguity in the lane evidence while keeping the notes
    # and the planner's two candidate skeletons from the made-up score.
    monkeypatch.setattr("src.api.sing_all_estimate.sheet_lane_counts",
                        lambda *args, **kwargs: LaneCounts(peak=2, real_voices=2, most=4))
    facts = _facts(_score())
    assert facts["rows"] == []
    assert len(facts["open_staffs"]) == 1
    staff = facts["open_staffs"][0]
    assert set(staff["rows_if"]) == {"shared", "separate"}
    assert all(row["split"] and row["hasWords"]
               for option in staff["rows_if"].values() for row in option)


def test_open_staff_evidence_includes_late_bars(monkeypatch) -> None:
    monkeypatch.setattr("src.api.sing_all_estimate.sheet_lane_counts",
                        lambda *args, **kwargs: LaneCounts(peak=2, real_voices=2, most=4))
    score = _score()
    late_note = deepcopy(next(note for note in score["parts"][0]["notes"]
                              if not note.get("is_rest")))
    late_note["measure_number"] = 6
    score["parts"][0]["notes"].append(late_note)
    seen = []
    def capture_xml(_score, _parts, bars):
        seen.append(bars)
        return "made-up evidence"
    monkeypatch.setattr("src.api.sing_all_estimate.trim_musicxml", capture_xml)
    facts = _facts(score)
    assert facts["open_staffs"]
    assert seen and 6 in seen[0]


def test_open_staff_over_planner_evidence_limit_has_no_rows(monkeypatch) -> None:
    monkeypatch.setattr("src.api.sing_all_estimate.sheet_lane_counts",
                        lambda *args, **kwargs: LaneCounts(peak=2, real_voices=2, most=4))
    score = _score()
    facts = quote_facts(score, score["score_summary"], [], evidence_max_chars=1)
    assert facts["rows"] == facts["open_staffs"] == []
    assert facts["notes"] == [
        f"{score['score_summary']['parts'][0]['part_id']}:cannot_split_evidence_too_large"]


def _satb_first_staff():
    score = parse_score(_SATB)
    score["parts"] = score["parts"][:1]
    score["score_summary"]["parts"] = score["score_summary"]["parts"][:1]
    score["voice_part_signals"]["parts"] = score["voice_part_signals"]["parts"][:1]
    return score


def test_two_voices_one_note_each_give_two_known_rows() -> None:
    facts = _facts(_satb_first_staff())
    assert len(facts["rows"]) == 2
    assert facts["open_staffs"] == []
    assert all(row["split"] for row in facts["rows"])


def test_chord_in_one_of_two_voices_gives_three_known_rows() -> None:
    score = _satb_first_staff()
    notes = score["parts"][0]["notes"]
    first = next(note for note in notes if note.get("voice") == "1" and not note.get("is_rest"))
    chord_note = deepcopy(first)
    chord_note["pitch_midi"] = float(first["pitch_midi"]) + 4
    notes.append(chord_note)
    facts = _facts(score)
    assert len(facts["rows"]) == 3
    assert facts["open_staffs"] == []


def test_two_chorded_voices_in_separate_bars_offer_two_or_four_rows() -> None:
    score = _satb_first_staff()
    part = score["parts"][0]
    part["notes"] = [note for note in part["notes"]
                     if (note.get("voice") == "1" and note["measure_number"] == 1)
                     or (note.get("voice") == "2" and note["measure_number"] == 2)]
    for voice in ("1", "2"):
        first = next(note for note in part["notes"] if note.get("voice") == voice
                     and not note.get("is_rest"))
        chord_note = deepcopy(first)
        chord_note["pitch_midi"] = float(first["pitch_midi"]) + 4
        part["notes"].append(chord_note)
    facts = _facts(score)
    assert facts["rows"] == []
    assert len(facts["open_staffs"]) == 1
    assert {layout: len(rows) for layout, rows in facts["open_staffs"][0]["rows_if"].items()} == {
        "shared": 2, "separate": 4}
    variants = facts["open_staffs"][0]["rows_if"]
    shared_keys = {row["rowId"] for row in variants["shared"]}
    separate_keys = {row["rowId"] for row in variants["separate"]}
    assert shared_keys <= separate_keys
    assert any(key.endswith(":slot2") for key in separate_keys - shared_keys)


def test_two_staves_one_line_each_still_need_two_split_rows() -> None:
    score = parse_score(_SATB)
    combined = deepcopy(score["parts"][0])
    combined["part_id"] = "P1-Combined"
    combined["notes"] = [
        {**note, "staff": str(staff)}
        for staff, part in enumerate(score["parts"], start=1)
        for note in part["notes"] if str(note.get("voice")) in {"1", "5"}
    ]
    score["parts"] = [combined]
    score["score_summary"]["parts"] = score["score_summary"]["parts"][:1]
    score["score_summary"]["parts"][0]["part_id"] = "P1-Combined"
    score["voice_part_signals"]["parts"] = score["voice_part_signals"]["parts"][:1]
    score["voice_part_signals"]["parts"][0]["multi_voice_part"] = True
    facts = _facts(score)
    assert len(facts["rows"]) == 2
    assert all(row["split"] for row in facts["rows"])


def test_oversize_open_staff_discards_earlier_rows_of_same_part(monkeypatch) -> None:
    score = parse_score(_SATB)
    combined = deepcopy(score["parts"][0])
    combined["part_id"] = "P1-Combined"
    combined["notes"] = [
        {**note, "staff": str(staff)}
        for staff, part in enumerate(score["parts"], start=1)
        for note in part["notes"] if str(note.get("voice")) in {"1", "5"}
    ]
    score["parts"] = [combined]
    score["score_summary"]["parts"] = score["score_summary"]["parts"][:1]
    score["score_summary"]["parts"][0]["part_id"] = "P1-Combined"
    score["voice_part_signals"]["parts"] = score["voice_part_signals"]["parts"][:1]
    score["voice_part_signals"]["parts"][0]["multi_voice_part"] = True
    monkeypatch.setattr("src.api.sing_all_estimate.sheet_lane_counts",
                        lambda *args, **kwargs: (LaneCounts(1, 1, 1) if kwargs["staff_scope"] == "1"
                                                 else LaneCounts(2, 2, 4)))
    monkeypatch.setattr("src.api.sing_all_estimate.trim_musicxml",
                        lambda *_args, **_kwargs: "oversize evidence")
    facts = quote_facts(score, score["score_summary"], [], evidence_max_chars=1)
    assert facts["rows"] == facts["open_staffs"] == []
    assert facts["notes"] == ["P1-Combined:cannot_split_evidence_too_large"]
