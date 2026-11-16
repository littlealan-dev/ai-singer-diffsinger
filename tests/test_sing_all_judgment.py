"""The Sing All call asks only unresolved questions and checks every reply."""

from copy import deepcopy
import asyncio
import json

import pytest

from src.backend.sing_all_judgment import (
    build_request, check_reply, check_style_reply, compatible_voicebanks,
    input_hash, quote_reply_schema, style_reply_schema,
    quote_judgment, style_judgment, QuoteUnavailable, StyleUnavailable,
)
from src.musicxml.instrument_programs import FLUIDR3_MELODIC_BANK, FLUIDR3_PERCUSSION_BANK


_VOICES = [
    {"id": "voice_a", "name": "Voice A", "languages": ["en", "ja"]},
    {"id": "voice_b", "name": "Voice B", "languages": ["en"]},
]


def _row(key):
    return {"rowId": key, "sourcePartId": "P1", "name": key,
            "partName": "Made-up lead",
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
    assert all(row["part_name"] == "Made-up lead" for row in request["rows"])


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


def test_style_schema_without_voice_colours_accepts_only_null() -> None:
    schema = style_reply_schema([{"rowId": "lead", "voice": "voice_a"}],
                                {"voice_a": {"voice_colors": []}})
    props = schema["properties"]["rows"]["properties"]["lead"]["properties"]
    assert "enum" not in props["voiceColor"]
    reply = {"rows": {"lead": {
        "voiceColor": None, "airiness": 0, "clarity": 100,
        "gender": 0, "intensity": 0.5}}}
    assert check_style_reply(reply, schema) == []
    reply["rows"]["lead"]["voiceColor"] = "unknown"
    assert check_style_reply(reply, schema) == ["lead: invalid voiceColor"]


def test_quote_call_reasks_invalid_sound_then_keeps_null_and_reuses_cache() -> None:
    calls = []
    invalid = _valid_reply()
    invalid["sounds"]["i1"] = {"bank": 0, "kind": "melodic", "program": 0,
                                 "evidence": []}

    async def call(request, *, response_schema):
        calls.append(request)
        return json.dumps(invalid)

    facts = _facts()
    result = asyncio.run(quote_judgment(
        facts, "en", _VOICES, prompt="quote task", model="model",
        score_fingerprint="made-up-score", call=call,
    ))
    assert len(calls) == 2
    assert calls[1]["previous_errors"]
    assert result["roles"] == {"P1": "voice"}
    assert result["sounds"]["P2-I1"] is None
    assert result["layouts"] == {"P1|1": "shared"}
    assert result["voices"]["shared-1"] == "voice_a"
    assert "separate-2" not in result["voices"]

    async def fail_if_called(request, *, response_schema):
        raise AssertionError("cached quote must not call the model")

    repeated = asyncio.run(quote_judgment(
        facts, "en", _VOICES, prompt="quote task", model="model",
        score_fingerprint="made-up-score", call=fail_if_called, cached=result,
    ))
    assert repeated == result


@pytest.mark.parametrize("missing_section", [False, True])
def test_missing_sound_reasks_once_then_becomes_null(missing_section) -> None:
    reply = _valid_reply()
    if missing_section:
        reply.pop("sounds")
    else:
        reply["sounds"].pop("i1")
    calls = []
    async def call(request, *, response_schema):
        calls.append(request)
        return json.dumps(reply)
    result = asyncio.run(quote_judgment(
        _facts(), "en", _VOICES, prompt="task", model="model",
        score_fingerprint="made-up-score", call=call))
    assert len(calls) == 2
    assert result["sounds"]["P2-I1"] is None


def test_quote_call_reasks_invalid_role_once_then_fails() -> None:
    reply = _valid_reply()
    reply["roles"]["p1"] = "other"
    count = 0

    async def call(request, *, response_schema):
        nonlocal count
        count += 1
        return json.dumps(reply)

    with pytest.raises(QuoteUnavailable):
        asyncio.run(quote_judgment(
            _facts(), "en", _VOICES, prompt="task", model="model",
            score_fingerprint="made-up-score", call=call,
        ))
    assert count == 2


def test_language_change_reuses_roles_and_layouts_and_switching_back_is_cached() -> None:
    voices = [dict(bank, languages=["en", "ja"]) for bank in _VOICES]
    calls = []

    async def call(request, *, response_schema):
        calls.append(request)
        if request["parts"]:
            return json.dumps(_valid_reply())
        return json.dumps({"voices": {f"r{index}": "voice_b" for index in range(1, 5)}})

    facts = _facts()
    english = asyncio.run(quote_judgment(
        facts, "en", voices, prompt="task", model="model",
        score_fingerprint="made-up-score", call=call,
    ))
    japanese = asyncio.run(quote_judgment(
        facts, "ja", voices, prompt="task", model="model",
        score_fingerprint="made-up-score", call=call, previous=english,
    ))
    assert len(calls) == 2
    assert calls[1]["parts"] == calls[1]["instruments"] == calls[1]["staffs"] == []
    assert japanese["roles"] == english["roles"]
    assert japanese["layouts"] == english["layouts"]
    assert japanese["voices"]["lead"] == "voice_b"
    assert asyncio.run(quote_judgment(
        facts, "en", voices, prompt="task", model="model",
        score_fingerprint="made-up-score", call=call,
        cached=english, previous=japanese,
    )) == english
    assert len(calls) == 2


def test_no_voice_for_language_returns_before_call() -> None:
    async def fail_if_called(request, *, response_schema):
        raise AssertionError("unsupported language must not call the model")

    with pytest.raises(QuoteUnavailable, match="no_voice_for_language"):
        asyncio.run(quote_judgment(
            _facts(), "fr", _VOICES, prompt="task", model="model",
            score_fingerprint="made-up-score", call=fail_if_called,
        ))


def test_style_call_reasks_bad_intensity_and_reuses_answer() -> None:
    rows = [{"rowId": "lead", "voice": "voice_a", "sourcePartId": "P1",
             "partName": "Made-up choir", "name": "Lead"}]
    banks = {"voice_a": {"voice_colors": [{"name": "Warm"}],
                         "default_voice_color": None,
                         "synthesis_control_defaults": {"airiness": 0, "clarity": 100,
                                                         "gender": 0, "intensity": 0.5}}}
    bad = {"rows": {"lead": {"voiceColor": "Warm", "airiness": 10,
                             "clarity": 100, "gender": 0, "intensity": 2}}}
    good = deepcopy(bad)
    good["rows"]["lead"]["intensity"] = 0.7
    calls = []

    async def call(request, *, response_schema, style):
        calls.append(request)
        assert style is True
        return json.dumps(bad if len(calls) == 1 else good)

    result = asyncio.run(style_judgment(
        "warmer lead", rows, banks, prompt="style task", model="model", call=call,
    ))
    assert len(calls) == 2
    assert calls[0]["rows"][0]["part_name"] == "Made-up choir"
    assert result["controls"]["lead"]["intensity"] == 0.7
    assert asyncio.run(style_judgment(
        "warmer lead", rows, banks, prompt="style task", model="model",
        call=call, cached=result,
    )) == result
    assert len(calls) == 2
    assert asyncio.run(style_judgment(
        "", rows, banks, prompt="style task", model="model", call=call,
    )) is None
    assert len(calls) == 2


def test_style_call_fails_after_second_bad_answer() -> None:
    rows = [{"rowId": "lead", "voice": "voice_a"}]
    banks = {"voice_a": {"voice_colors": []}}
    count = 0

    async def call(request, *, response_schema, style):
        nonlocal count
        count += 1
        return json.dumps({"rows": {"lead": {"voiceColor": "unknown"}}})

    with pytest.raises(StyleUnavailable):
        asyncio.run(style_judgment(
            "brighter", rows, banks, prompt="task", model="model", call=call,
        ))
    assert count == 2
