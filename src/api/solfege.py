from __future__ import annotations

"""API boundary for deterministic generated-solfege MusicXML transforms."""

from pathlib import Path
from typing import Any, Dict, Optional

from src.api.score import parse_score
from src.musicxml.solfege import (
    GENERATED_LYRIC_NAME,
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
    unselected = parse_score(output_path, expand_repeats=False)
    selection = _find_generated_selection(unselected, result.get("target"))
    if selection is None:
        raise ValueError("Generated solfege lyric selection was not found in output score.")
    result["lyric_selection"] = selection
    parsed = parse_score(
        output_path,
        lyric_selection=selection,
        expand_repeats=False,
    )
    return _attach_parsed_score(result, parsed)


def modify_solfege_settings(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    settings: Optional[Dict[str, Any]] = None,
    selected_verse_number: Optional[str | int] = None,
    selected_lyric_selection: Optional[Dict[str, str]] = None,
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
        lyric_selection=selected_lyric_selection,
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


def _find_generated_selection(
    parsed: Dict[str, Any], target: Any
) -> Optional[Dict[str, str]]:
    part_id = target.get("part_id") if isinstance(target, dict) else None
    part_index = target.get("part_index") if isinstance(target, dict) else None
    summary = parsed.get("score_summary") if isinstance(parsed, dict) else None
    for index, part in enumerate((summary or {}).get("parts") or []):
        if part_index is not None and index != part_index:
            continue
        if part_index is None and part_id is not None and part.get("part_id") != part_id:
            continue
        for selection in part.get("lyric_selections") or []:
            if selection.get("name") == GENERATED_LYRIC_NAME:
                return {
                    "id": str(selection["id"]),
                    "number": str(selection["number"]),
                    "name": str(selection["name"]),
                }
    return None
