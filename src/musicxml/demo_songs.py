"""Identify the app's built-in demo songs from a marker inside their MusicXML.

A demo song carries
``<identification><miscellaneous><miscellaneous-field name="sightsinger-demo">``
with its id. The marker is the only source of truth: titles and file names are
user-editable and not reliable. Derived scores (solfege lines, part splitting,
name edits) edit the raw MusicXML and keep the marker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from src.musicxml.io import read_musicxml_content

DEMO_SONG_MARKER_FIELD = "sightsinger-demo"
DEMO_SONG_IDS = frozenset({"amazing-grace", "happy-birthday"})


def read_demo_song_id(path: Path) -> Optional[str]:
    """Return the demo song id marked in the MusicXML, or None for any other score."""
    try:
        root = ElementTree.fromstring(read_musicxml_content(Path(path)))
    except (ElementTree.ParseError, OSError, ValueError):
        return None
    for field in root.iterfind("./identification/miscellaneous/miscellaneous-field"):
        if field.get("name") == DEMO_SONG_MARKER_FIELD:
            value = (field.text or "").strip()
            return value if value in DEMO_SONG_IDS else None
    return None
