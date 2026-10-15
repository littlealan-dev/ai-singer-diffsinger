"""The structured quote reply: what it must copy exactly, and how it reads in the chat."""

from __future__ import annotations

from src.backend.quote_presentation import (
    QUOTE_PRESENTATION_SCHEMA,
    presentation_for_quote,
    presented_quote,
    quote_presentation_problems,
    quote_presentation_schema,
    render_quote_presentation,
)

QUOTE = {
    "status": "quote_ready",
    "quote_id": "q-123",
    "part_id": "Voice",
    "vocal_duration_seconds": 4.5,
    "vocal_part_credits": 1,
    "instrumental_credits": 1,
    "total_estimated_credits": 2,
    "available_credits": 8,
    "balance_after": 6,
    "render_choices": {
        "part_id": "Voice",
        "voicebank": "Qixuan_v2.7.0_DiffSinger_OpenUtau",
        "language": "en",
        "lyric_selection": {"id": "lyr_1", "number": "1", "name": ""},
        "expand_repeats": True,
    },
}


def _reply(**changes):
    reply = presentation_for_quote(QUOTE)
    for path, value in changes.items():
        target = reply["quote_presentation"]
        *parents, leaf = path.split("__")
        for name in parents:
            target = target[name]
        if value is None:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return reply


def test_an_exact_presentation_has_no_problems():
    assert quote_presentation_problems(_reply(), QUOTE) == []


def test_ids_numbers_and_flags_must_be_exact_copies():
    cases = {
        "quote_id": ("q-other", "quote_id must be 'q-123'"),
        "part__part_id": ("Alto", "part.part_id must be 'Voice'"),
        "lyrics__lyric_selection_id": ("lyr_2", "lyrics.lyric_selection_id must be 'lyr_1'"),
        "language__code": ("ja", "language.code must be 'en'"),
        "voice__voicebank_id": ("Dummy", "voice.voicebank_id must be 'Qixuan_v2.7.0_DiffSinger_OpenUtau'"),
        "repeats__expand_repeats": (False, "repeats.expand_repeats must be True"),
        "duration__seconds": (9.0, "duration.seconds must be 4.5"),
        "total_credits__credits": (3, "total_credits.credits must be 2"),
        "balance_after__credits": (5, "balance_after.credits must be 6"),
    }
    for path, (value, problem) in cases.items():
        assert quote_presentation_problems(_reply(**{path: value}), QUOTE) == [problem], path


def test_credits_are_numbers_not_text():
    assert quote_presentation_problems(_reply(total_credits__credits="2"), QUOTE) == [
        "total_credits.credits must be 2"
    ]
    # A whole float from JSON still counts as the integer.
    assert quote_presentation_problems(_reply(total_credits__credits=2.0), QUOTE) == []


def test_required_rows_and_texts_must_be_present():
    assert quote_presentation_problems(_reply(voice=None), QUOTE) == ["voice is missing"]
    assert quote_presentation_problems(_reply(closing=""), QUOTE) == ["closing is missing"]
    assert quote_presentation_problems(_reply(part__display=" "), QUOTE) == ["part.display is missing"]
    assert quote_presentation_problems({"tool_calls": []}, QUOTE) == ["quote_presentation is missing"]
    assert quote_presentation_problems(None, QUOTE) == ["the reply is not a JSON object"]


def test_balance_rows_are_required_and_checked_when_the_quote_has_a_balance():
    reply = _reply(available_credits=None, balance_after=None)
    assert quote_presentation_problems(reply, QUOTE) == [
        "available_credits is missing",
        "balance_after is missing",
    ]
    # Only a quote made under the e2e credit bypass has no balance to check against.
    no_balance = {key: value for key, value in QUOTE.items() if key not in {"available_credits", "balance_after"}}
    assert quote_presentation_problems(reply, no_balance) == []
    assert quote_presentation_problems(_reply(balance_after__credits=99), no_balance) == []


def test_a_quote_that_removes_player_takes_needs_the_note():
    quote = {**QUOTE, "takes_removed_from_player": {"takes": [{"part": "Alto"}]}}
    assert quote_presentation_problems(_reply(takes_removed_note=None), quote) == [
        "takes_removed_note is missing"
    ]
    assert quote_presentation_problems(_reply(takes_removed_note="Replaces the Alto take."), quote) == []


def test_the_chat_shows_intro_table_notes_and_closing():
    reply = _reply(
        intro="Since you picked Qixuan, here is the updated quote.",
        instrumental_credits__note="covers all instrumental tracks",
    )
    text = render_quote_presentation(reply["quote_presentation"])
    assert text == "\n\n".join([
        "Since you picked Qixuan, here is the updated quote.",
        "\n".join([
            "| Attribute | Value |",
            "| --- | --- |",
            "| Vocal part | Voice |",
            "| Lyrics | lyr_1 |",
            "| Language | en |",
            "| AI voice | Qixuan_v2.7.0_DiffSinger_OpenUtau |",
            "| Repeats | With repeats |",
            "| Estimated duration | 4.5 seconds |",
            "| Vocal credits | 1 |",
            "| Instrumental credits | 1 (covers all instrumental tracks) |",
            "| Total estimated credits | 2 |",
            "| Credits available | 8 |",
            "| Balance after this take | 6 |",
        ]),
        "Credits are estimates; the final charge follows the generated audio's length.",
        "Shall I go ahead?",
    ])


def test_table_cells_cannot_break_the_table():
    reply = _reply(voice__display="Qixuan | v2.7.0\nnew line")
    text = render_quote_presentation(reply["quote_presentation"])
    assert "| AI voice | Qixuan \\| v2.7.0 new line |" in text


def test_the_quote_to_present_is_top_level_or_nested_under_a_refusal():
    refusal = {"status": "action_required", "action": "synthesis_quote_refresh_required", "quote": QUOTE}
    failed = {"status": "action_required", "quote": {"status": "action_required"}}
    assert presented_quote(QUOTE) is QUOTE
    assert presented_quote(refusal) is QUOTE
    assert presented_quote(failed) is None
    assert presented_quote(None) is None


def test_the_schema_allows_no_tool_calls_and_types_the_copies():
    top = QUOTE_PRESENTATION_SCHEMA["properties"]
    assert set(top) == {"quote_presentation"}
    rows = top["quote_presentation"]["properties"]
    assert rows["total_credits"]["properties"]["credits"]["type"] == "INTEGER"
    assert rows["duration"]["properties"]["seconds"]["type"] == "NUMBER"
    assert rows["repeats"]["properties"]["expand_repeats"]["type"] == "BOOLEAN"
    required = top["quote_presentation"]["required"]
    assert {"available_credits", "balance_after"} <= set(required)
    assert "takes_removed_note" not in required


def test_the_player_notice_is_required_only_for_a_quote_that_replaces_takes():
    plain = quote_presentation_schema(QUOTE)
    replacing = quote_presentation_schema({**QUOTE, "takes_removed_from_player": {"takes": [{}]}})
    assert plain is QUOTE_PRESENTATION_SCHEMA
    assert "takes_removed_note" in replacing["properties"]["quote_presentation"]["required"]
    # The shared schema is not changed by building one for a quote.
    assert "takes_removed_note" not in QUOTE_PRESENTATION_SCHEMA["properties"]["quote_presentation"]["required"]


STYLED = {
    **QUOTE,
    "render_choices": {**QUOTE["render_choices"], "gender": 20.0, "voice_color": "Soft"},
}


def test_a_styled_quote_requires_the_style_row_with_exact_copies():
    schema = quote_presentation_schema(STYLED)["properties"]["quote_presentation"]
    assert "voice_style" in schema["required"]
    style_row = schema["properties"]["voice_style"]
    # Exactly the controls this quote sets are offered, and all are required.
    assert set(style_row["properties"]) == {"label", "display", "gender", "voice_color"}
    assert set(style_row["required"]) == {"label", "display", "gender", "voice_color"}
    assert style_row["properties"]["gender"]["type"] == "NUMBER"
    assert style_row["properties"]["voice_color"]["type"] == "STRING"
    reply = presentation_for_quote(STYLED)
    assert quote_presentation_problems(reply, STYLED) == []

    missing = presentation_for_quote(STYLED)
    del missing["quote_presentation"]["voice_style"]
    assert quote_presentation_problems(missing, STYLED) == ["voice_style is missing"]

    wrong = presentation_for_quote(STYLED)
    wrong["quote_presentation"]["voice_style"]["gender"] = 40
    wrong["quote_presentation"]["voice_style"]["airiness"] = 10
    assert quote_presentation_problems(wrong, STYLED) == [
        "voice_style.gender must be 20.0",
        "voice_style.airiness is not in the quote",
    ]


def test_an_unstyled_quote_needs_no_style_row():
    assert "voice_style" not in quote_presentation_schema(QUOTE)["properties"]["quote_presentation"]["required"]
    assert quote_presentation_problems(_reply(), QUOTE) == []


def test_an_unstyled_quote_offers_no_control_fields_for_voicebank_defaults():
    """Production 2026-10-15: a Qixuan quote with no style inputs got its reply
    rejected 8 times, because the schema offered gender and clarity and the
    model filled them with Qixuan's defaults. Now no such field exists."""
    style_row = quote_presentation_schema(QUOTE)["properties"]["quote_presentation"]["properties"]["voice_style"]
    assert set(style_row["properties"]) == {"label", "display"}
    # A text-only "default" row is still allowed.
    assert quote_presentation_problems(
        _reply(voice_style={"label": "Voice style", "display": "Default (clarity 170, gender 20.83)"}),
        QUOTE,
    ) == []
    # The check still refuses a copy the quote does not set, should a client
    # ever return one.
    assert quote_presentation_problems(
        _reply(voice_style={"label": "Voice style", "display": "Default", "gender": 20.83, "clarity": 170}),
        QUOTE,
    ) == ["voice_style.gender is not in the quote", "voice_style.clarity is not in the quote"]


def test_a_quote_with_two_style_inputs_offers_both():
    quote = {**QUOTE, "render_choices": {**QUOTE["render_choices"], "gender": -30.0, "airiness": 5.0}}
    style_row = quote_presentation_schema(quote)["properties"]["quote_presentation"]["properties"]["voice_style"]
    assert set(style_row["properties"]) == {"label", "display", "gender", "airiness"}
    assert set(style_row["required"]) == {"label", "display", "gender", "airiness"}


def test_the_style_row_is_shown_after_the_voice():
    reply = presentation_for_quote(STYLED)
    reply["quote_presentation"]["voice_style"]["display"] = "Soft colour, more masculine (gender +20)"
    text = render_quote_presentation(reply["quote_presentation"])
    voice_row = "| AI voice | Qixuan_v2.7.0_DiffSinger_OpenUtau |"
    style_row = "| Voice style | Soft colour, more masculine (gender +20) |"
    assert text.index(voice_row) < text.index(style_row) < text.index("| Repeats |")
