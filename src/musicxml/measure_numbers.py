"""Preserve uploaded MusicXML while giving entirely unnumbered scores usable measures."""

from __future__ import annotations

import re


MEASURE_TAG = re.compile(r"<measure(?=[\s/>])[^>]*>")
PART_BLOCK = re.compile(r"<part(?=[\s>])[^>]*>.*?</part>", re.S)
NUMBER_ATTR = re.compile(r"(\snumber\s*=\s*)([\"'])(.*?)\2", re.S)


def renumber_unnumbered_measures(xml_text: str) -> tuple[str, int]:
    """Number measures 1..N per part when no measure has a positive number."""
    if "<score-partwise" not in xml_text:
        return xml_text, 0

    tags = MEASURE_TAG.findall(xml_text)
    if not tags:
        return xml_text, 0
    for tag in tags:
        attribute = NUMBER_ATTR.search(tag)
        value = attribute.group(3).strip() if attribute else ""
        if not value:
            continue
        try:
            number = int(value)
        except ValueError:
            return xml_text, 0
        if number > 0:
            return xml_text, 0

    count = 0

    def replace_part(part_match: re.Match[str]) -> str:
        nonlocal count
        position = 0

        def replace_measure(measure_match: re.Match[str]) -> str:
            nonlocal count, position
            position += 1
            count += 1
            tag = measure_match.group(0)
            attribute = NUMBER_ATTR.search(tag)
            if attribute:
                start, end = attribute.span(3)
                return tag[:start] + str(position) + tag[end:]
            return tag.replace("<measure", f'<measure number="{position}"', 1)

        return MEASURE_TAG.sub(replace_measure, part_match.group(0))

    updated = PART_BLOCK.sub(replace_part, xml_text)
    return updated, count
