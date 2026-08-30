"""Generate local instrumental MIDI performance artifacts from MusicXML."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from music21 import bar, instrument, repeat, stream, tempo

from src.musicxml.parser import _expand_repeat_navigation
from src.musicxml.part_reference import load_musicxml_score, map_parser_part_indices_to_raw_part_ids
from src.musicxml.instrument_programs import instrumental_programs_by_part


PERFORMANCE_MIDI_VERSION = 4


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


def _copy_score_tempos(source_score: stream.Score, target_score: stream.Score) -> None:
    """Preserve conductor tempo events when exporting only selected parts.

    MusicXML direction marks can be attached to the top-level score rather
    than an individual instrument part.  A new score containing only copied
    parts otherwise writes MIDI at music21's default 120 quarter-notes/minute.
    """
    for mark in source_score.recurse().getElementsByClass(tempo.MetronomeMark):
        try:
            offset = float(mark.getOffsetInHierarchy(source_score))
        except Exception:
            offset = float(mark.offset)
        target_score.insert(offset, deepcopy(mark))


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
