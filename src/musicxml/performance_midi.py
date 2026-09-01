"""Generate local instrumental MIDI performance artifacts from MusicXML."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from xml.etree import ElementTree

from music21 import bar, instrument, midi, note, percussion, repeat, stream, tempo

from src.musicxml.io import read_musicxml_content
from src.musicxml.parser import _expand_repeat_navigation
from src.musicxml.part_reference import load_musicxml_score, map_parser_part_indices_to_raw_part_ids
from src.musicxml.instrument_programs import instrumental_programs_by_part


PERFORMANCE_MIDI_VERSION = 7


def build_instrumental_performance_midis(
    source_path: Path,
    *,
    original_output_path: Path,
    expanded_output_path: Path,
    instrument_program_assignments: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Write written- and played-order instrumental MIDI files for one score.

    The source notation is loaded separately for each representation so repeat
    expansion and sounding-pitch conversion never mutate the uploaded MusicXML.
    """
    original_score = load_musicxml_score(source_path)
    raw_part_ids = map_parser_part_indices_to_raw_part_ids(source_path, score=original_score)
    programs_by_part = instrumental_programs_by_part(
        source_path, assignments=instrument_program_assignments
    )
    parts = _instrumental_part_metadata(original_score, raw_part_ids, programs_by_part)
    eligible_indices = {part["part_index"] for part in parts if part["eligible"]}
    result: Dict[str, Any] = {
        "version": PERFORMANCE_MIDI_VERSION,
        "instrumental_parts": parts,
        "has_instrumental_parts": bool(eligible_indices),
        "original_midi_available": False,
        "expanded_midi_available": False,
        "diagnostic": None,
    }
    if not eligible_indices:
        return result

    try:
        _write_instrumental_midi(
            source_path,
            output_path=original_output_path,
            eligible_indices=eligible_indices,
            expand_repeats=False,
            programs_by_part=programs_by_part,
        )
        result["original_midi_available"] = original_output_path.is_file()
        _write_instrumental_midi(
            source_path,
            output_path=expanded_output_path,
            eligible_indices=eligible_indices,
            expand_repeats=True,
            programs_by_part=programs_by_part,
        )
        result["expanded_midi_available"] = expanded_output_path.is_file()
    except Exception as exc:  # Keep a MIDI conversion issue isolated from score upload.
        result["diagnostic"] = f"Instrumental MIDI could not be prepared: {exc}"
        for output_path in (original_output_path, expanded_output_path):
            output_path.unlink(missing_ok=True)
        result["original_midi_available"] = False
        result["expanded_midi_available"] = False
    return result


def _instrumental_part_metadata(
    score: stream.Score,
    raw_part_ids: Dict[int, str],
    programs_by_part: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for index, part in enumerate(score.parts):
        score_instrument = part.getInstrument(returnDefault=False)
        has_lyrics = _part_has_lyrics(part)
        is_vocal = isinstance(score_instrument, instrument.Vocalist)
        program = getattr(score_instrument, "midiProgram", None)
        channel = getattr(score_instrument, "midiChannel", None)
        has_explicit_non_vocal_instrument = (
            score_instrument is not None
            and not is_vocal
            and (isinstance(program, int) or isinstance(channel, int))
        )
        source_eligible = bool(part.recurse().notes) and (
            not has_lyrics or has_explicit_non_vocal_instrument
        )
        percussion = channel == 9 or isinstance(score_instrument, instrument.UnpitchedPercussion)
        raw_part_id = raw_part_ids.get(index, str(part.id or ""))
        program_facts = programs_by_part.get(raw_part_id, {})
        resolved_program = program_facts.get("resolved_gm_program")
        playback_preset = program_facts.get("playback_preset")
        score_instrument_id = program_facts.get("score_instrument_id")
        eligible = source_eligible and isinstance(resolved_program, int)
        result.append(
            {
                "part_index": index,
                "part_id": str(part.id or raw_part_id),
                "raw_part_id": raw_part_id,
                "label": str(part.partName or raw_part_id or f"Part {index + 1}"),
                "eligible": eligible,
                "has_lyrics": has_lyrics,
                # ``None`` means the MusicXML supplied no explicit program and
                # the synthesize caller did not provide an LLM assignment.  Do
                # not silently report it as piano (GM 0).
                "midi_program": resolved_program if isinstance(resolved_program, int) else None,
                "playback_preset": playback_preset if isinstance(playback_preset, dict) else None,
                "soundfont_bank": playback_preset.get("bank") if isinstance(playback_preset, dict) else None,
                "score_instrument_id": score_instrument_id,
                "program_source": program_facts.get("program_source", "unresolved"),
                "midi_channel": int(channel) if isinstance(channel, int) else None,
                "percussion": percussion,
                "diagnostic": (
                    "Part has lyrics and no explicit non-vocal instrument."
                    if has_lyrics and not has_explicit_non_vocal_instrument
                    else "Part has no resolved General MIDI program."
                    if source_eligible and not isinstance(resolved_program, int)
                    else None
                ),
            }
        )
    return result


def _part_has_lyrics(part: stream.Part) -> bool:
    return any(
        bool((lyric.text or "").strip())
        for note in part.recurse().notes
        if not note.isRest
        for lyric in note.lyrics
    )


def _write_instrumental_midi(
    source_path: Path,
    *,
    output_path: Path,
    eligible_indices: set[int],
    expand_repeats: bool,
    programs_by_part: Dict[str, Dict[str, Any]],
) -> None:
    score = load_musicxml_score(source_path)
    written_raw_part_ids = map_parser_part_indices_to_raw_part_ids(source_path, score=score)
    _restore_unpitched_midi_pitches(score, source_path, written_raw_part_ids)
    if expand_repeats:
        score = _expand_repeat_navigation(score, source_path)
    else:
        _strip_navigation_for_written_order_midi(score)
    instrumental_score = stream.Score()
    raw_part_ids = map_parser_part_indices_to_raw_part_ids(source_path, score=score)
    for index, part in enumerate(score.parts):
        if index in eligible_indices:
            program_facts = programs_by_part.get(raw_part_ids.get(index, str(part.id or "")))
            resolved_program = (
                program_facts.get("resolved_gm_program")
                if isinstance(program_facts, dict)
                else None
            )
            if isinstance(resolved_program, int):
                part.getInstrument(returnDefault=True).midiProgram = resolved_program
            instrumental_score.insert(0, deepcopy(part))
    _copy_score_tempos(score, instrumental_score)
    if not instrumental_score.parts:
        raise ValueError("No eligible instrumental parts.")
    try:
        sounding_score = instrumental_score.toSoundingPitch(inPlace=False)
    except Exception:
        sounding_score = instrumental_score
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sounding_score.write("midi", fp=str(output_path))
    _replace_midi_tempo_map(output_path, _canonical_tempo_events(score))


def _restore_unpitched_midi_pitches(
    score: stream.Score,
    source_path: Path,
    raw_part_ids: Dict[int, str],
) -> None:
    """Restore MusicXML's per-note GM drum pitches before MIDI export.

    music21 imports the first declared percussion instrument for an entire
    part and discards each ``note/instrument`` route. Its MIDI writer then
    emits that first instrument's pitch for every note (commonly bass drum).
    MusicXML's ``midi-unpitched`` value is the explicit source fact for every
    route. It uses the MusicXML 1-128 numbering, so convert it to MIDI's
    0-127 pitch numbering before attaching it to parsed atomic Unpitched notes
    ahead of repeat expansion or MIDI writing.
    """
    source_pitches_by_part = _unpitched_midi_pitches_by_part(source_path)
    for index, part in enumerate(score.parts):
        source_pitches = source_pitches_by_part.get(raw_part_ids.get(index, ""))
        if not source_pitches:
            continue
        parsed_notes = list(_atomic_unpitched_notes(part))
        # Only apply the source sequence when every source note is represented
        # by music21. A malformed score must retain its existing fallback
        # behaviour rather than shifting drum pitches onto later notes.
        if len(parsed_notes) != len(source_pitches):
            continue
        for parsed_note, midi_pitch in zip(parsed_notes, source_pitches):
            if midi_pitch is None:
                continue
            percussion_instrument = instrument.UnpitchedPercussion()
            percussion_instrument.midiChannel = 9
            percussion_instrument.percMapPitch = midi_pitch
            parsed_note.storedInstrument = percussion_instrument


def _atomic_unpitched_notes(part: stream.Part) -> List[note.Unpitched]:
    result: List[note.Unpitched] = []
    for element in part.recurse().notes:
        if isinstance(element, percussion.PercussionChord):
            result.extend(
                item for item in element.notes if isinstance(item, note.Unpitched)
            )
        elif isinstance(element, note.Unpitched):
            result.append(element)
    return result


def _unpitched_midi_pitches_by_part(source_path: Path) -> Dict[str, List[int | None]]:
    root = ElementTree.fromstring(read_musicxml_content(source_path))
    result: Dict[str, List[int | None]] = {}
    pitches_by_part_and_instrument: Dict[str, Dict[str, int]] = {}
    for score_part in _children_named(_first_child_named(root, "part-list"), "score-part"):
        raw_part_id = str(score_part.attrib.get("id") or "")
        pitches_by_instrument_id: Dict[str, int] = {}
        for midi_instrument in _children_named(score_part, "midi-instrument"):
            instrument_id = str(midi_instrument.attrib.get("id") or "")
            pitch = _valid_midi_unpitched(_child_text(midi_instrument, "midi-unpitched"))
            if instrument_id and pitch is not None:
                pitches_by_instrument_id[instrument_id] = pitch
        if not pitches_by_instrument_id:
            continue
        result[raw_part_id] = []
        pitches_by_part_and_instrument[raw_part_id] = pitches_by_instrument_id

    for raw_part in _children_named(root, "part"):
        raw_part_id = str(raw_part.attrib.get("id") or "")
        if raw_part_id not in result:
            continue
        for source_note in _descendants_named(raw_part, "note"):
            if _first_child_named(source_note, "unpitched") is None:
                continue
            source_instrument = _first_child_named(source_note, "instrument")
            instrument_id = (
                str(source_instrument.attrib.get("id") or "")
                if source_instrument is not None
                else ""
            )
            result[raw_part_id].append(
                pitches_by_part_and_instrument[raw_part_id].get(instrument_id)
            )
    return result


def _valid_midi_unpitched(value: str | None) -> int | None:
    try:
        musicxml_pitch = int(value) if value is not None else None
    except ValueError:
        return None
    # Unlike raw MIDI, MusicXML defines midi-unpitched as 1..128. Convert to
    # the 0..127 value consumed by music21's percussion MIDI writer.
    if musicxml_pitch is None or not 1 <= musicxml_pitch <= 128:
        return None
    return musicxml_pitch - 1


def _children_named(element: ElementTree.Element | None, name: str) -> List[ElementTree.Element]:
    if element is None:
        return []
    return [child for child in element if _local_name(child.tag) == name]


def _descendants_named(element: ElementTree.Element, name: str) -> List[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _first_child_named(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next((child for child in element if _local_name(child.tag) == name), None)


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    child = _first_child_named(element, name)
    return (child.text or "").strip() if child is not None else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _copy_score_tempos(source_score: stream.Score, target_score: stream.Score) -> None:
    """Preserve conductor tempo events when exporting only selected parts.

    MusicXML direction marks can be attached to the top-level score rather
    than an individual instrument part.  A new score containing only copied
    parts otherwise writes MIDI at music21's default 120 quarter-notes/minute.
    """
    for offset, bpm in _canonical_tempo_events(source_score):
        target_score.insert(offset, tempo.MetronomeMark(number=bpm))


def _canonical_tempo_events(score: stream.Score) -> List[Tuple[float, float]]:
    """Return the score-wide tempo map in played beat offsets.

    A MusicXML tempo direction can belong to a vocal part that is deliberately
    excluded from instrumental export.  Keep the map score-wide rather than
    taking timings from just the exported parts.  Identical directions at the
    same offset (common in multi-part scores) are emitted only once.
    """
    events: List[Tuple[float, float]] = []
    for mark in score.recurse().getElementsByClass(tempo.MetronomeMark):
        bpm = mark.getQuarterBPM() if hasattr(mark, "getQuarterBPM") else mark.number
        if bpm is None or float(bpm) <= 0:
            continue
        try:
            offset = float(mark.getOffsetInHierarchy(score))
        except Exception:
            offset = float(mark.offset)
        events.append((offset, float(bpm)))
    if not events:
        return [(0.0, 120.0)]
    events.sort(key=lambda event: event[0])
    deduped: List[Tuple[float, float]] = []
    for event in events:
        if (
            deduped
            and abs(event[0] - deduped[-1][0]) < 1e-9
            and abs(event[1] - deduped[-1][1]) < 1e-9
        ):
            continue
        deduped.append(event)
    return deduped


def _replace_midi_tempo_map(
    output_path: Path,
    tempo_events: Sequence[Tuple[float, float]],
) -> None:
    """Write the canonical score tempo map to the MIDI conductor track.

    music21's score writer drops a zero-offset tempo placed on a reconstructed
    top-level Score when the original owning part is omitted.  MIDI's standard
    Set Tempo meta event belongs in the conductor track, so rewrite that track
    after music21 has emitted the notes.  This keeps browser MIDI parsers and
    the vocal/score time axis on the same tempo map.
    """
    midi_file = midi.MidiFile()
    midi_file.open(str(output_path))
    midi_file.read()
    midi_file.close()
    if not midi_file.tracks:
        raise ValueError("MIDI export contains no tracks.")

    for track in midi_file.tracks:
        _remove_track_tempo_events(track)

    conductor = midi_file.tracks[0]
    absolute_events = _absolute_track_events(conductor)
    for offset_beats, bpm in tempo_events:
        tick = max(0, round(offset_beats * midi_file.ticksPerQuarterNote))
        absolute_events.append((tick, _midi_set_tempo_event(conductor, bpm)))
    _set_absolute_track_events(conductor, absolute_events)

    midi_file.open(str(output_path), "wb")
    try:
        midi_file.write()
    finally:
        midi_file.close()


def _remove_track_tempo_events(track: midi.MidiTrack) -> None:
    retained = [
        (absolute_tick, event)
        for absolute_tick, event in _absolute_track_events(track)
        if event.type != midi.MetaEvents.SET_TEMPO
    ]
    _set_absolute_track_events(track, retained)


def _absolute_track_events(track: midi.MidiTrack) -> List[Tuple[int, midi.MidiEvent]]:
    """Convert music21's delta-time event pairs to absolute ticks."""
    result: List[Tuple[int, midi.MidiEvent]] = []
    absolute_tick = 0
    for delta, event in zip(track.events[::2], track.events[1::2]):
        absolute_tick += int(delta.time)
        result.append((absolute_tick, event))
    return result


def _set_absolute_track_events(
    track: midi.MidiTrack,
    absolute_events: Sequence[Tuple[int, midi.MidiEvent]],
) -> None:
    """Replace a track's events while preserving their absolute positions."""
    previous_tick = 0
    ordered_events = sorted(
        absolute_events,
        key=lambda item: (
            item[0],
            2 if item[1].type == midi.MetaEvents.END_OF_TRACK else 0,
        ),
    )
    rebuilt: List[midi.DeltaTime | midi.MidiEvent] = []
    for absolute_tick, event in ordered_events:
        tick = max(previous_tick, int(absolute_tick))
        event.track = track
        rebuilt.append(midi.DeltaTime(track=track, time=tick - previous_tick))
        rebuilt.append(event)
        previous_tick = tick
    track.events = rebuilt


def _midi_set_tempo_event(track: midi.MidiTrack, bpm: float) -> midi.MidiEvent:
    microseconds_per_quarter = round(60_000_000 / bpm)
    if not 1 <= microseconds_per_quarter <= 0xFFFFFF:
        raise ValueError(f"Tempo cannot be represented in MIDI: {bpm} BPM.")
    event = midi.MidiEvent(track=track, type=midi.MetaEvents.SET_TEMPO)
    event.data = microseconds_per_quarter.to_bytes(3, byteorder="big")
    return event


def _strip_navigation_for_written_order_midi(score: stream.Score) -> None:
    """Stop music21's MIDI writer from implicitly expanding written repeats."""
    for measure in score.recurse().getElementsByClass(stream.Measure):
        if isinstance(measure.leftBarline, bar.Repeat):
            measure.leftBarline = bar.Barline("regular")
        if isinstance(measure.rightBarline, bar.Repeat):
            measure.rightBarline = bar.Barline("regular")
        for element in list(measure.recurse()):
            if isinstance(element, repeat.RepeatExpression):
                try:
                    element.activeSite.remove(element)
                except (AttributeError, ValueError):
                    pass
