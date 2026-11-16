"""Sing All prices made-up rows at the existing take and instrumental rates."""

import pytest

from src.backend.sing_all import (
    available_balance_from_user, build_items, choices_hash, mark_voice_ranges,
    materialize_rows, quote_lines,
    style_changes, summary_with_judged_instruments,
)


def _rows():
    return [
        {"rowId": "a", "sourcePartId": "P1", "split": True,
         "hasWords": True, "include": True},
        {"rowId": "b", "sourcePartId": "P1", "split": True,
         "hasWords": False, "include": True},
        {"rowId": "c", "sourcePartId": "P2", "split": False,
         "hasWords": False, "include": False},
    ]


@pytest.mark.parametrize("mode,takes", [
    ("lyrics", ["take:a:lyrics", "take:b:solfege"]),
    ("solfege", ["take:a:solfege", "take:b:solfege"]),
    ("both", ["take:a:lyrics", "take:a:solfege", "take:b:solfege"]),
])
def test_items_mode_and_wordless_row(mode, takes):
    items = build_items(_rows(), mode=mode, duration_seconds=59,
                        instrumental_charge_required=False)
    assert [item["itemId"] for item in items] == takes + ["split:P1"]
    assert [item["creditsReserved"] for item in items] == [2] * len(takes) + [0]


def test_one_instrumental_charge_and_quote_lines():
    items = build_items(_rows(), mode="both", duration_seconds=59,
                        instrumental_charge_required=True)
    assert [item["itemId"] for item in items].count("instrumental") == 1
    assert items[-1]["creditsReserved"] == 1
    lines = quote_lines(items, duration_seconds=59, balance=42)
    assert lines == [
        {"kind": "singing", "parts": 2, "takes": 3, "secondsEach": 59,
         "credits": 6},
        {"kind": "preparing", "credits": 0},
        {"kind": "instrumentals", "credits": 1},
        {"kind": "total", "credits": 7},
        {"kind": "balance", "before": 42, "after": 35},
    ]


def test_instrumentals_already_paid_and_empty_singing():
    items = build_items([dict(_rows()[0], include=False)], mode="lyrics",
                        duration_seconds=121, instrumental_charge_required=False)
    assert items == []
    assert quote_lines(items, duration_seconds=121, balance=9)[-2:] == [
        {"kind": "total", "credits": 0},
        {"kind": "balance", "before": 9, "after": 9},
    ]


def test_choices_hash_covers_style_note_and_controls():
    choices = {"language": "en", "rows": [{"rowId": "a", "voice": "voice_a"}]}
    style = {"note": "warm", "controls": {"a": {"airiness": 10}}}
    key = choices_hash(choices, style)
    assert key == choices_hash(dict(reversed(list(choices.items()))), style)
    assert key != choices_hash(choices, {**style, "note": "bright"})
    assert key != choices_hash(choices, {**style, "controls": {"a": {"airiness": 11}}})


def test_available_balance_reads_legacy_topups_without_writing_credits():
    user = {"credits": {"balance": 10, "reserved": 3},
            "topupCredits": {"totalRemaining": 8, "totalReserved": 0}}
    assert available_balance_from_user(user) == 15
    user["topupCredits"].update(totalAvailable=4, totalReserved=2)
    assert available_balance_from_user(user) == 11


@pytest.mark.parametrize("role,note,ticked", [
    ("voice", "no_words", True),
    ("instrument", "likely_instrument", False),
    ("unsure", "undecided", False),
])
def test_undecided_part_role_sets_default_tick_and_note(role, note, ticked):
    facts = {"rows": [{"rowId": "a", "sourcePartId": "P1", "note": "no_words"}],
             "open_staffs": []}
    judgment = {"roles": {"P1": role}, "voices": {"a": "voice_a"}}
    row = materialize_rows(facts, judgment)[0]
    assert (row["include"], row["note"]) == (ticked, note)
    selected = [{"rowId": "a", "include": True, "voicePicked": "user", "voice": "voice_b"}]
    assert materialize_rows(facts, judgment, selected)[0]["voice"] == "voice_b"


def test_only_chosen_staff_layout_is_shown_and_split_is_priced_once():
    facts = {"rows": [], "open_staffs": [{"key": "P1|1", "rows_if": {
        "shared": [dict(_rows()[0])], "separate": [dict(_rows()[0]), dict(_rows()[1])],
    }}]}
    judgment = {"layouts": {"P1|1": "separate"},
                "voices": {"a": "voice_a", "b": "voice_a"}}
    rows = materialize_rows(facts, judgment)
    assert [row["rowId"] for row in rows] == ["a", "b"]
    assert [item["itemId"] for item in build_items(
        rows, mode="lyrics", duration_seconds=59,
        instrumental_charge_required=False)] == [
            "take:a:lyrics", "take:b:solfege", "split:P1"]


def test_selected_voice_outside_declared_range_sets_note():
    rows = [{"rowId": "a", "voice": "voice_a", "note": None,
             "pitchRange": {"lowest_note": "C3", "highest_note": "G4"}}]
    mark_voice_ranges(rows, {"voice_a": {"supported_range": "D3 - C6"}})
    assert rows[0]["note"] == "voice_out_of_range"
    rows[0]["note"] = None
    mark_voice_ranges(rows, {"voice_a": {"supported_range": "C3 - C6"}})
    assert rows[0]["note"] is None


def test_style_summary_shows_only_departures_from_row_voice_defaults():
    rows = [{"rowId": "a", "voice": "voice_a", "include": True},
            {"rowId": "b", "voice": "voice_b", "include": False}]
    banks = {
        "voice_a": {"default_voice_color": "Warm",
                    "synthesis_control_defaults": {"airiness": 0, "clarity": 100,
                                                   "gender": 0, "intensity": 0.5}},
        "voice_b": {"default_voice_color": None,
                    "synthesis_control_defaults": {"airiness": 5}},
    }
    controls = {
        "a": {"voiceColor": "Warm", "airiness": 10, "clarity": 100,
              "gender": -10, "intensity": 0.7},
        "b": {"voiceColor": None, "airiness": 15},
    }
    assert style_changes(rows, controls, banks) == {
        "a": {"airiness": 10, "gender": -10, "intensity": pytest.approx(0.2)}}


@pytest.mark.parametrize("role,sound,expected", [
    ("voice", None, False),
    ("instrument", None, True),
    ("unsure", None, True),
    ("instrument", {"bank": 0, "kind": "melodic", "program": 0,
                    "evidence": ["keyboard staff"]}, True),
])
def test_undecided_part_counts_as_instrumental_unless_judged_voice(role, sound, expected):
    summary = {"parts": [{"part_id": "P1", "instruments": [{
        "score_instrument_id": "P1-I1", "instrumental_candidate": True,
        "is_percussion": False, "playback_preset": None,
        "eligible_for_instrumental_midi": False,
    }]}], "instrument_program_resolution": {
        "unresolved_score_instrument_ids": ["P1-I1"],
        "instrumental_score_instrument_ids": [], "unresolved_reasons": {"P1-I1": "undeclared"},
    }}
    amended = summary_with_judged_instruments(
        summary, {"roles": {"P1": role}, "sounds": {"P1-I1": sound}})
    assert bool(amended["instrument_program_resolution"]["instrumental_score_instrument_ids"]) is expected
    assert summary["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
    if role != "voice":
        ticked = summary_with_judged_instruments(
            summary, {"roles": {"P1": role}, "sounds": {"P1-I1": sound}},
            sung_source_ids={"P1"})
        assert ticked["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
