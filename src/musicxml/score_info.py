"""The credits a MusicXML file declares: subtitle, composer, lyricist, arranger and copyright.

Read from the XML directly so each element is read the same way, whatever music21 keeps.
Each field takes the first rule that gives text (see docs/specs/song-info-spec.md §3).

Typed creators (`<creator type=...>`) fill their fields. The printed composer, lyricist and
arranger boxes are free text whose box label says only where it sits on the page (one may read
"Arranged by ..." in the composer box), so their text goes, as printed, into `credits`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List

SCORE_INFO_LIMITS = {
    "subtitle": 200,
    "composer": 200,
    "lyricist": 200,
    "arranger": 200,
    "scoreCopyright": 300,
    "credits": 500,
}
PRINTED_CREDIT_TYPES = ("composer", "lyricist", "arranger")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> Iterable[ET.Element]:
    return (child for child in element if _local(child.tag) == name)


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _join(values: List[str], separator: str, limit: int) -> str:
    unique = list(dict.fromkeys(value for value in values if value))
    return separator.join(unique)[:limit].strip()


def score_info_from_musicxml(path: str | Path) -> Dict[str, str]:
    """Each score field's text, or "" when the file declares none."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {key: "" for key in SCORE_INFO_LIMITS}

    creators: Dict[str, List[str]] = {}
    rights: List[str] = []
    for identification in _children(root, "identification"):
        for creator in _children(identification, "creator"):
            creators.setdefault((creator.get("type") or "").strip().lower(), []).append(_text(creator))
        rights.extend(_text(element) for element in _children(identification, "rights"))

    credits: Dict[str, List[str]] = {}
    printed: List[str] = []
    for credit in _children(root, "credit"):
        kinds = [_text(kind).lower() for kind in _children(credit, "credit-type")]
        words = " ".join(_text(element) for element in _children(credit, "credit-words")).strip()
        for kind in kinds:
            credits.setdefault(kind, []).append(words)
        if words and any(kind in PRINTED_CREDIT_TYPES for kind in kinds):
            printed.append(words)

    work_title = ""
    for work in _children(root, "work"):
        work_title = _text(next(iter(_children(work, "work-title")), None)) or work_title
    movement_title = _text(next(iter(_children(root, "movement-title")), None))

    def first(*sources: List[str], separator: str = ", ", key: str) -> str:
        for values in sources:
            joined = _join(values, separator, SCORE_INFO_LIMITS[key])
            if joined:
                return joined
        return ""

    subtitle_fallback = [movement_title] if work_title and movement_title and movement_title != work_title else []
    info = {
        "subtitle": first(credits.get("subtitle", []), subtitle_fallback, key="subtitle"),
        "composer": first(creators.get("composer", []), key="composer"),
        "lyricist": first(creators.get("lyricist", []), creators.get("poet", []), key="lyricist"),
        "arranger": first(creators.get("arranger", []), key="arranger"),
        "scoreCopyright": first(rights, credits.get("rights", []), separator="; ", key="scoreCopyright"),
    }
    # A printed box that only repeats a typed creator adds nothing.
    typed = {value.casefold() for key in ("composer", "lyricist", "arranger")
             for value in info[key].split(", ") if value}
    info["credits"] = _join([words for words in printed if words.casefold() not in typed],
                            " · ", SCORE_INFO_LIMITS["credits"])
    return info
