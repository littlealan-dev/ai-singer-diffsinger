"""The Sing All call asks only unresolved questions and checks every reply."""

from copy import deepcopy

import pytest

from src.backend.sing_all_judgment import (
    build_request, check_reply, check_style_reply, compatible_voicebanks,
    input_hash, quote_reply_schema, style_reply_schema,
)
from src.musicxml.instrument_programs import FLUIDR3_MELODIC_BANK, FLUIDR3_PERCUSSION_BANK


_VOICES = [
    {"id": "voice_a", "name": "Voice A", "languages": ["en", "ja"]},
    {"id": "voice_b", "name": "Voice B", "languages": ["en"]},
]


def _row(key):
    return {"rowId": key, "sourcePartId": "P1", "name": key,
            "pitchRange": {"lowest_note": "C4", "highest_note": "G4",
                           "tessitura_low_note": "D4", "tessitura_high_note": "F4"}}


def _facts():
    candidate = {"score_instrument_id": "P1:default", "is_percussion": False}
    drum = {"score_instrument_id": "P2-I1", "is_percussion": True,
            "program_source": "unresolved"}
    return {
        "rows": [_row("lead")],
        "undecided_parts": [{"partId": "P1", "part": {
            "part_id": "P1", "part_name": "Lead", "instruments": [candidate]}}],
        "instruments": [{"partId": "P2", "route": drum}],
        "open_staffs": [{"key": "P1|1", "evidence": {
            "part_name": "Lead", "staff_peak": 2, "lanes_if_shared": 2,
            "lanes_if_separate": 4, "voices": {}, "score_xml": "<score/>"},
            "rows_if": {"shared": [_row("shared-1")],
                        "separate": [_row("separate-1"), _row("separate-2")]}}],
    }


def _valid_reply():
    return {
        "roles": {"p1": "voice"},
        "sounds": {"p1": None, "i1": {
            "bank": FLUIDR3_PERCUSSION_BANK, "kind": "percussion_kit",
            "program": 0, "evidence": ["drum staff"]}},
        "layouts": {"s1": "shared"},
        "voices": {"r1": "voice_a", "r2": "voice_a", "r3": "voice_b", "r4": "voice_a"},
    }


def test_request_has_one_staff_and_both_layouts_with_short_keys() -> None:
    questions = build_request(_facts(), "en", _VOICES)
    request = questions.request
    assert request["language"] == "en"
    assert len(request["parts"]) == len(request["instruments"]) == len(request["staffs"]) == 1
    assert len(request["rows"]) == 4
    assert [row["layout"] for row in request["rows"]] == [None, "shared", "separate", "separate"]
    assert set(questions.rows) == {"r1", "r2", "r3", "r4"}
    assert request["staffs"][0]["score_xml"] == "<score/>"


def test_no_open_staff_and_one_compatible_voice_need_no_call() -> None:
    facts = {"rows": [_row("lead")], "undecided_parts": [],
             "instruments": [], "open_staffs": []}
    questions = build_request(facts, "ja", _VOICES)
    assert questions.request["staffs"] == []
    assert questions.request["rows"] == []
    assert questions.has_questions is False
    assert list(questions.automatic_voices.values()) == ["voice_a"]


def test_language_change_can_ask_only_voices() -> None:
    questions = build_request(
        _facts(), "en", _VOICES,
        previous={"roles": {"P1": "voice"}, "sounds": {"P2-I1": None},
                  "layouts": {"P1|1": "shared"}},
    )
    assert questions.request["parts"] == []
    assert questions.request["instruments"] == []
    assert questions.request["staffs"] == []
    assert len(questions.request["rows"]) == 4


def test_quote_reply_schema_uses_route_constants_and_compatible_voices() -> None:
    questions = build_request(_facts(), "en", _VOICES)
    schema = quote_reply_schema(questions, compatible_voicebanks(_VOICES, "en"))
    assert set(schema["required"]) == {"roles", "sounds", "layouts", "voices"}
    assert schema["properties"]["roles"]["properties"]["p1"]["enum"] == [
        "voice", "instrument", "unsure"]
    sounds = schema["properties"]["sounds"]["properties"]
    assert sounds["p1"]["properties"]["bank"]["enum"] == [FLUIDR3_MELODIC_BANK]
    assert sounds["i1"]["properties"]["bank"]["enum"] == [FLUIDR3_PERCUSSION_BANK]
    assert schema["properties"]["voices"]["properties"]["r1"]["enum"] == [
        "voice_a", "voice_b"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda reply: reply["sounds"].pop("i1"),
        lambda reply: reply["sounds"].update({"p1": {
            "bank": 0, "kind": "melodic", "program": 0, "evidence": []}}),
        lambda reply: reply["sounds"]["i1"].update({"bank": 0, "kind": "melodic"}),
        lambda reply: reply["sounds"]["i1"].update({"program": 128}),
    ],
)
def test_invalid_sounds_are_identified_for_one_reask(mutation) -> None:
    questions = build_request(_facts(), "en", _VOICES)
    reply = _valid_reply()
    mutation(reply)
    errors, sound_errors = check_reply(questions, reply, ["voice_a", "voice_b"])
    assert errors
    assert sound_errors


@pytest.mark.parametrize(
    "mutation",
    [
        lambda reply: reply["roles"].update({"p1": "other"}),
        lambda reply: reply["layouts"].update({"s1": "mixed"}),
        lambda reply: reply["voices"].update({"r1": "unknown"}),
        lambda reply: reply["voices"].pop("r2"),
    ],
)
def test_invalid_roles_layouts_and_voices_are_not_sound_only(mutation) -> None:
    questions = build_request(_facts(), "en", _VOICES)
    reply = _valid_reply()
    mutation(reply)
    errors, sound_errors = check_reply(questions, reply, ["voice_a", "voice_b"])
    assert errors
    assert not sound_errors


def test_valid_quote_reply_and_input_hash() -> None:
    questions = build_request(_facts(), "en", _VOICES)
    assert check_reply(questions, _valid_reply(), ["voice_a", "voice_b"]) == ([], set())
    key = input_hash(questions.request, "prompt", "model")
    assert key == input_hash(deepcopy(questions.request), "prompt", "model")
    assert key != input_hash(questions.request, "changed", "model")


def test_style_schema_and_control_validation() -> None:
    bank = {"voice_a": {"voice_colors": [{"name": "Warm"}]}}
    schema = style_reply_schema([{"rowId": "lead", "voice": "voice_a"}], bank)
    props = schema["properties"]["rows"]["properties"]["lead"]["properties"]
    assert props["voiceColor"]["enum"] == ["Warm"]
    assert props["airiness"]["minimum"] == -100
    assert props["clarity"]["maximum"] == 200
    assert props["intensity"]["maximum"] == 1
    assert "Tension" in props["intensity"]["description"]
    reply = {"rows": {"lead": {
        "voiceColor": "Warm", "airiness": 10, "clarity": 100,
        "gender": 0, "intensity": 0.5}}}
    assert check_style_reply(reply, schema) == []
    for field, value in (("voiceColor", "Cold"), ("airiness", 101),
                         ("intensity", 1.1)):
        bad = deepcopy(reply)
        bad["rows"]["lead"][field] = value
        assert check_style_reply(bad, schema)
    assert check_style_reply({"rows": {}}, schema)
