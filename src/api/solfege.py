from __future__ import annotations

"""API boundary for deterministic generated-solfege MusicXML transforms."""

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.api.score import parse_score
from src.musicxml.solfege import (
    GENERATED_LYRIC_NAME,
    add_solfege_lyric_verses as transform_add_solfege_lyric_verses,
    modify_generated_solfege_verses,
    regenerate_generated_solfege_verses,
)
from src.musicxml.part_reference import resolve_part_reference


def add_solfege_lyric_verse(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    part_ids: Iterable[str],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Add a generated solfege line to each listed part in one pass.

    ``part_ids`` are parser-visible part IDs. Staves of one MusicXML part are
    processed once and reported under each requested ID. Returns
    ``status: "ready"`` with ``completed_targets``, ``already_present`` and
    ``skipped`` when any part has a line afterwards, or ``action_required``
    for the first skipped part when none does.
    """
    requested = [str(part_id) for part_id in part_ids]
    if not requested:
        raise ValueError("add_solfege_lyric_verse needs at least one part.")
    output_path = Path(output_musicxml_path)
    references: Dict[str, Any] = {}
    skipped: list[Dict[str, Any]] = []
    for part_id in dict.fromkeys(requested):
        try:
            references[part_id] = resolve_part_reference(
                part_id=part_id, source_path=source_musicxml_path
            )
        except ValueError as exc:
            skipped.append({
                "part_id": part_id,
                "code": "target_not_found",
                "message": "The selected score part could not be found.",
                "diagnostics": {"part_id": part_id, "detail": str(exc)},
            })
    result = transform_add_solfege_lyric_verses(
        Path(source_musicxml_path),
        output_path,
        raw_part_ids=[reference.raw_part_id for reference in references.values()],
        settings=settings,
    )
    outcomes: Dict[str, tuple[str, Dict[str, Any]]] = {}
    for outcome in ("completed", "already_present", "skipped"):
        for entry in result[outcome]:
            outcomes[str(entry["part_id"])] = (outcome, entry)

    completed_targets: list[Dict[str, Any]] = []
    already_present: list[Dict[str, Any]] = []
    for part_id, reference in references.items():
        outcome, entry = outcomes[reference.raw_part_id]
        if outcome == "skipped":
            skipped.append({**entry, "part_id": part_id, "raw_part_id": reference.raw_part_id})
            continue
        target = {
            "part_id": reference.parser_part_id,
            "raw_part_id": reference.raw_part_id,
            "part_index": reference.parser_part_index,
            "part_name": reference.parser_part_name,
        }
        (completed_targets if outcome == "completed" else already_present).append(target)

    if not completed_targets and not already_present:
        first = skipped[0]
        return {
            "status": "action_required",
            "action": first["code"],
            "code": first["code"],
            "message": first["message"],
            "diagnostics": first.get("diagnostics") or {},
            "skipped": skipped,
        }

    payload: Dict[str, Any] = {
        "status": "ready",
        "completed_targets": completed_targets,
        "already_present": already_present,
        "skipped": skipped,
        "new_verse_number": result["new_verse_number"],
        "selected_verse_number": result["new_verse_number"],
        "settings": result["settings"],
        "warnings": [],
    }
    score_path = output_path if completed_targets else Path(source_musicxml_path)
    unselected = parse_score(score_path, expand_repeats=False)
    for target in completed_targets + already_present:
        selection = _find_generated_selection(unselected, target)
        if selection is None:
            raise ValueError(f"Generated solfege lyric selection was not found for {target['part_id']}.")
        target["lyric_selection"] = selection
    if not completed_targets:
        return payload
    payload["derived_musicxml_path"] = result["derived_musicxml_path"]
    # The score opens on the first new line; a quote for another part passes its own selection.
    parsed = parse_score(
        output_path,
        lyric_selection=completed_targets[0]["lyric_selection"],
        expand_repeats=False,
    )
    return _attach_parsed_score(payload, parsed)


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


def regenerate_solfege_verses(
    source_musicxml_path: str | Path,
    output_musicxml_path: str | Path,
    *,
    part_ids: Iterable[str],
    settings: Optional[Dict[str, Any]] = None,
    selected_verse_number: Optional[str | int] = None,
    selected_lyric_selection: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Generate the given parts' solfege verses again and return the reparsed score."""
    output_path = Path(output_musicxml_path)
    result = regenerate_generated_solfege_verses(
        Path(source_musicxml_path),
        output_path,
        part_ids=part_ids,
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
