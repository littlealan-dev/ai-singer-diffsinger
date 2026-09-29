"""Build the repeat-navigation fixtures with a piano accompaniment part.

Each vocal fixture in tests/fixtures/repeat_navigation gains a Piano part that
clones the vocal part's measures: the same barlines, repeats, endings and
navigation directions, so both parts expand to the same played order, with the
notes one octave lower and no lyrics. Part ids are fixed (P1 voice, P2 piano)
so every fixture shares one lyric selection id.

Usage: python scripts/build_repeat_piano_fixtures.py
"""

from __future__ import annotations

import copy
from pathlib import Path
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = ROOT / "tests" / "fixtures" / "repeat_navigation"
TARGET_DIRECTORY = ROOT / "tests" / "fixtures" / "repeat_navigation_piano"
# .xml, as the upload accepts only .xml and .mxl names: a fixture can be
# uploaded in the UI as it is.
FIXTURE_NAMES = (
    "forward_repeat.xml",
    "volta_endings.xml",
    "da_capo.xml",
    "da_capo_al_fine.xml",
    "da_capo_al_coda.xml",
    "dal_segno.xml",
    "dal_segno_al_fine.xml",
    "dal_segno_al_coda.xml",
)
VOICE_PART_ID = "P1"
PIANO_PART_ID = "P2"
PIANO_INSTRUMENT_ID = "P2-I1"


def _piano_score_part() -> ElementTree.Element:
    score_part = ElementTree.Element("score-part", {"id": PIANO_PART_ID})
    ElementTree.SubElement(score_part, "part-name").text = "Piano"
    instrument = ElementTree.SubElement(
        score_part, "score-instrument", {"id": PIANO_INSTRUMENT_ID}
    )
    ElementTree.SubElement(instrument, "instrument-name").text = "Acoustic Grand Piano"
    midi = ElementTree.SubElement(
        score_part, "midi-instrument", {"id": PIANO_INSTRUMENT_ID}
    )
    ElementTree.SubElement(midi, "midi-channel").text = "2"
    ElementTree.SubElement(midi, "midi-program").text = "1"
    return score_part


def _piano_part(voice_part: ElementTree.Element) -> ElementTree.Element:
    piano = copy.deepcopy(voice_part)
    piano.set("id", PIANO_PART_ID)
    for note in piano.iter("note"):
        for lyric in note.findall("lyric"):
            note.remove(lyric)
        octave = note.find("pitch/octave")
        if octave is not None and octave.text:
            octave.text = str(int(octave.text) - 1)
    return piano


def build(source_path: Path, target_path: Path) -> None:
    root = ElementTree.parse(source_path).getroot()
    part_list = root.find("part-list")
    parts = root.findall("part")
    if part_list is None or len(parts) != 1:
        raise ValueError(f"{source_path.name}: expected exactly one vocal part")
    voice_score_part = part_list.find("score-part")
    if voice_score_part is None:
        raise ValueError(f"{source_path.name}: missing score-part")
    voice_score_part.set("id", VOICE_PART_ID)
    voice_part = parts[0]
    voice_part.set("id", VOICE_PART_ID)
    part_list.append(_piano_score_part())
    root.insert(list(root).index(voice_part) + 1, _piano_part(voice_part))
    ElementTree.indent(root, space="  ")
    target_path.write_bytes(
        ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    )


def main() -> None:
    TARGET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name in FIXTURE_NAMES:
        build(SOURCE_DIRECTORY / name, TARGET_DIRECTORY / name)
        print(f"wrote {TARGET_DIRECTORY.relative_to(ROOT) / name}")


if __name__ == "__main__":
    main()
