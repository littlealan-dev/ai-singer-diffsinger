from __future__ import annotations

"""API boundary for deterministic generated-solfege MusicXML transforms."""

from pathlib import Path
from typing import Any, Dict, Optional

from src.api.score import parse_score
from src.musicxml.solfege import (
    add_solfege_lyric_verse as transform_add_solfege_lyric_verse,
    modify_generated_solfege_verses,
)


def add_solfege_lyric_verse(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    part_id: Optional[str] = None,
    part_index: Optional[int] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create, parse, and return a derived score with one new solfege verse."""
    output_path = Path(output_musicxml_path)
    result = transform_add_solfege_lyric_verse(
        Path(source_musicxml_path),
        output_path,
        part_id=part_id,
        part_index=part_index,
        settings=settings,
    )
    if result.get("status") != "ready":
        return result
    selected_verse = str(result["new_verse_number"])
    parsed = parse_score(output_path, verse_number=selected_verse, expand_repeats=False)
    return _attach_parsed_score(result, parsed)


def modify_solfege_settings(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    settings: Optional[Dict[str, Any]] = None,
    selected_verse_number: Optional[str | int] = None,
) -> Dict[str, Any]:
    """Rewrite every generated solfege verse and return the reparsed score."""
    output_path = Path(output_musicxml_path)
    result = modify_generated_solfege_verses(
        Path(source_musicxml_path),
        output_path,
        settings=settings,
    )
    parsed = parse_score(
        output_path,
        verse_number=selected_verse_number,
        expand_repeats=False,
    )
    return _attach_parsed_score(result, parsed)


def _attach_parsed_score(result: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(result)
    summary = parsed.get("score_summary") if isinstance(parsed, dict) else None
    score = dict(parsed)
    score.pop("score_summary", None)
    payload["derived_score"] = score
    payload["score_summary"] = summary
    payload["derived_musicxml_path"] = str(Path(payload["derived_musicxml_path"]).resolve())
    return payload
