from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from music21 import chord, converter, note, stream, tempo


@dataclass(frozen=True)
class TempoEvent:
    offset_beats: float
    bpm: float


@dataclass(frozen=True)
class NoteEvent:
    offset_beats: float
    duration_beats: float
    pitch_midi: Optional[float]
    pitch_hz: Optional[float]
    lyric: Optional[str]
    syllabic: Optional[str]
    is_rest: bool
    tie_type: Optional[str]
    voice: Optional[str]
    measure_number: Optional[int]


@dataclass(frozen=True)
class PartData:
    part_id: str
    part_name: Optional[str]
    notes: Sequence[NoteEvent]


@dataclass(frozen=True)
class ScoreData:
    title: Optional[str]
    tempos: Sequence[TempoEvent]
    parts: Sequence[PartData]


def parse_musicxml(
    path: str | Path,
    *,
    part_id: Optional[str] = None,
    part_index: Optional[int] = None,
    lyrics_only: bool = True,
    keep_rests: bool = False,
) -> ScoreData:
    """Parse MusicXML (.xml or .mxl) into a lightweight score structure.

    If no part is specified, the highest-average-pitch part is selected.
    lyrics_only: when True, parts with lyrics drop notes without lyric tokens unless
    the lyric is marked as extended.
    keep_rests: when True, rest events are included alongside notes.
    """
    score = converter.parse(str(path))
    selected_parts = _select_parts(score, part_id=part_id, part_index=part_index)
    tempos = _extract_tempos(score)
    parts = []
    for part in selected_parts:
        part_events = _collect_part_events(
            part,
            lyrics_only=lyrics_only,
            keep_rests=keep_rests,
        )
        part_id_value = str(part.id) if part.id is not None else ""
        parts.append(
            PartData(
                part_id=part_id_value,
                part_name=part.partName,
                notes=part_events,
            )
        )
    title = score.metadata.title if score.metadata else None
    return ScoreData(title=title, tempos=tempos, parts=parts)


def _select_parts(
    score: stream.Score,
    *,
    part_id: Optional[str],
    part_index: Optional[int],
) -> Sequence[stream.Part]:
    if part_id is not None and part_index is not None:
        raise ValueError("Provide part_id or part_index, not both.")
    parts = list(score.parts)
    if part_id is not None:
        for part in parts:
            if part.id == part_id:
                return [part]
        raise ValueError(f"part_id not found: {part_id}")
    if part_index is not None:
        if part_index < 0 or part_index >= len(parts):
            raise IndexError(f"part_index out of range: {part_index}")
        return [parts[part_index]]
    if not parts:
        return []
    return [_select_top_part(parts)]


def _select_top_part(parts: Sequence[stream.Part]) -> stream.Part:
    best_part = parts[0]
    best_pitch = _part_average_midi(best_part)
    for part in parts[1:]:
        avg_pitch = _part_average_midi(part)
        if avg_pitch > best_pitch:
            best_part = part
            best_pitch = avg_pitch
    return best_part


def _part_average_midi(part: stream.Part) -> float:
    midis = []
    for element in part.recurse().notes:
        if isinstance(element, chord.Chord):
            pitch = max(element.pitches, key=lambda p: p.midi)
            midis.append(float(pitch.midi))
        elif isinstance(element, note.Note) and element.pitch is not None:
            midis.append(float(element.pitch.midi))
    if not midis:
        return float("-inf")
    return sum(midis) / len(midis)


def _extract_tempos(score: stream.Score) -> Sequence[TempoEvent]:
    tempo_events = []
    for mark in score.recurse().getElementsByClass(tempo.MetronomeMark):
        bpm = _metronome_bpm(mark)
        if bpm is None:
            continue
        tempo_events.append(
            TempoEvent(
                offset_beats=float(mark.offset),
                bpm=float(bpm),
            )
        )
    if not tempo_events:
        tempo_events = [TempoEvent(offset_beats=0.0, bpm=120.0)]
    tempo_events.sort(key=lambda event: event.offset_beats)
    return tempo_events


def _metronome_bpm(mark: tempo.MetronomeMark) -> Optional[float]:
    if hasattr(mark, "getQuarterBPM"):
        bpm = mark.getQuarterBPM()
        if bpm is not None:
            return float(bpm)
    if mark.number is not None:
        return float(mark.number)
    return None


def _collect_part_events(
    part: stream.Part,
    *,
    lyrics_only: bool,
    keep_rests: bool,
) -> Sequence[NoteEvent]:
    elements = list(part.recurse().notesAndRests)
    elements.sort(
        key=lambda element: (
            float(element.getOffsetInHierarchy(part)),
            getattr(element, "priority", 0),
        )
    )
    has_lyric_text = any(
        _extract_lyric_text(element)[0] is not None
        for element in elements
        if not element.isRest
    )
    active_lyric: Optional[str] = None
    active_syllabic: Optional[str] = None
    events: list[NoteEvent] = []
    for element in elements:
        is_rest = element.isRest
        offset = float(element.getOffsetInHierarchy(part))
        if is_rest:
            if keep_rests:
                events.append(_make_rest_event(element, offset_beats=offset))
            continue
        lyric_text, syllabic, is_extended = _extract_lyric_text(element)
        include = True
        lyric_value = lyric_text
        syllabic_value = syllabic
        if lyrics_only and has_lyric_text:
            if lyric_text is None:
                if is_extended and active_lyric is not None:
                    lyric_value = active_lyric
                    syllabic_value = active_syllabic
                else:
                    include = False
        if include:
            events.append(
                _make_note_event(
                    element,
                    offset_beats=offset,
                    lyric=lyric_value,
                    syllabic=syllabic_value,
                )
            )
        if lyric_text is not None:
            active_lyric = lyric_text
            active_syllabic = syllabic
    return events


def _extract_lyric_text(element: note.NotRest) -> tuple[Optional[str], Optional[str], bool]:
    if not element.lyrics:
        return None, None, False
    lyric = element.lyrics[0]
    text = lyric.text if lyric.text is not None else ""
    text = text.strip()
    if not text:
        text = None
    syllabic = getattr(lyric, "syllabic", None)
    is_extended = False
    if hasattr(lyric, "isExtended"):
        is_extended = bool(lyric.isExtended)
    elif hasattr(lyric, "extend"):
        is_extended = bool(lyric.extend)
    return text, syllabic, is_extended


def _make_note_event(
    element: note.NotRest,
    *,
    offset_beats: float,
    lyric: Optional[str],
    syllabic: Optional[str],
) -> NoteEvent:
    pitch = None
    if isinstance(element, chord.Chord):
        pitch = max(element.pitches, key=lambda p: p.midi)
    elif isinstance(element, note.Note):
        pitch = element.pitch
    pitch_midi = float(pitch.midi) if pitch is not None else None
    pitch_hz = float(pitch.frequency) if pitch is not None else None
    tie_type = element.tie.type if element.tie is not None else None
    voice = None
    voice_ctx = element.getContextByClass(stream.Voice)
    if voice_ctx is not None:
        voice = str(getattr(voice_ctx, "id", None) or getattr(voice_ctx, "number", None))
    measure_ctx = element.getContextByClass(stream.Measure)
    measure_number = measure_ctx.number if measure_ctx is not None else None
    return NoteEvent(
        offset_beats=offset_beats,
        duration_beats=float(element.duration.quarterLength),
        pitch_midi=pitch_midi,
        pitch_hz=pitch_hz,
        lyric=lyric,
        syllabic=syllabic,
        is_rest=False,
        tie_type=tie_type,
        voice=voice,
        measure_number=measure_number,
    )


def _make_rest_event(
    element: note.Rest,
    *,
    offset_beats: float,
) -> NoteEvent:
    measure_ctx = element.getContextByClass(stream.Measure)
    measure_number = measure_ctx.number if measure_ctx is not None else None
    return NoteEvent(
        offset_beats=offset_beats,
        duration_beats=float(element.duration.quarterLength),
        pitch_midi=None,
        pitch_hz=None,
        lyric=None,
        syllabic=None,
        is_rest=True,
        tie_type=None,
        voice=None,
        measure_number=measure_number,
    )
