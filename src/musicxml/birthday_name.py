"""Write a name into the Happy Birthday demo's "you ___" placeholder.

Deterministic, and only for the Happy Birthday demo song. The placeholder's
position is fixed: measure 7, the first two beats, in the Alto (P2) and Men
(P3) parts. Nothing is searched. Every call rebuilds those two beats from the
original pitches, so a second name, or none, replaces any earlier one cleanly.

Rhythm, by syllable count (all syllables but the last on beat 1, the last on
beat 2):

    1   quarter: the whole name, extended under a slur | quarter (slur stop)
    2   quarter: syllable 1                             | quarter: syllable 2
    3   two eighths: syllables 1, 2                     | quarter: syllable 3
    4   triplet eighths: syllables 1-3                  | quarter: syllable 4

The demo's vocal parts use 12 divisions per quarter, so a triplet eighth is 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from xml.etree import ElementTree

from src.musicxml.demo_songs import read_demo_song_id
from src.musicxml.solfege import _read_root, _write_root

DEMO_SONG_ID = "happy-birthday"
PLACEHOLDER_MEASURE = "7"
DIVISIONS_PER_QUARTER = 12
MAX_SYLLABLES = 4
PLACEHOLDER_TEXT = "you"


@dataclass(frozen=True)
class _Pitch:
    step: str
    octave: int


# Beat 1 and beat 2 pitches of the placeholder, per part.
PLACEHOLDER_PITCHES: Dict[str, tuple[_Pitch, _Pitch]] = {
    "P2": (_Pitch("E", 4), _Pitch("D", 4)),
    "P3": (_Pitch("C", 3), _Pitch("D", 3)),
}


class NamePlaceholderError(ValueError):
    """The score is not the Happy Birthday demo, or its placeholder was changed."""


def write_birthday_name(
    source_path: Path,
    output_path: Path,
    *,
    words: Optional[Sequence[Sequence[str]]],
) -> Dict[str, Any]:
    """Rebuild the placeholder with a name's syllables, or restore "you".

    ``words`` holds each word's syllables, e.g. ``[["Hen", "ry"]]`` or
    ``[["An", "na"], ["Ma", "rie"]]``; ``None`` restores the original "you".
    Returns ``status: "unchanged"`` without writing when the placeholder
    already holds exactly this text and rhythm.
    """
    if read_demo_song_id(Path(source_path)) != DEMO_SONG_ID:
        raise NamePlaceholderError("This score is not the Happy Birthday demo song.")
    syllables = _syllables_with_syllabic(words)
    if words is not None and not 1 <= len(syllables) <= MAX_SYLLABLES:
        raise ValueError(f"A name must have 1 to {MAX_SYLLABLES} syllables.")
    root = _read_root(Path(source_path))
    changed = False
    for part_id, (beat1, beat2) in PLACEHOLDER_PITCHES.items():
        measure = _placeholder_measure(root, part_id)
        span = _placeholder_span(measure, beat1, beat2)
        voice = span[0].findtext("voice") or "1"
        replacement = _build_notes(beat1, beat2, syllables, voice=voice)
        if _span_key(span) == _span_key(replacement):
            continue
        index = list(measure).index(span[0])
        for note in span:
            measure.remove(note)
        for offset, note in enumerate(replacement):
            measure.insert(index + offset, note)
        changed = True
    if not changed:
        return {"status": "unchanged"}
    _write_root(root, Path(output_path))
    return {"status": "ready", "derived_musicxml_path": str(output_path)}


def _syllables_with_syllabic(
    words: Optional[Sequence[Sequence[str]]],
) -> List[tuple[str, str]]:
    if words is None:
        return [(PLACEHOLDER_TEXT, "single")]
    syllables: List[tuple[str, str]] = []
    for word in words:
        pieces = list(word)
        for index, piece in enumerate(pieces):
            if len(pieces) == 1:
                syllabic = "single"
            elif index == 0:
                syllabic = "begin"
            elif index == len(pieces) - 1:
                syllabic = "end"
            else:
                syllabic = "middle"
            syllables.append((piece, syllabic))
    return syllables


def _placeholder_measure(root: ElementTree.Element, part_id: str) -> ElementTree.Element:
    part = root.find(f"part[@id='{part_id}']")
    if part is None:
        raise NamePlaceholderError(f"Part {part_id} is missing.")
    divisions = [element.text for element in part.iter("divisions")]
    if divisions[:1] != [str(DIVISIONS_PER_QUARTER)]:
        raise NamePlaceholderError(f"Part {part_id} does not use {DIVISIONS_PER_QUARTER} divisions.")
    for measure in part.findall("measure"):
        if measure.get("number") == PLACEHOLDER_MEASURE:
            return measure
    raise NamePlaceholderError(f"Part {part_id} has no measure {PLACEHOLDER_MEASURE}.")


def _placeholder_span(
    measure: ElementTree.Element, beat1: _Pitch, beat2: _Pitch
) -> List[ElementTree.Element]:
    """The notes filling the measure's first two beats: the placeholder or an earlier name."""
    span: List[ElementTree.Element] = []
    total = 0
    for note in measure.findall("note"):
        if total >= 2 * DIVISIONS_PER_QUARTER:
            break
        span.append(note)
        total += int(note.findtext("duration") or 0)
    pitches = [_note_pitch(note) for note in span]
    beat1_notes, last = span[:-1], span[-1] if span else None
    valid = (
        total == 2 * DIVISIONS_PER_QUARTER
        and last is not None
        and pitches[-1] == beat2
        and int(last.findtext("duration") or 0) == DIVISIONS_PER_QUARTER
        and beat1_notes
        and all(pitch == beat1 for pitch in pitches[:-1])
        and all(note.find("chord") is None for note in span)
    )
    if not valid:
        raise NamePlaceholderError("Measure 7 no longer holds the birthday name placeholder.")
    return span


def _note_pitch(note: ElementTree.Element) -> Optional[_Pitch]:
    pitch = note.find("pitch")
    if pitch is None or pitch.find("alter") is not None:
        return None
    return _Pitch(pitch.findtext("step") or "", int(pitch.findtext("octave") or 0))


def _build_notes(
    beat1: _Pitch,
    beat2: _Pitch,
    syllables: List[tuple[str, str]],
    *,
    voice: str,
) -> List[ElementTree.Element]:
    *front, last = syllables
    quarter = DIVISIONS_PER_QUARTER
    if not front:
        # One syllable: the whole word on beat 1, held into beat 2 under a slur.
        text, syllabic = last
        return [
            _note(beat1, quarter, "quarter", voice, lyric=(text, syllabic, True), slur="start"),
            _note(beat2, quarter, "quarter", voice, slur="stop"),
        ]
    notes: List[ElementTree.Element] = []
    if len(front) == 1:
        notes.append(_note(beat1, quarter, "quarter", voice, lyric=(*front[0], False)))
    else:
        count = len(front)
        triplet = count == 3
        duration = quarter // count
        for index, (text, syllabic) in enumerate(front):
            beam = "begin" if index == 0 else "end" if index == count - 1 else "continue"
            tuplet = "start" if triplet and index == 0 else "stop" if triplet and index == count - 1 else None
            notes.append(
                _note(
                    beat1, duration, "eighth", voice,
                    lyric=(text, syllabic, False), beam=beam, triplet=triplet, tuplet=tuplet,
                )
            )
    notes.append(_note(beat2, quarter, "quarter", voice, lyric=(*last, False)))
    return notes


def _note(
    pitch: _Pitch,
    duration: int,
    note_type: str,
    voice: str,
    *,
    lyric: Optional[tuple[str, str, bool]] = None,
    slur: Optional[str] = None,
    beam: Optional[str] = None,
    triplet: bool = False,
    tuplet: Optional[str] = None,
) -> ElementTree.Element:
    note = ElementTree.Element("note")
    pitch_element = ElementTree.SubElement(note, "pitch")
    ElementTree.SubElement(pitch_element, "step").text = pitch.step
    ElementTree.SubElement(pitch_element, "octave").text = str(pitch.octave)
    ElementTree.SubElement(note, "duration").text = str(duration)
    ElementTree.SubElement(note, "voice").text = voice
    ElementTree.SubElement(note, "type").text = note_type
    if triplet:
        modification = ElementTree.SubElement(note, "time-modification")
        ElementTree.SubElement(modification, "actual-notes").text = "3"
        ElementTree.SubElement(modification, "normal-notes").text = "2"
    if beam:
        ElementTree.SubElement(note, "beam", {"number": "1"}).text = beam
    if slur or tuplet:
        notations = ElementTree.SubElement(note, "notations")
        if slur:
            ElementTree.SubElement(notations, "slur", {"type": slur, "number": "1"})
        if tuplet:
            ElementTree.SubElement(notations, "tuplet", {"type": tuplet, "bracket": "yes"})
    if lyric:
        text, syllabic, extend = lyric
        lyric_element = ElementTree.SubElement(note, "lyric", {"number": "1"})
        ElementTree.SubElement(lyric_element, "syllabic").text = syllabic
        ElementTree.SubElement(lyric_element, "text").text = text
        if extend:
            ElementTree.SubElement(lyric_element, "extend")
    return note


def _span_key(notes: Sequence[ElementTree.Element]) -> List[Any]:
    """What the placeholder sings: pitch, rhythm and verse-1 lyric, ignoring layout."""
    key: List[Any] = []
    for note in notes:
        lyrics = [
            (lyric.findtext("syllabic"), lyric.findtext("text"), lyric.find("extend") is not None)
            for lyric in note.findall("lyric")
            if (lyric.get("number") or "1") == "1" and not lyric.get("name")
        ]
        key.append((_note_pitch(note), note.findtext("duration"), note.findtext("type"), lyrics))
    return key
