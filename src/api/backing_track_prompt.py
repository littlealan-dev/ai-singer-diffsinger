"""Helpers for deriving backing-track prompts from MusicXML scores."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree

from src.api.score import parse_score
from src.musicxml.io import read_musicxml_content


_MAJOR_KEYS = {
    -7: "Cb major",
    -6: "Gb major",
    -5: "Db major",
    -4: "Ab major",
    -3: "Eb major",
    -2: "Bb major",
    -1: "F major",
    0: "C major",
    1: "G major",
    2: "D major",
    3: "A major",
    4: "E major",
    5: "B major",
    6: "F# major",
    7: "C# major",
}

_MINOR_KEYS = {
    -7: "Ab minor",
    -6: "Eb minor",
    -5: "Bb minor",
    -4: "F minor",
    -3: "C minor",
    -2: "G minor",
    -1: "D minor",
    0: "A minor",
    1: "E minor",
    2: "B minor",
    3: "F# minor",
    4: "C# minor",
    5: "G# minor",
    6: "D# minor",
    7: "A# minor",
}

_KIND_SUFFIXES = {
    "major": "",
    "minor": "m",
    "dominant": "7",
    "major-seventh": "maj7",
    "minor-seventh": "m7",
    "diminished": "dim",
    "augmented": "aug",
    "half-diminished": "m7b5",
}

_ACCIDENTALS = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}


def build_elevenlabs_backing_track_prompt(
    file_path: str | Path,
    *,
    purpose: str = "celebration",
    style_description: str = (
        "a simple, happy and supportive groove that allows a melody to be easily followed"
    ),
) -> str:
    """Build a compact backing-track prompt from a MusicXML score."""
    metadata = extract_backing_track_metadata(file_path)
    return build_elevenlabs_backing_track_prompt_from_metadata(
        metadata,
        purpose=purpose,
        style_description=style_description,
    )


def build_elevenlabs_backing_track_prompt_from_metadata(
    metadata: Dict[str, Any],
    *,
    purpose: str = "celebration",
    style_description: str = (
        "a simple, happy and supportive groove that allows a melody to be easily followed"
    ),
) -> str:
    """Build a compact backing-track prompt from pre-extracted score metadata."""
    section_line = _format_section_plan(metadata)
    return (
        f"Create an original instrumental backing track for {purpose} "
        f"in the key of {metadata['key_signature']} with a {metadata['time_signature']} "
        f"time signature and a tempo of {metadata['bpm']} BPM. "
        f"The track should be about {int(round(float(metadata['duration_seconds'])))} seconds long, "
        f"featuring {style_description}. "
        "The audio must continue through the end of the last measure and must not end early. "
        f"Start the accompaniment from the beginning of the first measure. "
        f"Use this section plan: {section_line}."
    )


def extract_backing_track_metadata(file_path: str | Path) -> Dict[str, Any]:
    """Extract backing-track metadata from a score for prompt generation."""
    path = Path(file_path)
    score = parse_score(path)
    root = ElementTree.fromstring(read_musicxml_content(path))

    beats, beat_type = _extract_time_signature(root)
    bpm = _extract_bpm(score)
    duration_seconds = _extract_duration_seconds(score, beats, bpm)

    return {
        "title": (score.get("score_summary") or {}).get("title") or score.get("title") or path.stem,
        "key_signature": _extract_key_signature(root),
        "time_signature": f"{beats}/{beat_type}",
        "beats_per_measure": beats,
        "beat_type": beat_type,
        "bpm": bpm,
        "measure_count": _count_measures(root),
        "duration_seconds": duration_seconds,
        "duration_mmss": _format_mmss(duration_seconds),
        "pickup": _extract_pickup(score, beats),
        "chord_progression": _extract_chord_progression(root),
    }


def _format_section_plan(metadata: Dict[str, Any]) -> str:
    measure_chords = _measure_chord_sequence(metadata["chord_progression"])
    measure_count = int(metadata["measure_count"])
    pickup = metadata.get("pickup") or {}
    sections = _build_sections(measure_count=measure_count, has_pickup=bool(pickup.get("has_pickup")))
    formatted_sections: List[str] = []
    for label, start_measure, end_measure in sections:
        section_chords = [
            chord
            for measure, chord in measure_chords
            if start_measure <= measure <= end_measure and chord
        ]
        if not section_chords:
            continue
        bars_label = (
            f"bar {start_measure}"
            if start_measure == end_measure
            else f"bars {start_measure}-{end_measure}"
        )
        formatted_sections.append(
            f"{label}: {bars_label}, chords {', '.join(section_chords)}"
        )
    return "; ".join(formatted_sections)


def _build_sections(*, measure_count: int, has_pickup: bool) -> List[tuple[str, int, int]]:
    if measure_count <= 0:
        return []
    sections: List[tuple[str, int, int]] = []
    start_measure = 1
    if has_pickup:
        sections.append(("pickup section", 1, 1))
        start_measure = 2
    if start_measure > measure_count:
        return sections
    remaining_measures = measure_count - start_measure + 1
    first_section_length = (remaining_measures + 1) // 2
    section_1_end = start_measure + first_section_length - 1
    sections.append(("section 1", start_measure, section_1_end))
    if section_1_end < measure_count:
        sections.append(("section 2", section_1_end + 1, measure_count))
    return sections


def _measure_chord_sequence(progression: List[Dict[str, Any]]) -> List[tuple[int, str]]:
    measure_map: Dict[int, str] = {}
    first_measure_chord = _first_measure_chord(progression)
    if first_measure_chord:
        measure_map[1] = first_measure_chord
    for entry in progression:
        measure = entry.get("measure")
        values = entry.get("chords")
        if not isinstance(measure, int):
            continue
        if not isinstance(values, list) or not values:
            continue
        chord = str(values[0]).strip()
        if chord:
            measure_map[measure] = chord
    return sorted(measure_map.items())

def _first_measure_chord(progression: List[Dict[str, Any]]) -> str | None:
    for entry in progression:
        measure = entry.get("measure")
        values = entry.get("chords")
        if not isinstance(values, list) or not values:
            continue
        first_chord = str(values[0]).strip()
        if not first_chord:
            continue
        if measure == 1:
            return first_chord
    for entry in progression:
        values = entry.get("chords")
        if not isinstance(values, list) or not values:
            continue
        first_chord = str(values[0]).strip()
        if first_chord:
            return first_chord
    return None


def _extract_bpm(score: Dict[str, Any]) -> int:
    tempos = score.get("tempos") or []
    if not tempos:
        return 120
    bpm = tempos[0].get("bpm")
    if bpm is None:
        return 120
    return int(round(float(bpm)))


def _extract_duration_seconds(score: Dict[str, Any], beats_per_measure: int, bpm: int) -> float:
    summary = score.get("score_summary") or {}
    duration = summary.get("duration_seconds")
    if duration is not None:
        return float(duration)
    total_beats = 0.0
    parts = score.get("parts") or []
    if parts:
        for note in parts[0].get("notes", []):
            total_beats = max(total_beats, float(note["offset_beats"]) + float(note["duration_beats"]))
    if bpm <= 0:
        bpm = 120
    return total_beats * 60.0 / bpm if total_beats else beats_per_measure * 60.0 / bpm


def _extract_time_signature(root: ElementTree.Element) -> tuple[int, int]:
    for elem in root.iter():
        if _local_tag(elem.tag) != "time":
            continue
        beats = None
        beat_type = None
        for child in elem:
            tag = _local_tag(child.tag)
            if tag == "beats":
                beats = int((child.text or "0").strip() or 0)
            elif tag == "beat-type":
                beat_type = int((child.text or "0").strip() or 0)
        if beats and beat_type:
            return beats, beat_type
    return 4, 4


def _extract_key_signature(root: ElementTree.Element) -> str:
    for elem in root.iter():
        if _local_tag(elem.tag) != "key":
            continue
        fifths = 0
        mode = "major"
        for child in elem:
            tag = _local_tag(child.tag)
            if tag == "fifths":
                fifths = int((child.text or "0").strip() or 0)
            elif tag == "mode":
                mode = ((child.text or "").strip() or "major").lower()
        if mode == "minor":
            return _MINOR_KEYS.get(fifths, f"{fifths} fifths minor")
        return _MAJOR_KEYS.get(fifths, f"{fifths} fifths major")
    return "C major"


def _count_measures(root: ElementTree.Element) -> int:
    for part in root.iter():
        if _local_tag(part.tag) != "part":
            continue
        return sum(1 for child in part if _local_tag(child.tag) == "measure")
    return 0


def _extract_pickup(score: Dict[str, Any], beats_per_measure: int) -> Dict[str, Any]:
    parts = score.get("parts") or []
    if not parts:
        return {"has_pickup": False, "starts_on_beat": None, "second_bar_downbeat_note": None}

    notes = parts[0].get("notes") or []
    first_bar_notes = [note for note in notes if int(note.get("measure_number") or 0) == 1 and not note.get("is_rest")]
    if not first_bar_notes:
        return {"has_pickup": False, "starts_on_beat": None, "second_bar_downbeat_note": None}

    first_note = min(first_bar_notes, key=lambda item: float(item["offset_beats"]))
    offset_in_measure = float(first_note["offset_beats"])
    if offset_in_measure <= 0.0:
        return {"has_pickup": False, "starts_on_beat": None, "second_bar_downbeat_note": None}

    second_measure_notes = [
        note for note in notes if int(note.get("measure_number") or 0) == 2 and not note.get("is_rest")
    ]
    downbeat_note = None
    for note in second_measure_notes:
        local_offset = float(note["offset_beats"]) - beats_per_measure
        if abs(local_offset) < 1e-6:
            downbeat_note = note
            break
    if downbeat_note is None and second_measure_notes:
        downbeat_note = min(second_measure_notes, key=lambda item: float(item["offset_beats"]))

    return {
        "has_pickup": True,
        "starts_on_beat": int(round(offset_in_measure)) + 1,
        "second_bar_downbeat_note": _format_pitch(downbeat_note),
    }


def _extract_chord_progression(root: ElementTree.Element) -> List[Dict[str, Any]]:
    progression: List[Dict[str, Any]] = []
    first_part = None
    for part in root.iter():
        if _local_tag(part.tag) == "part":
            first_part = part
            break
    if first_part is None:
        return progression

    for measure in first_part:
        if _local_tag(measure.tag) != "measure":
            continue
        chords: List[str] = []
        for child in measure:
            if _local_tag(child.tag) != "harmony":
                continue
            name = _format_harmony(child)
            if name and (not chords or chords[-1] != name):
                chords.append(name)
        if chords:
            progression.append(
                {
                    "measure": int((measure.attrib.get("number") or "0").strip() or 0),
                    "chords": chords,
                }
            )
    return progression


def _format_harmony(harmony_elem: ElementTree.Element) -> str:
    root_step = None
    root_alter = 0
    bass_step = None
    bass_alter = 0
    kind = "major"

    for child in harmony_elem:
        tag = _local_tag(child.tag)
        if tag == "root":
            for nested in child:
                nested_tag = _local_tag(nested.tag)
                if nested_tag == "root-step":
                    root_step = (nested.text or "").strip() or None
                elif nested_tag == "root-alter":
                    root_alter = int(float((nested.text or "0").strip() or 0))
        elif tag == "bass":
            for nested in child:
                nested_tag = _local_tag(nested.tag)
                if nested_tag == "bass-step":
                    bass_step = (nested.text or "").strip() or None
                elif nested_tag == "bass-alter":
                    bass_alter = int(float((nested.text or "0").strip() or 0))
        elif tag == "kind":
            kind = ((child.text or "").strip() or "major").lower()

    if not root_step:
        return ""

    chord_name = f"{root_step}{_ACCIDENTALS.get(root_alter, '')}{_KIND_SUFFIXES.get(kind, '')}"
    if bass_step:
        chord_name = f"{chord_name}/{bass_step}{_ACCIDENTALS.get(bass_alter, '')}"
    return chord_name


def _format_pitch(note: Dict[str, Any] | None) -> str | None:
    if not note:
        return None
    step = note.get("pitch_step")
    octave = note.get("pitch_octave")
    alter = int(note.get("pitch_alter") or 0)
    if step is None or octave is None:
        return None
    return f"{step}{_ACCIDENTALS.get(alter, '')}{int(octave)}"


def _format_mmss(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes}:{remaining_seconds:02d}"


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag
