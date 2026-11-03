"""Bounded MusicXML evidence for planner v2 chooser requests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from src.api.score import _read_musicxml_content


class EvidenceUnavailable(RuntimeError):
    """The original score cannot safely supply the requested bars."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_CARRY = ("divisions", "key", "time", "staves", "clef", "transpose")
_LAYOUT_ELEMENTS = {"defaults", "credit", "identification", "print", "midi-instrument", "midi-device"}
_LAYOUT_ATTRIBUTES = {
    "default-x", "default-y", "relative-x", "relative-y", "width", "print-object",
    "bezier-x", "bezier-y",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute_key(element: ET.Element) -> tuple[str, str]:
    tag = _local(element.tag)
    number = element.get("number", "1") if tag in {"key", "time", "clef"} else ""
    return tag, number


def _carry_attributes(measure: ET.Element, state: dict[tuple[str, str], ET.Element]) -> None:
    if not state:
        return
    # A later attributes element can change the music midway through this bar.
    # The carried state must precede any note or direction in the kept bar.
    attributes = measure[0] if len(measure) and _local(measure[0].tag) == "attributes" else None
    if attributes is None:
        namespace = measure.tag.split("}", 1)[0] + "}" if "}" in measure.tag else ""
        attributes = ET.Element(namespace + "attributes")
        measure.insert(0, attributes)
    present = {_attribute_key(child) for child in attributes if _local(child.tag) in _CARRY}
    for key, value in sorted(state.items(), key=lambda item: (_CARRY.index(item[0][0]), item[0][1])):
        if key in present:
            continue
        rank = _CARRY.index(key[0])
        position = next((index for index, child in enumerate(attributes)
                         if _local(child.tag) in _CARRY
                         and _CARRY.index(_local(child.tag)) > rank), len(attributes))
        attributes.insert(position, deepcopy(value))


def _remove_layout(element: ET.Element) -> None:
    for key in _LAYOUT_ATTRIBUTES:
        element.attrib.pop(key, None)
    if element.text is not None and not element.text.strip():
        element.text = None
    if element.tail is not None and not element.tail.strip():
        element.tail = None
    for child in list(element):
        if _local(child.tag) in _LAYOUT_ELEMENTS:
            element.remove(child)
        else:
            _remove_layout(child)


def trim_musicxml(score: dict, raw_part_ids: list[str], measures: set[int]) -> str:
    """Return the requested original parts and bars, carrying prior musical attributes."""
    path = score.get("source_musicxml_path")
    if not path:
        raise EvidenceUnavailable("missing_path")
    if not raw_part_ids or not measures:
        raise EvidenceUnavailable("empty_selection")
    try:
        root = ET.fromstring(_read_musicxml_content(Path(path)))
    except Exception as exc:
        raise EvidenceUnavailable("unreadable_path") from exc
    if _local(root.tag) != "score-partwise":
        raise EvidenceUnavailable("invalid_document")

    wanted_parts = set(raw_part_ids)
    found_parts: set[str] = set()
    for child in list(root):
        tag = _local(child.tag)
        if tag == "part-list":
            for entry in list(child):
                if _local(entry.tag) != "score-part" or entry.get("id") not in wanted_parts:
                    child.remove(entry)
        elif tag == "part":
            raw_id = child.get("id")
            if raw_id not in wanted_parts:
                root.remove(child)
                continue
            if raw_id in found_parts:
                raise EvidenceUnavailable("duplicate_part")
            found_parts.add(raw_id)
            state: dict[tuple[str, str], ET.Element] = {}
            found_measures: set[int] = set()
            previous_kept = False
            position = 0
            for measure in list(child):
                if _local(measure.tag) != "measure":
                    continue
                position += 1
                try:
                    number = int(measure.get("number") or position)
                except (TypeError, ValueError) as exc:
                    raise EvidenceUnavailable("unreadable_measure_number") from exc
                if number in found_measures:
                    raise EvidenceUnavailable("duplicate_measure_number")
                found_measures.add(number)
                keep = number in measures
                if keep and not previous_kept:
                    _carry_attributes(measure, state)
                for attributes in (item for item in measure
                                   if _local(item.tag) == "attributes"):
                    for item in attributes:
                        if _local(item.tag) in _CARRY:
                            state[_attribute_key(item)] = deepcopy(item)
                if not keep:
                    child.remove(measure)
                previous_kept = keep
            if not measures.issubset(found_measures):
                raise EvidenceUnavailable("missing_measure")
    if found_parts != wanted_parts:
        raise EvidenceUnavailable("missing_part")
    _remove_layout(root)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)
