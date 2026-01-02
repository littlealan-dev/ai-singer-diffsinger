from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
    lyric_is_extended: bool
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
    verse_number: Optional[str | int] = None,
    lyrics_only: bool = True,
    keep_rests: bool = False,
) -> ScoreData:
    """Parse MusicXML (.xml or .mxl) into a lightweight score structure.

    If no part is specified, the first part with lyrics is selected.
    verse_number: when provided, select lyrics for the matching verse number.
    lyrics_only: when True, parts with lyrics drop notes without lyric tokens unless
    the lyric is marked as extended.
    keep_rests: when True, rest events are included alongside notes.
    """
    score = converter.parse(str(path))
    return _parse_score(
        score,
        part_id=part_id,
        part_index=part_index,
        verse_number=verse_number,
        lyrics_only=lyrics_only,
        keep_rests=keep_rests,
    )


def parse_musicxml_with_summary(
    path: str | Path,
    *,
    part_id: Optional[str] = None,
    part_index: Optional[int] = None,
    verse_number: Optional[str | int] = None,
    lyrics_only: bool = True,
    keep_rests: bool = False,
) -> tuple[ScoreData, Dict[str, Any]]:
    score = converter.parse(str(path))
    summary = _summarize_score(score)
    normalized_verse = _normalize_verse_number(verse_number)
    if normalized_verse is None:
        available_verses = summary.get("available_verses") if isinstance(summary, dict) else None
        if available_verses:
            normalized_verse = str(available_verses[0])
    score_data = _parse_score(
        score,
        part_id=part_id,
        part_index=part_index,
        verse_number=normalized_verse,
        lyrics_only=lyrics_only,
        keep_rests=keep_rests,
    )
    return score_data, summary


def _parse_score(
    score: stream.Score,
    *,
    part_id: Optional[str],
    part_index: Optional[int],
    verse_number: Optional[str | int],
    lyrics_only: bool,
    keep_rests: bool,
) -> ScoreData:
    selected_parts = _select_parts(
        score,
        part_id=part_id,
        part_index=part_index,
        verse_number=verse_number,
    )
    normalized_verse = _normalize_verse_number(verse_number)
    tempos = _extract_tempos(score)
    parts: List[PartData] = []
    for part in selected_parts:
        part_events = _collect_part_events(
            part,
            verse_number=normalized_verse,
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
    verse_number: Optional[str | int],
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
    for part in parts:
        if _part_has_lyrics(part, verse_number=verse_number):
            return [part]
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


def _part_has_lyrics(part: stream.Part, *, verse_number: Optional[str | int]) -> bool:
    for element in part.recurse().notes:
        if element.isRest:
            continue
        lyric_text, _, _ = _extract_lyric_text(element, verse_number=verse_number)
        if lyric_text:
            return True
    return False


def _summarize_score(score: stream.Score) -> Dict[str, Any]:
    metadata = score.metadata
    summary: Dict[str, Any] = {
        "title": metadata.title if metadata else None,
        "composer": metadata.composer if metadata else None,
        "lyricist": getattr(metadata, "lyricist", None) if metadata else None,
    }

    parts_summary: List[Dict[str, Any]] = []
    available_verses: set[str] = set()
    for index, part in enumerate(score.parts):
        lyric_numbers: set[str] = set()
        note_count = 0

        for element in part.recurse().notes:
            if element.isRest:
                continue
            note_count += 1
            for lyric in element.lyrics:
                text = (lyric.text or "").strip()
                if not text:
                    continue
                number = lyric.number or "1"
                lyric_numbers.add(str(number))

        if lyric_numbers:
            available_verses.update(lyric_numbers)

        parts_summary.append(
            {
                "part_index": index,
                "part_id": str(part.id) if part.id is not None else "",
                "part_name": part.partName,
                "has_lyrics": bool(lyric_numbers),
                "note_count": note_count,
            }
        )

    summary["parts"] = parts_summary
    summary["available_verses"] = sorted(available_verses, key=_lyric_sort_key)
    return summary


def _lyric_sort_key(value: str) -> tuple[int, object]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def _normalize_verse_number(verse_number: Optional[str | int]) -> Optional[str]:
    if verse_number is None:
        return None
    return str(verse_number)


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
    verse_number: Optional[str],
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
        _extract_lyric_text(element, verse_number=verse_number)[0] is not None
        for element in elements
        if not element.isRest
    )
    active_lyric: Optional[str] = None
    active_syllabic: Optional[str] = None
    active_extend = False
    events: list[NoteEvent] = []
    for element in elements:
        is_rest = element.isRest
        offset = float(element.getOffsetInHierarchy(part))
        if is_rest:
            if keep_rests:
                events.append(_make_rest_event(element, offset_beats=offset))
            active_extend = False
            continue
        lyric_text, syllabic, is_extended = _extract_lyric_text(element, verse_number=verse_number)
        tie_type = element.tie.type if element.tie is not None else None
        include = True
        lyric_value = lyric_text
        syllabic_value = syllabic
        lyric_extended = False
        if lyrics_only and has_lyric_text:
            if lyric_text is None:
                if (active_extend or tie_type in ("start", "continue", "stop")) and active_lyric is not None:
                    lyric_value = active_lyric
                    syllabic_value = active_syllabic
                    lyric_extended = True
                else:
                    include = False
        if include:
            events.append(
                _make_note_event(
                    element,
                    offset_beats=offset,
                    lyric=lyric_value,
                    syllabic=syllabic_value,
                    lyric_extended=lyric_extended,
                )
            )
        if lyric_text is not None:
            active_lyric = lyric_text
            active_syllabic = syllabic
            active_extend = is_extended
        elif tie_type in ("start", "continue"):
            active_extend = True
        elif tie_type == "stop" and not active_extend:
            active_extend = False
    return events


def _extract_lyric_text(
    element: note.NotRest,
    *,
    verse_number: Optional[str | int],
) -> tuple[Optional[str], Optional[str], bool]:
    if not element.lyrics:
        return None, None, False
    lyric = None
    if verse_number is None:
        lyric = element.lyrics[0]
    else:
        normalized = _normalize_verse_number(verse_number)
        for candidate in element.lyrics:
            candidate_number = _normalize_verse_number(candidate.number) or "1"
            if candidate_number == normalized:
                lyric = candidate
                break
    if lyric is None:
        return None, None, False
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
    lyric_extended: bool,
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
        lyric_is_extended=lyric_extended,
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
    voice = None
    voice_ctx = element.getContextByClass(stream.Voice)
    if voice_ctx is not None:
        voice = str(getattr(voice_ctx, "id", None) or getattr(voice_ctx, "number", None))
    return NoteEvent(
        offset_beats=offset_beats,
        duration_beats=float(element.duration.quarterLength),
        pitch_midi=None,
        pitch_hz=None,
        lyric=None,
        syllabic=None,
        lyric_is_extended=False,
        is_rest=True,
        tie_type=None,
        voice=voice,
        measure_number=measure_number,
    )
