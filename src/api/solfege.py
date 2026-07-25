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
from src.musicxml.part_reference import resolve_part_reference


def add_solfege_lyric_verse(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    part_id: str,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create, parse, and return a derived score with one new solfege verse."""
    output_path = Path(output_musicxml_path)
    reference = resolve_part_reference(
        part_id=part_id,
        source_path=source_musicxml_path,
    )
    result = transform_add_solfege_lyric_verse(
        Path(source_musicxml_path),
        output_path,
        part_id=reference.raw_part_id,
        settings=settings,
    )
    if result.get("status") != "ready":
        return result
    if isinstance(result.get("target"), dict):
        result["target"] = {
            **result["target"],
            "part_id": reference.parser_part_id,
            "raw_part_id": reference.raw_part_id,
            "part_index": reference.parser_part_index,
        }
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
        raw_part_id = part.get("raw_part_id") or part.get("part_id")
        parsed_part_id = part.get("part_id")
        matches = False
        if part_index is not None and index == part_index:
            matches = True
        elif part_id is not None and (raw_part_id == part_id or parsed_part_id == part_id):
            matches = True

        if matches:
            for selection in part.get("lyric_selections") or []:
                if selection.get("name") == GENERATED_LYRIC_NAME:
                    return {
                        "id": str(selection["id"]),
                        "number": str(selection["number"]),
                        "name": str(selection["name"]),
                    }
    return None
