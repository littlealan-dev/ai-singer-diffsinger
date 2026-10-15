"""The structured reply that presents a synthesis quote: schema, checks and chat text.

The reply presenting a quote is generated under QUOTE_PRESENTATION_SCHEMA, which
has no tool_calls, so the model cannot act on an earlier message in that step.
Ids and numbers are copies the code checks against the quote; labels and display
texts are the model's own words. A quote counts as presented only when its reply
passes these checks.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional


def _row(label_example: str, fields: Dict[str, Dict[str, Any]], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "OBJECT",
        "required": ["label", *required],
        "properties": {
            "label": {"type": "STRING", "description": f"Row label, e.g. '{label_example}'."},
            **fields,
        },
    }


QUOTE_PRESENTATION_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "required": ["quote_presentation"],
    "properties": {
        "quote_presentation": {
            "type": "OBJECT",
            "description": (
                "The quote to show the user. Copy ids and numbers exactly from the quote "
                "payload; write labels and display texts in your own words."
            ),
            "required": [
                "quote_id", "intro", "part", "lyrics", "language", "voice", "repeats",
                "duration", "vocal_credits", "instrumental_credits", "total_credits",
                "available_credits", "balance_after", "estimate_note", "closing",
            ],
            "propertyOrdering": [
                "quote_id", "intro", "part", "lyrics", "language", "voice", "voice_style",
                "repeats", "duration", "vocal_credits", "instrumental_credits", "total_credits",
                "available_credits", "balance_after", "estimate_note", "takes_removed_note",
                "closing",
            ],
            "properties": {
                "quote_id": {"type": "STRING", "description": "Copy of quote_id from the payload."},
                "intro": {
                    "type": "STRING",
                    "description": (
                        "One or two sentences introducing the quote. If changed_choices is "
                        "present, say what changed since the quote the user saw before."
                    ),
                },
                "part": _row("Vocal part", {
                    "part_id": {"type": "STRING", "description": "Copy of part_id from the payload."},
                    "display": {"type": "STRING", "description": "The part's name as the user knows it."},
                }, ["part_id", "display"]),
                "lyrics": _row("Lyrics", {
                    "lyric_selection_id": {
                        "type": "STRING",
                        "description": "Copy of render_choices.lyric_selection.id.",
                    },
                    "display": {
                        "type": "STRING",
                        "description": "The verse or lyric line in words, e.g. 'Verse 1' or 'Solfège'.",
                    },
                }, ["lyric_selection_id", "display"]),
                "language": _row("Language", {
                    "code": {"type": "STRING", "description": "Copy of render_choices.language."},
                    "display": {"type": "STRING", "description": "The language's name, e.g. 'English'."},
                }, ["code", "display"]),
                "voice": _row("AI voice", {
                    "voicebank_id": {"type": "STRING", "description": "Copy of render_choices.voicebank."},
                    "display": {"type": "STRING", "description": "The voice's display name."},
                }, ["voicebank_id", "display"]),
                "voice_style": _row("Voice style", {
                    "display": {
                        "type": "STRING",
                        "description": (
                            "The style/color and the voice controls in words, "
                            "e.g. 'More masculine (gender +20)'."
                        ),
                    },
                    "voice_color": {"type": "STRING", "description": "Copy of render_choices.voice_color."},
                    "gender": {"type": "NUMBER", "description": "Copy of render_choices.gender."},
                    "airiness": {"type": "NUMBER", "description": "Copy of render_choices.airiness."},
                    "intensity": {"type": "NUMBER", "description": "Copy of render_choices.intensity."},
                    "clarity": {"type": "NUMBER", "description": "Copy of render_choices.clarity."},
                    "articulation": {
                        "type": "NUMBER",
                        "description": "Copy of render_choices.articulation.",
                    },
                }, ["display"]),
                "repeats": _row("Repeats", {
                    "expand_repeats": {
                        "type": "BOOLEAN",
                        "description": "Copy of render_choices.expand_repeats.",
                    },
                    "display": {
                        "type": "STRING",
                        "description": "Says whether repeats are sung or the written order is kept.",
                    },
                }, ["expand_repeats", "display"]),
                "duration": _row("Estimated duration", {
                    "seconds": {"type": "NUMBER", "description": "Copy of vocal_duration_seconds."},
                    "display": {
                        "type": "STRING",
                        "description": "The duration in words, e.g. 'about 4.5 seconds'.",
                    },
                }, ["seconds", "display"]),
                "vocal_credits": _row("Vocal credits", {
                    "credits": {"type": "INTEGER", "description": "Copy of vocal_part_credits."},
                }, ["credits"]),
                "instrumental_credits": _row("Instrumental credits", {
                    "credits": {
                        "type": "INTEGER",
                        "description": "Copy of instrumental_credits (0 when already generated for this score).",
                    },
                    "note": {
                        "type": "STRING",
                        "description": "Optional short note, e.g. 'covers all instrumental tracks' or 'already generated'.",
                    },
                }, ["credits"]),
                "total_credits": _row("Total estimated credits", {
                    "credits": {"type": "INTEGER", "description": "Copy of total_estimated_credits."},
                }, ["credits"]),
                "available_credits": _row("Credits available", {
                    "credits": {
                        "type": "INTEGER",
                        "description": "Copy of available_credits.",
                    },
                }, ["credits"]),
                "balance_after": _row("Balance after this take", {
                    "credits": {
                        "type": "INTEGER",
                        "description": "Copy of balance_after.",
                    },
                }, ["credits"]),
                "estimate_note": {
                    "type": "STRING",
                    "description": (
                        "Says the credits are estimates and the final charge is based on the "
                        "generated audio's length."
                    ),
                },
                "takes_removed_note": {
                    "type": "STRING",
                    "description": (
                        "Include only when the payload has takes_removed_from_player: which "
                        "player takes this take replaces, and why."
                    ),
                },
                "closing": {
                    "type": "STRING",
                    "description": (
                        "Asks the user to confirm this quote. The user's latest message was "
                        "before this quote and does not confirm it."
                    ),
                },
            },
        },
    },
}

# The quote's optional style inputs. None changes the price, but each one the
# quote sets is shown, so the user sees what they are confirming.
STYLE_FIELDS = ("voice_color", "gender", "airiness", "intensity", "clarity", "articulation")


def _quote_style(quote: Dict[str, Any]) -> Dict[str, Any]:
    choices = quote.get("render_choices") if isinstance(quote.get("render_choices"), dict) else {}
    return {key: choices[key] for key in STYLE_FIELDS if choices.get(key) is not None}


def quote_presentation_schema(quote: Dict[str, Any]) -> Dict[str, Any]:
    """The schema for presenting this quote.

    Its player notice is required when it replaces takes, and its style row,
    with a copy of each style input it sets, when it has any.
    """
    style = _quote_style(quote)
    if not quote.get("takes_removed_from_player") and not style:
        return QUOTE_PRESENTATION_SCHEMA
    schema = copy.deepcopy(QUOTE_PRESENTATION_SCHEMA)
    presentation = schema["properties"]["quote_presentation"]
    if quote.get("takes_removed_from_player"):
        presentation["required"].append("takes_removed_note")
    if style:
        presentation["required"].append("voice_style")
        presentation["properties"]["voice_style"]["required"].extend(style)
    return schema


QUOTE_PRESENTATION_INSTRUCTION = (
    "Present this quote by returning quote_presentation as the response schema describes. "
    "The user's latest message was before this quote and does not confirm it."
)

_ROWS = (
    ("part", "display"),
    ("lyrics", "display"),
    ("language", "display"),
    ("voice", "display"),
    ("voice_style", "display"),
    ("repeats", "display"),
    ("duration", "display"),
    ("vocal_credits", "credits"),
    ("instrumental_credits", "credits"),
    ("total_credits", "credits"),
    ("available_credits", "credits"),
    ("balance_after", "credits"),
)


def presented_quote(payload: Any) -> Optional[Dict[str, Any]]:
    """The quote a message-only payload asks the reply to present, if any.

    A quote is either the payload itself or, after a refused synthesize, nested
    under the refusal's "quote" field.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("status") == "quote_ready":
        return payload
    nested = payload.get("quote")
    if isinstance(nested, dict) and nested.get("status") == "quote_ready":
        return nested
    return None


def _expected_copies(quote: Dict[str, Any]) -> Dict[tuple[str, str], Any]:
    choices = quote.get("render_choices") if isinstance(quote.get("render_choices"), dict) else {}
    lyric_selection = choices.get("lyric_selection") if isinstance(choices.get("lyric_selection"), dict) else {}
    return {
        ("part", "part_id"): quote.get("part_id"),
        ("lyrics", "lyric_selection_id"): lyric_selection.get("id"),
        ("language", "code"): choices.get("language"),
        ("voice", "voicebank_id"): choices.get("voicebank"),
        ("repeats", "expand_repeats"): bool(choices.get("expand_repeats")),
        ("duration", "seconds"): quote.get("vocal_duration_seconds"),
        ("vocal_credits", "credits"): quote.get("vocal_part_credits"),
        ("instrumental_credits", "credits"): quote.get("instrumental_credits"),
        ("total_credits", "credits"): quote.get("total_estimated_credits"),
        ("available_credits", "credits"): quote.get("available_credits"),
        ("balance_after", "credits"): quote.get("balance_after"),
    }


def _same(field: str, actual: Any, expected: Any) -> bool:
    if field in STYLE_FIELDS and field != "voice_color":
        return (
            isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(expected, (int, float))
            and math.isclose(float(actual), float(expected), rel_tol=1e-6, abs_tol=1e-6)
        )
    if field == "expand_repeats":
        return isinstance(actual, bool) and actual == expected
    if field == "seconds":
        return (
            isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(expected, (int, float))
            and math.isclose(float(actual), float(expected), rel_tol=1e-3, abs_tol=0.05)
        )
    if field == "credits":
        return (
            isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and float(actual).is_integer() and int(actual) == expected
        )
    return actual == expected


def quote_presentation_problems(reply: Any, quote: Dict[str, Any]) -> List[str]:
    """What keeps a reply from presenting this quote; empty when it presents it exactly."""
    if not isinstance(reply, dict):
        return ["the reply is not a JSON object"]
    presentation = reply.get("quote_presentation")
    if not isinstance(presentation, dict):
        return ["quote_presentation is missing"]
    problems: List[str] = []
    if presentation.get("quote_id") != quote.get("quote_id"):
        problems.append(f"quote_id must be {quote.get('quote_id')!r}")
    for key in ("intro", "estimate_note", "closing"):
        if not str(presentation.get(key) or "").strip():
            problems.append(f"{key} is missing")
    # Only a quote made without a credit lookup (the e2e credit bypass) has no
    # balance; there is nothing to check those rows against.
    unchecked_rows = {
        "available_credits": quote.get("available_credits") is None,
        "balance_after": quote.get("balance_after") is None,
    }
    for (row_name, field), expected in _expected_copies(quote).items():
        row = presentation.get(row_name)
        if unchecked_rows.get(row_name):
            continue
        if not isinstance(row, dict):
            problems.append(f"{row_name} is missing")
            continue
        if not str(row.get("label") or "").strip():
            problems.append(f"{row_name}.label is missing")
        display_field = dict(_ROWS)[row_name]
        if display_field == "display" and not str(row.get("display") or "").strip():
            problems.append(f"{row_name}.display is missing")
        if not _same(field, row.get(field), expected):
            problems.append(f"{row_name}.{field} must be {expected!r}")
    problems.extend(_style_problems(presentation.get("voice_style"), _quote_style(quote)))
    if quote.get("takes_removed_from_player") and not str(
        presentation.get("takes_removed_note") or ""
    ).strip():
        problems.append("takes_removed_note is missing")
    return problems


def _style_problems(row: Any, style: Dict[str, Any]) -> List[str]:
    """The style row shows exactly the quote's style inputs, or is absent without any."""
    if row is None:
        return ["voice_style is missing"] if style else []
    if not isinstance(row, dict):
        return ["voice_style is not an object"]
    problems = []
    if style:
        if not str(row.get("label") or "").strip():
            problems.append("voice_style.label is missing")
        if not str(row.get("display") or "").strip():
            problems.append("voice_style.display is missing")
    for key in STYLE_FIELDS:
        if key in style:
            if not _same(key, row.get(key), style[key]):
                problems.append(f"voice_style.{key} must be {style[key]!r}")
        elif row.get(key) is not None:
            problems.append(f"voice_style.{key} is not in the quote")
    return problems


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_quote_presentation(presentation: Dict[str, Any]) -> str:
    """The chat text for a checked presentation: intro, the quote table, notes, closing."""
    rows = []
    for row_name, value_field in _ROWS:
        row = presentation.get(row_name)
        if not isinstance(row, dict):
            continue
        value = row.get(value_field)
        if row_name == "instrumental_credits" and str(row.get("note") or "").strip():
            value = f"{value} ({row['note'].strip()})"
        rows.append(f"| {_cell(row.get('label'))} | {_cell(value)} |")
    table = ["| Attribute | Value |", "| --- | --- |", *rows]
    paragraphs = [str(presentation.get("intro") or "").strip(), "\n".join(table)]
    for key in ("estimate_note", "takes_removed_note", "closing"):
        text = str(presentation.get(key) or "").strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def presentation_for_quote(quote: Dict[str, Any]) -> Dict[str, Any]:
    """A plain, valid presentation of a quote, for offline and test LLM clients."""
    copies = _expected_copies(quote)
    labels = {
        "part": "Vocal part", "lyrics": "Lyrics", "language": "Language", "voice": "AI voice",
        "repeats": "Repeats", "duration": "Estimated duration", "vocal_credits": "Vocal credits",
        "instrumental_credits": "Instrumental credits", "total_credits": "Total estimated credits",
        "available_credits": "Credits available", "balance_after": "Balance after this take",
    }
    presentation: Dict[str, Any] = {
        "quote_id": quote.get("quote_id"),
        "intro": "Here is the quote for this take.",
        "estimate_note": "Credits are estimates; the final charge follows the generated audio's length.",
        "closing": "Shall I go ahead?",
    }
    for (row_name, field), value in copies.items():
        if value is None and row_name in {"available_credits", "balance_after"}:
            continue
        row = presentation.setdefault(row_name, {"label": labels[row_name]})
        row[field] = value
        if dict(_ROWS)[row_name] == "display":
            row["display"] = (
                ("With repeats" if value else "Written order") if field == "expand_repeats"
                else f"{value} seconds" if field == "seconds" else str(value)
            )
    style = _quote_style(quote)
    if style:
        presentation["voice_style"] = {
            "label": "Voice style",
            "display": ", ".join(f"{key} {value}" for key, value in style.items()),
            **style,
        }
    if quote.get("takes_removed_from_player"):
        presentation["takes_removed_note"] = "This take replaces takes in the player."
    return {"quote_presentation": presentation}
