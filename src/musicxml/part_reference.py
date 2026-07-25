from __future__ import annotations

"""Resolve parser-visible score parts to their raw MusicXML counterparts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from xml.etree import ElementTree

from music21 import converter, stream

from src.musicxml.io import read_musicxml_content


@dataclass(frozen=True)
class PartReference:
    """One public parsed-score part and the raw MusicXML part it represents."""

    raw_part_id: str
    raw_part_index: int
    parser_part_index: int
    parser_part_id: str
    parser_part_name: str


@dataclass(frozen=True)
class _RawMusicXmlPart:
    part_id: str
    part_name: str
    is_multistaff: bool


def load_musicxml_score(path: str | Path) -> stream.Score:
    """Load MusicXML via the bounded reader when the source is an MXL archive."""
    source_path = Path(path)
    if source_path.suffix.lower() != ".mxl":
        return converter.parse(str(source_path))
    return converter.parseData(read_musicxml_content(source_path), format="musicxml")


def map_parser_part_indices_to_raw_part_ids(
    path: str | Path, *, score: Optional[stream.Score] = None
) -> Dict[int, str]:
    """Return raw part IDs for parser-visible indices, including expanded staffs."""
    source_path = Path(path)
    parsed_score = score if score is not None else load_musicxml_score(source_path)
    raw_parts = _raw_musicxml_parts(source_path)
    return _map_score_parts_to_raw_part_ids(parsed_score, raw_parts)


def resolve_part_reference(
    *,
    part_id: str,
    score: Optional[Mapping[str, Any]] = None,
    source_path: Optional[str | Path] = None,
) -> PartReference:
    """Resolve a public parser-visible part ID to parser and raw identities.

    Prefer the active parsed score, whose parts carry ``raw_part_id``. The
    MusicXML path is a fallback for direct transform callers without that score.
    Public callers never select by positional index.
    """
    requested_part_id = str(part_id or "").strip()
    if not requested_part_id:
        raise ValueError("part_id must not be empty.")
    if isinstance(score, Mapping):
        score_parts = score.get("parts")
        if isinstance(score_parts, list):
            for parser_part_index, part in enumerate(score_parts):
                if not isinstance(part, Mapping):
                    continue
                parser_part_id = str(part.get("part_id") or "").strip()
                if parser_part_id != requested_part_id:
                    continue
                raw_part_id = str(part.get("raw_part_id") or parser_part_id).strip()
                if not raw_part_id:
                    raise ValueError(f"part_id has no raw MusicXML reference: {part_id}")
                return PartReference(
                    raw_part_id=raw_part_id,
                    raw_part_index=-1,
                    parser_part_index=parser_part_index,
                    parser_part_id=parser_part_id,
                    parser_part_name=str(part.get("part_name") or "").strip(),
                )
        raise ValueError(f"Unknown part_id: {part_id}")
    if source_path is None:
        raise ValueError("score or source_path is required to resolve part_id.")

    source_path = Path(source_path)
    parsed_score = load_musicxml_score(source_path)
    raw_parts = _raw_musicxml_parts(source_path)
    raw_part_ids_by_index = _map_score_parts_to_raw_part_ids(parsed_score, raw_parts)
    raw_indices = {part.part_id: index for index, part in enumerate(raw_parts)}
    for parser_part_index, part in enumerate(parsed_score.parts):
        parser_part_id = str(part.id or "").strip()
        if requested_part_id != parser_part_id:
            continue
        raw_part_id = raw_part_ids_by_index.get(parser_part_index)
        if raw_part_id is not None:
            return _reference_for_index(
                parsed_score,
                raw_part_id=raw_part_id,
                raw_part_index=raw_indices[raw_part_id],
                parser_part_index=parser_part_index,
            )
    raise ValueError(f"Unknown parser-visible part_id: {part_id}")


def _reference_for_index(
    score: stream.Score,
    *,
    raw_part_id: str,
    raw_part_index: int,
    parser_part_index: int,
) -> PartReference:
    part = score.parts[parser_part_index]
    return PartReference(
        raw_part_id=raw_part_id,
        raw_part_index=raw_part_index,
        parser_part_index=parser_part_index,
        parser_part_id=str(part.id or "").strip(),
        parser_part_name=str(part.partName or "").strip(),
    )


def _raw_musicxml_parts(path: Path) -> List[_RawMusicXmlPart]:
    root = ElementTree.fromstring(read_musicxml_content(path))
    names_by_id: Dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) != "score-part":
            continue
        part_id = str(element.attrib.get("id") or "").strip()
        if not part_id:
            continue
        names_by_id[part_id] = next(
            (
                (child.text or "").strip()
                for child in element
                if _local_name(child.tag) == "part-name"
            ),
            "",
        )
    parts: List[_RawMusicXmlPart] = []
    for element in root:
        if _local_name(element.tag) != "part":
            continue
        part_id = str(element.attrib.get("id") or "").strip()
        if not part_id:
            continue
        staff_numbers = [
            int(text)
            for node in element.iter()
            if _local_name(node.tag) in {"staves", "staff"}
            if (text := str(node.text or "").strip()).isdigit()
        ]
        parts.append(
            _RawMusicXmlPart(
                part_id=part_id,
                part_name=names_by_id.get(part_id, ""),
                is_multistaff=max(staff_numbers, default=1) > 1,
            )
        )
    return parts


def _map_score_parts_to_raw_part_ids(
    score: stream.Score, raw_parts: List[_RawMusicXmlPart]
) -> Dict[int, str]:
    raw_part_names = {part.part_id: part.part_name for part in raw_parts}
    raw_ids_by_name: Dict[str, List[str]] = {}
    for raw_part in raw_parts:
        normalized_name = str(raw_part.part_name or "").strip()
        if normalized_name:
            raw_ids_by_name.setdefault(normalized_name, []).append(raw_part.part_id)

    result: Dict[int, str] = {}
    for index, part in enumerate(score.parts):
        parser_part_id = str(part.id or "").strip()
        parser_part_name = str(part.partName or "").strip()
        raw_part_id = parser_part_id if parser_part_id in raw_part_names else ""
        if not raw_part_id:
            candidates = raw_ids_by_name.get(parser_part_name, [])
            if len(candidates) == 1:
                raw_part_id = candidates[0]
        if not raw_part_id:
            # music21 may expand a raw multi-staff part into separate parser
            # parts. Their IDs retain the raw ID as a prefix, but the suffix is
            # library-specific, so use source structure rather than a suffix.
            candidates = [
                raw_part.part_id
                for raw_part in raw_parts
                if raw_part.is_multistaff
                and parser_part_id.startswith(raw_part.part_id)
                and parser_part_id != raw_part.part_id
                and any(character.isdigit() for character in parser_part_id[len(raw_part.part_id) :])
            ]
            if candidates:
                raw_part_id = max(candidates, key=len)
        if raw_part_id:
            result[index] = raw_part_id
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
