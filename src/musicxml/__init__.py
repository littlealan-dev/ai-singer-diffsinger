"""MusicXML parsing package exports.

The parser (and music21) loads on first use of these names, so a service that only reads
MusicXML files (the share service) imports src.musicxml.io without loading music21.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "NoteEvent",
    "PartData",
    "ScoreData",
    "TempoEvent",
    "parse_musicxml",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import parser

        return getattr(parser, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
