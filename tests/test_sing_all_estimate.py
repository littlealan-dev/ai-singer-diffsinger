"""Deterministic Sing All row discovery on a made-up two-bar score."""

from copy import deepcopy
from pathlib import Path

from src.api.score import parse_score
from src.api.sing_all_estimate import quote_facts
from src.api.voice_parts import LaneCounts


_SCORE = Path(__file__).parent / "fixtures" / "sa_chord_gaps_two_bars.xml"


def _score():
    return parse_score(_SCORE)


def _facts(score, *, verse=None):
    return quote_facts(score, score["score_summary"], [], verse_number=verse)


def test_split_rows_use_planner_lanes_and_estimate_ranges(capsys) -> None:
    score = _score()
    facts = _facts(score)
    assert len(facts["rows"]) == 2
    assert facts["open_staffs"] == []
    assert all(row["split"] and row["partId"] is None for row in facts["rows"])
    assert len({row["rowId"] for row in facts["rows"]}) == 2
    for row in facts["rows"]:
        estimated = row["pitchRange"]
        real = score["score_summary"]["parts"][0]["pitch_range"]
        print(f"{row['rowId']}: estimate {estimated.get('lowest_note')}..{estimated.get('highest_note')}; source {real['lowest_note']}..{real['highest_note']}")
        assert estimated["lowest_midi"] <= estimated["highest_midi"]
    assert "estimate" in capsys.readouterr().out


def test_one_line_with_words_needs_no_split() -> None:
    score = _score()
    signal = score["voice_part_signals"]["parts"][0]
    signal["multi_voice_part"] = False
    signal["missing_lyric_voice_parts"] = []
    facts = _facts(score)
    assert len(facts["rows"]) == 1
    assert facts["rows"][0]["partId"] == score["score_summary"]["parts"][0]["part_id"]
    assert facts["rows"][0]["hasWords"] is True


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


def test_visible_split_rows_are_reused_only_for_the_loaded_verse() -> None:
    score = _score()
    original = score["score_summary"]["parts"][0]
    child = deepcopy(original)
    child.update({"part_index": 1, "part_id": "P_DERIVED_TEST", "part_name": "Upper line",
                  "is_derived_part": True})
    score["score_summary"]["parts"].append(child)
    score["parts"].append({"part_id": "P_DERIVED_TEST", "part_name": "Upper line", "notes": []})
    score["voice_part_transforms"] = {
        "line": {"derived_lane": {"source": {"part_index": 0}},
                 "appended_part_ref": {"part_index": 1}}
    }
    same = _facts(score)
    assert [row["partId"] for row in same["rows"]] == ["P_DERIVED_TEST"]
    assert same["rows"][0]["split"] is False
    other = _facts(score, verse="2")
    assert len(other["rows"]) == 2
    assert all(row["split"] for row in other["rows"])


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


def test_open_staff_over_planner_evidence_limit_has_no_rows(monkeypatch) -> None:
    monkeypatch.setattr("src.api.sing_all_estimate.sheet_lane_counts",
                        lambda *args, **kwargs: LaneCounts(peak=2, real_voices=2, most=4))
    score = _score()
    facts = quote_facts(score, score["score_summary"], [], evidence_max_chars=1)
    assert facts["rows"] == facts["open_staffs"] == []
    assert facts["notes"] == [
        f"{score['score_summary']['parts'][0]['part_id']}:cannot_split_evidence_too_large"]
