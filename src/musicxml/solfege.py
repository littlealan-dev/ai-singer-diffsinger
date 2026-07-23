from __future__ import annotations

"""Deterministic MusicXML solfege lyric generation and rewriting."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from xml.etree import ElementTree

from src.musicxml.io import read_musicxml_content


GENERATED_LYRIC_NAME = "SightSinger Solfege"
GENERATED_LYRIC_NUMBER = "SSSolfege"
DEFAULT_SOLFEGE_SETTINGS: Dict[str, str] = {
    "system": "movable_do",
    "mode": "major",
}
VALID_SOLFEGE_SYSTEMS = {"movable_do", "fixed_do"}
VALID_SOLFEGE_MODES = {"major", "minor_la_based", "minor_do_based"}

_STEP_INDEX = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
_STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_MAJOR_TONIC_STEPS = {
    -7: "C", -6: "G", -5: "D", -4: "A", -3: "E", -2: "B", -1: "F",
    0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F", 7: "C",
}
_MINOR_TONIC_STEPS = {
    -7: "A", -6: "E", -5: "B", -4: "F", -3: "C", -2: "G", -1: "D",
    0: "A", 1: "E", 2: "B", 3: "F", 4: "C", 5: "G", 6: "D", 7: "A",
}
_PC_FALLBACK = ("do", "di", "re", "ri", "mi", "fa", "fi", "so", "si", "la", "li", "ti")
_FIXED_BY_SPELLING = {
    ("C", 0): "do", ("C", 1): "di",
    ("D", -1): "ra", ("D", 0): "re", ("D", 1): "ri",
    ("E", -1): "me", ("E", 0): "mi",
    ("F", 0): "fa", ("F", 1): "fi",
    ("G", -1): "se", ("G", 0): "so", ("G", 1): "si",
    ("A", -1): "le", ("A", 0): "la", ("A", 1): "li",
    ("B", -1): "te", ("B", 0): "ti",
}
_MAJOR_BASE = {1: "do", 2: "re", 3: "mi", 4: "fa", 5: "so", 6: "la", 7: "ti"}
_MAJOR_SEMITONES = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}
_MINOR_DO_BASE = {1: "do", 2: "re", 3: "me", 4: "fa", 5: "so", 6: "le", 7: "te"}
_MINOR_DO_SEMITONES = {1: 0, 2: 2, 3: 3, 4: 5, 5: 7, 6: 8, 7: 10}
_ALTERED = {
    (1, 1): "di",
    (2, -1): "ra", (2, 1): "ri",
    (3, -1): "me", (3, 1): "mi",
    (4, 1): "fi",
    (5, -1): "se", (5, 1): "si",
    (6, -1): "le", (6, 1): "la",
    (7, -1): "te", (7, 1): "ti",
}


@dataclass(frozen=True)
class SolfegeSettings:
    system: str = "movable_do"
    mode: str = "major"

    @classmethod
    def from_mapping(cls, value: Optional[Dict[str, Any]]) -> "SolfegeSettings":
        payload = value or DEFAULT_SOLFEGE_SETTINGS
        system = str(payload.get("system") or "movable_do")
        mode = str(payload.get("mode") or "major")
        if system not in VALID_SOLFEGE_SYSTEMS:
            raise ValueError(f"Unsupported solfege system: {system}")
        if mode not in VALID_SOLFEGE_MODES:
            raise ValueError(f"Unsupported solfege mode: {mode}")
        return cls(system=system, mode=mode)

    def as_dict(self) -> Dict[str, str]:
        return {"system": self.system, "mode": self.mode}


def add_solfege_lyric_verse(
    source_path: Path,
    output_path: Path,
    *,
    part_id: Optional[str] = None,
    part_index: Optional[int] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one generated solfege lyric verse to a monophonic MusicXML part."""
    root = _read_root(source_path)
    try:
        target, resolved_index = _select_part(root, part_id=part_id, part_index=part_index)
    except ValueError as exc:
        return _action_required(
            "target_not_found",
            "The selected score part could not be found.",
            {
                "part_id": part_id,
                "part_index": part_index,
                "detail": str(exc),
            },
        )
    resolved_part_id = str(target.attrib.get("id") or "")
    complexity = _part_complexity(target)
    if complexity:
        return _action_required(
            "complex_target_requires_preparation",
            "The selected target must be prepared as one clean singing line before solfege can be added.",
            {"part_id": resolved_part_id, **complexity},
        )
    existing_generated = _generated_verse_numbers(target)
    if existing_generated:
        return _action_required(
            "solfege_verse_already_exists",
            "The selected part already contains a generated solfege verse.",
            {"part_id": resolved_part_id, "verse_numbers": sorted(existing_generated, key=_verse_sort_key)},
        )

    # Keep this system-owned raw identifier independent of exporter-specific
    # lyric-number conventions. The parser selects the exact number/name pair.
    verse_number = GENERATED_LYRIC_NUMBER
    normalized_settings = SolfegeSettings.from_mapping(settings)
    notes_annotated, notes_extended = _append_generated_lyrics(
        target,
        verse_number=verse_number,
        settings=normalized_settings,
    )
    if notes_annotated == 0:
        return _action_required(
            "no_pitched_notes",
            "The selected target has no pitched notes that can receive solfege.",
            {"part_id": resolved_part_id},
        )
    _write_root(root, output_path)
    return {
        "status": "ready",
        "derived_musicxml_path": str(output_path),
        "target": {
            "part_id": resolved_part_id,
            "part_index": resolved_index,
            "part_name": _part_name(root, resolved_part_id),
        },
        "new_verse_number": verse_number,
        "selected_verse_number": verse_number,
        "settings": normalized_settings.as_dict(),
        "notes_annotated": notes_annotated,
        "notes_extended": notes_extended,
        "warnings": [],
    }


def modify_generated_solfege_verses(
    source_path: Path,
    output_path: Path,
    *,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Rewrite all SightSinger-generated solfege verses using canonical settings."""
    root = _read_root(source_path)
    normalized_settings = SolfegeSettings.from_mapping(settings)
    updated: list[Dict[str, Any]] = []
    for part_index, part in enumerate(_parts(root)):
        part_id = str(part.attrib.get("id") or "")
        for verse_number in sorted(_generated_verse_numbers(part), key=_verse_sort_key):
            notes_updated = _rewrite_generated_verse(
                part,
                verse_number=verse_number,
                settings=normalized_settings,
            )
            updated.append(
                {
                    "part_id": part_id,
                    "part_index": part_index,
                    "verse_number": verse_number,
                    "notes_updated": notes_updated,
                }
            )
    _write_root(root, output_path)
    return {
        "status": "ready",
        "derived_musicxml_path": str(output_path),
        "settings": normalized_settings.as_dict(),
        "updated_generated_verses": updated,
        "warnings": [],
    }


def _append_generated_lyrics(
    part: ElementTree.Element,
    *,
    verse_number: str,
    settings: SolfegeSettings,
) -> tuple[int, int]:
    annotated = 0
    extended = 0
    fifths = 0
    for measure in _children(part, "measure"):
        fifths = _measure_fifths(measure, fifths)
        for note in _children(measure, "note"):
            if _child(note, "rest") is not None or _child(note, "grace") is not None:
                continue
            pitch = _note_pitch(note)
            if pitch is None:
                continue
            lyric = ElementTree.Element(_qualified(note, "lyric"), {
                "number": verse_number,
                "name": GENERATED_LYRIC_NAME,
            })
            if _is_tie_continuation(note):
                ElementTree.SubElement(lyric, _qualified(note, "extend"))
                extended += 1
            else:
                syllabic = ElementTree.SubElement(lyric, _qualified(note, "syllabic"))
                syllabic.text = "single"
                text = ElementTree.SubElement(lyric, _qualified(note, "text"))
                text.text = _solfege_for_pitch(*pitch, fifths=fifths, settings=settings)
                annotated += 1
            note.append(lyric)
    return annotated, extended


def _rewrite_generated_verse(
    part: ElementTree.Element,
    *,
    verse_number: str,
    settings: SolfegeSettings,
) -> int:
    updated = 0
    fifths = 0
    for measure in _children(part, "measure"):
        fifths = _measure_fifths(measure, fifths)
        for note in _children(measure, "note"):
            pitch = _note_pitch(note)
            if pitch is None:
                continue
            for lyric in _children(note, "lyric"):
                if not _is_generated_lyric(lyric, verse_number):
                    continue
                text = _child(lyric, "text")
                if text is None:
                    continue
                text.text = _solfege_for_pitch(*pitch, fifths=fifths, settings=settings)
                updated += 1
    return updated


def _solfege_for_pitch(
    step: str,
    alter: int,
    *,
    fifths: int,
    settings: SolfegeSettings,
) -> str:
    pitch_pc = (_STEP_PC[step] + alter) % 12
    if settings.system == "fixed_do":
        return _FIXED_BY_SPELLING.get((step, alter), _PC_FALLBACK[pitch_pc])

    normalized_fifths = max(-7, min(7, fifths))
    major_tonic_pc = (7 * normalized_fifths) % 12
    if settings.mode == "minor_do_based":
        tonic_pc = (major_tonic_pc + 9) % 12
        tonic_step = _MINOR_TONIC_STEPS[normalized_fifths]
        base = _MINOR_DO_BASE
        expected = _MINOR_DO_SEMITONES
    else:
        tonic_pc = major_tonic_pc
        tonic_step = _MAJOR_TONIC_STEPS[normalized_fifths]
        base = _MAJOR_BASE
        expected = _MAJOR_SEMITONES
    degree = ((_STEP_INDEX[step] - _STEP_INDEX[tonic_step]) % 7) + 1
    actual = (pitch_pc - tonic_pc) % 12
    delta = _signed_semitone_delta(actual - expected[degree])
    if delta == 0:
        return base[degree]
    altered = _ALTERED.get((degree, delta))
    if altered:
        return altered
    return _PC_FALLBACK[actual]


def _signed_semitone_delta(value: int) -> int:
    normalized = value % 12
    return normalized - 12 if normalized > 6 else normalized


def _part_complexity(part: ElementTree.Element) -> Dict[str, Any]:
    voices: set[str] = set()
    chord_measures: list[str] = []
    for measure in _children(part, "measure"):
        has_chord = False
        for note in _children(measure, "note"):
            if _child(note, "rest") is not None:
                continue
            voice = _child(note, "voice")
            voices.add((voice.text or "1").strip() if voice is not None else "1")
            has_chord = has_chord or _child(note, "chord") is not None
        if has_chord:
            chord_measures.append(str(measure.attrib.get("number") or ""))
    diagnostics: Dict[str, Any] = {}
    if len(voices) > 1:
        diagnostics["voices"] = sorted(voices)
    if chord_measures:
        diagnostics["chord_measures"] = chord_measures
    return diagnostics


def _next_verse_number(part: ElementTree.Element) -> str:
    values = [int(value) for value in _all_verse_numbers(part) if value.isdigit() and int(value) > 0]
    return str(max(values, default=0) + 1)


def _all_verse_numbers(part: ElementTree.Element) -> set[str]:
    values: set[str] = set()
    for lyric in _descendants(part, "lyric"):
        values.add(str(lyric.attrib.get("number") or "1").strip() or "1")
    return values


def _generated_verse_numbers(part: ElementTree.Element) -> set[str]:
    return {
        str(lyric.attrib.get("number") or "1").strip() or "1"
        for lyric in _descendants(part, "lyric")
        if lyric.attrib.get("name") == GENERATED_LYRIC_NAME
    }


def _is_generated_lyric(lyric: ElementTree.Element, verse_number: str) -> bool:
    number = str(lyric.attrib.get("number") or "1").strip() or "1"
    return lyric.attrib.get("name") == GENERATED_LYRIC_NAME and number == verse_number


def _note_pitch(note: ElementTree.Element) -> Optional[tuple[str, int]]:
    pitch = _child(note, "pitch")
    if pitch is None:
        return None
    step_element = _child(pitch, "step")
    if step_element is None or not step_element.text:
        return None
    step = step_element.text.strip().upper()
    if step not in _STEP_PC:
        return None
    alter_element = _child(pitch, "alter")
    try:
        alter = int(float(alter_element.text)) if alter_element is not None and alter_element.text else 0
    except ValueError:
        alter = 0
    return step, alter


def _is_tie_continuation(note: ElementTree.Element) -> bool:
    tie_types = {
        str(tie.attrib.get("type") or "").strip()
        for tie in _children(note, "tie")
    }
    return "stop" in tie_types


def _measure_fifths(measure: ElementTree.Element, current: int) -> int:
    for attributes in _children(measure, "attributes"):
        key = _child(attributes, "key")
        if key is None:
            continue
        fifths = _child(key, "fifths")
        if fifths is not None and fifths.text:
            try:
                return int(fifths.text.strip())
            except ValueError:
                return current
    return current


def _select_part(
    root: ElementTree.Element,
    *,
    part_id: Optional[str],
    part_index: Optional[int],
) -> tuple[ElementTree.Element, int]:
    parts = list(_parts(root))
    if (part_id is None) == (part_index is None):
        raise ValueError("Exactly one of part_id or part_index is required.")
    if part_id is not None:
        requested_part_id = part_id.strip()
        for index, part in enumerate(parts):
            if part.attrib.get("id") == requested_part_id:
                return part, index
        requested_name = requested_part_id.casefold()
        name_matches = [
            (index, part)
            for index, part in enumerate(parts)
            if (_part_name(root, str(part.attrib.get("id") or "")) or "").casefold()
            == requested_name
        ]
        if len(name_matches) == 1:
            return name_matches[0][1], name_matches[0][0]
        if len(name_matches) > 1:
            raise ValueError(f"Ambiguous part name: {part_id}")
        raise ValueError(f"Unknown part_id or part name: {part_id}")
    assert part_index is not None
    if part_index < 0 or part_index >= len(parts):
        raise ValueError(f"part_index out of range: {part_index}")
    return parts[part_index], part_index


def _part_name(root: ElementTree.Element, part_id: str) -> Optional[str]:
    for score_part in _descendants(root, "score-part"):
        if score_part.attrib.get("id") != part_id:
            continue
        name = _child(score_part, "part-name")
        return (name.text or "").strip() or None if name is not None else None
    return None


def _read_root(path: Path) -> ElementTree.Element:
    try:
        root = ElementTree.fromstring(read_musicxml_content(path))
        if root.tag.startswith("{"):
            namespace = root.tag.split("}", 1)[0][1:]
            ElementTree.register_namespace("", namespace)
        return root
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid MusicXML: {exc}") from exc


def _write_root(root: ElementTree.Element, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _parts(root: ElementTree.Element) -> Iterable[ElementTree.Element]:
    return _children(root, "part")


def _children(element: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child.tag) == local_name]


def _child(element: ElementTree.Element, local_name: str) -> Optional[ElementTree.Element]:
    return next((child for child in element if _local_name(child.tag) == local_name), None)


def _descendants(element: ElementTree.Element, local_name: str) -> Iterable[ElementTree.Element]:
    return (candidate for candidate in element.iter() if _local_name(candidate.tag) == local_name)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _qualified(element: ElementTree.Element, local_name: str) -> str:
    if element.tag.startswith("{"):
        namespace = element.tag.split("}", 1)[0] + "}"
        return namespace + local_name
    return local_name


def _verse_sort_key(value: str) -> tuple[int, object]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _action_required(code: str, message: str, diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "action_required",
        "action": code,
        "code": code,
        "message": message,
        "diagnostics": diagnostics,
    }
