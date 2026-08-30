"""Extract MusicXML instrument facts and resolve browser playback presets.

Only portable General MIDI is resolved automatically. Source bank selections
are device-specific facts, never a numeric mapping to FluidR3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping
from xml.etree import ElementTree
import copy

from src.musicxml.io import read_musicxml_content


FLUIDR3_SOUNDFONT_ID = "FluidR3_GM"
FLUIDR3_MELODIC_BANK = 0
FLUIDR3_PERCUSSION_BANK = 128
FLUIDR3_PERCUSSION_PROGRAMS = {0, 8, 16, 24, 25, 32, 40, 48}


def build_instrument_program_summary(
    source_path: Path,
    parts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Attach factual instrument details to parser-visible score parts.

    ``parts`` is the score-summary part list.  It is updated in place so the
    LLM has the evidence next to each part; the compact top-level section only
    lists unresolved IDs and deliberately does not duplicate the evidence.
    """
    declarations = _read_part_declarations(source_path)
    unresolved_ids: List[str] = []
    unresolved_reasons: Dict[str, str] = {}
    instrumental_ids: List[str] = []
    seen_ids: set[str] = set()

    for part in parts:
        raw_part_id = str(part.get("raw_part_id") or part.get("part_id") or "")
        declaration = declarations.get(raw_part_id, {})
        instruments = _part_instruments(
            raw_part_id,
            declaration,
            has_lyrics=bool(part.get("has_lyrics")),
            note_count=int(part.get("note_count") or 0),
        )
        part["instruments"] = instruments
        for instrument in instruments:
            if not instrument["eligible_for_instrumental_midi"]:
                continue
            instrument_id = instrument["score_instrument_id"]
            # The parser can expose the same MusicXML route on multiple staff
            # views. The exporter has one track for that route, so resolve it once.
            if instrument_id in seen_ids:
                continue
            seen_ids.add(instrument_id)
            instrumental_ids.append(instrument_id)
            if instrument.get("playback_preset") is None:
                unresolved_ids.append(instrument_id)
                unresolved_reasons[instrument_id] = str(instrument.get("program_source") or "unresolved")

    return {
        "instrumental_score_instrument_ids": instrumental_ids,
        "unresolved_score_instrument_ids": unresolved_ids,
        "unresolved_reasons": unresolved_reasons,
        "playback_profile": FLUIDR3_SOUNDFONT_ID,
    }


def instrumental_programs_by_part(
    source_path: Path,
    *,
    assignments: Mapping[str, Any] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Return the factual/validated program choice for each MIDI-eligible part.

    This is used at MIDI-export time.  The caller must already have validated
    ``assignments``; values in the mapping are never guessed here.
    """
    declarations = _read_part_declarations(source_path)
    result: Dict[str, Dict[str, Any]] = {}
    for raw_part_id, declaration in declarations.items():
        instruments = _part_instruments(
            raw_part_id,
            declaration,
            has_lyrics=bool(declaration.get("has_lyrics")),
            note_count=1,
        )
        eligible = [item for item in instruments if item["eligible_for_instrumental_midi"]]
        if not eligible:
            continue
        # The existing MIDI exporter emits one track per score part.  MusicXML
        # files with in-score instrument changes retain all declarations in the
        # parse summary, but their event-level routing is outside this first
        # per-part playback implementation.
        primary = eligible[0]
        instrument_id = primary["score_instrument_id"]
        override = _normalise_preset(assignments.get(instrument_id)) if assignments else None
        preset = override or primary.get("playback_preset")
        program = preset.get("program") if isinstance(preset, dict) else None
        result[raw_part_id] = {
            **primary,
            "playback_preset": preset,
            "resolved_gm_program": program,
            "program_source": "llm_inferred" if override else primary["program_source"],
        }
    return result


def apply_llm_program_assignments(
    score_summary: Mapping[str, Any], assignments: Any
) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Validate and persist LLM-selected FluidR3 playback presets."""
    summary = copy.deepcopy(dict(score_summary))
    resolution = summary.get("instrument_program_resolution")
    if not isinstance(resolution, dict):
        return summary, None
    expected = resolution.get("unresolved_score_instrument_ids")
    expected_ids = [str(value) for value in expected if isinstance(value, str) and value]
    if not expected_ids:
        if assignments not in (None, []):
            return summary, _resolution_required_payload(
                [], "Playback preset assignments are only accepted for unresolved score-instruments."
            )
        return summary, None

    if assignments is None:
        assignments = []
    if not isinstance(assignments, list):
        return summary, _resolution_required_payload(expected_ids, "instrument_program_assignments must be an array.")

    instrument_index = _summary_instrument_index(summary)
    supplied: Dict[str, Dict[str, Any]] = {}
    invalid: List[str] = []
    for item in assignments:
        if not isinstance(item, dict):
            invalid.append("Each instrument assignment must be an object.")
            continue
        instrument_id = item.get("score_instrument_id")
        source = item.get("source")
        if not isinstance(instrument_id, str) or not instrument_id:
            invalid.append("Each instrument assignment needs score_instrument_id.")
            continue
        if instrument_id in supplied:
            invalid.append(f"Duplicate assignment for {instrument_id}.")
            continue
        if instrument_id not in expected_ids:
            invalid.append(f"{instrument_id} is not awaiting a program assignment.")
            continue
        if source != "llm_inferred":
            invalid.append(f"{instrument_id} must set source to llm_inferred.")
            continue
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
            invalid.append(f"{instrument_id} evidence must be an array of strings.")
            continue
        preset = _normalise_preset(item.get("playback_preset"))
        matching_instruments = instrument_index.get(instrument_id, [])
        if not preset or not matching_instruments or not _valid_preset_for_instrument(preset, matching_instruments[0]):
            invalid.append(f"{instrument_id} needs a valid FluidR3 playback_preset for this route.")
            continue
        supplied[instrument_id] = {"playback_preset": preset, "evidence": list(evidence)}

    if invalid:
        return summary, _resolution_required_payload(expected_ids, " ".join(invalid))

    missing = [instrument_id for instrument_id in expected_ids if instrument_id not in supplied]
    if missing:
        return summary, _resolution_required_payload(missing, "A FluidR3 playback preset is required for every listed instrumental route.")

    for instrument_id, assignment in supplied.items():
        for instrument in instrument_index[instrument_id]:
            instrument["playback_preset"] = assignment["playback_preset"]
            instrument["resolved_gm_program"] = assignment["playback_preset"]["program"]
            instrument["program_source"] = "llm_inferred"
            instrument["program_evidence"] = assignment["evidence"]
    resolution["unresolved_score_instrument_ids"] = []
    resolution["unresolved_reasons"] = {}
    return summary, None


def llm_program_assignments_from_summary(score_summary: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return all validated target presets keyed by MusicXML instrument ID."""
    result: Dict[str, Dict[str, Any]] = {}
    for instrument_id, instruments in _summary_instrument_index(score_summary).items():
        preset = _normalise_preset(instruments[0].get("playback_preset"))
        if preset:
            result[instrument_id] = preset
    return result


def _summary_instrument_index(summary: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    parts = summary.get("parts")
    if not isinstance(parts, list):
        return result
    for part in parts:
        if not isinstance(part, dict):
            continue
        instruments = part.get("instruments")
        if not isinstance(instruments, list):
            continue
        for instrument in instruments:
            if not isinstance(instrument, dict):
                continue
            instrument_id = instrument.get("score_instrument_id")
            if isinstance(instrument_id, str) and instrument_id:
                result.setdefault(instrument_id, []).append(instrument)
    return result


def _resolution_required_payload(
    unresolved_ids: List[str], message: str
) -> Dict[str, Any]:
    return {
        "status": "action_required",
        "action": "instrument_program_resolution_required",
        "message": message,
        "unresolved_score_instrument_ids": unresolved_ids,
    }


def _read_part_declarations(source_path: Path) -> Dict[str, Dict[str, Any]]:
    root = ElementTree.fromstring(read_musicxml_content(source_path))
    score_parts: Dict[str, Dict[str, Any]] = {}
    for score_part in _children_named(_first_child_named(root, "part-list"), "score-part"):
        raw_part_id = str(score_part.attrib.get("id") or "")
        midi_devices = {
            str(device.attrib.get("id") or ""): (device.text or "").strip()
            for device in _children_named(score_part, "midi-device")
            if (device.text or "").strip()
        }
        instruments: List[Dict[str, Any]] = []
        for score_instrument in _children_named(score_part, "score-instrument"):
            virtual = _first_child_named(score_instrument, "virtual-instrument")
            instruments.append(
                {
                    "score_instrument_id": str(score_instrument.attrib.get("id") or ""),
                    "instrument_name": _child_text(score_instrument, "instrument-name"),
                    "instrument_sound": _child_text(score_instrument, "instrument-sound"),
                    "virtual_instrument_library": _child_text(virtual, "virtual-library"),
                    "virtual_instrument_name": _child_text(virtual, "virtual-name"),
                }
            )
        midi_by_id: Dict[str, Dict[str, Any]] = {}
        for midi_instrument in _children_named(score_part, "midi-instrument"):
            instrument_id = str(midi_instrument.attrib.get("id") or "")
            midi_by_id[instrument_id] = {
                "native_midi_program": _positive_int(_child_text(midi_instrument, "midi-program")),
                "native_midi_channel": _positive_int(_child_text(midi_instrument, "midi-channel")),
                "native_midi_bank": _nonnegative_int(_child_text(midi_instrument, "midi-bank")),
                "midi_unpitched_notes": _unique_ints(_child_text_values(midi_instrument, "midi-unpitched")),
            }
        score_parts[raw_part_id] = {
            "part_name": _child_text(score_part, "part-name"),
            "midi_devices": midi_devices,
            "instruments": instruments,
            "midi_by_id": midi_by_id,
            "direction_words": [],
            "has_lyrics": False,
        }

    for part in _children_named(root, "part"):
        raw_part_id = str(part.attrib.get("id") or "")
        declaration = score_parts.setdefault(
            raw_part_id,
            {"part_name": None, "midi_devices": {}, "instruments": [], "midi_by_id": {}, "direction_words": [], "has_lyrics": False},
        )
        for measure in _children_named(part, "measure"):
            measure_number = str(measure.attrib.get("number") or "")
            for direction in _children_named(measure, "direction"):
                for words in _descendants_named(direction, "words"):
                    text = (words.text or "").strip()
                    if text:
                        declaration["direction_words"].append(
                            {
                                "text": text,
                                "measure_number": measure_number or None,
                                "placement": direction.attrib.get("placement"),
                            }
                        )
            if any(
                (text.text or "").strip()
                for lyric in _descendants_named(measure, "lyric")
                for text in _children_named(lyric, "text")
            ):
                declaration["has_lyrics"] = True
    return score_parts


def _part_instruments(
    raw_part_id: str,
    declaration: Mapping[str, Any],
    *,
    has_lyrics: bool,
    note_count: int,
) -> List[Dict[str, Any]]:
    if note_count <= 0:
        return []
    declared = declaration.get("instruments")
    declared = declared if isinstance(declared, list) else []
    midi_by_id = declaration.get("midi_by_id")
    midi_by_id = midi_by_id if isinstance(midi_by_id, dict) else {}
    if not declared:
        # Malformed-but-common exports can omit score-instrument while still
        # declaring a MIDI instrument. Keep that declaration addressable
        # instead of throwing away the one explicit program fact it contains.
        declared = [
            {
                "score_instrument_id": instrument_id,
                "instrument_name": None,
                "instrument_sound": None,
                "virtual_instrument_library": None,
                "virtual_instrument_name": None,
                "synthetic": False,
            }
            for instrument_id in midi_by_id
        ] or [
            {
                "score_instrument_id": f"{raw_part_id}:default",
                "instrument_name": None,
                "instrument_sound": None,
                "virtual_instrument_library": None,
                "virtual_instrument_name": None,
                "synthetic": True,
            }
        ]
    result: List[Dict[str, Any]] = []
    for index, source in enumerate(declared):
        score_instrument_id = str(source.get("score_instrument_id") or f"{raw_part_id}:default:{index + 1}")
        native = midi_by_id.get(score_instrument_id)
        native = native if isinstance(native, dict) else {}
        native_channel = native.get("native_midi_channel")
        native_program = native.get("native_midi_program")
        native_bank = native.get("native_midi_bank")
        percussion = native_channel == 10
        synthetic = bool(source.get("synthetic"))
        # A synthetic placeholder cannot be targeted by the MIDI exporter and
        # must never make synthesis wait for an LLM assignment.
        has_explicit_midi = isinstance(native_program, int) or isinstance(native_channel, int)
        eligible = not synthetic and (not has_lyrics or has_explicit_midi)
        preset, program_source = _portable_preset(
            channel=native_channel, bank=native_bank, program=native_program
        )
        result.append(
            {
                "score_instrument_id": score_instrument_id,
                "instrument_name": source.get("instrument_name"),
                "instrument_sound": source.get("instrument_sound"),
                # The MusicXML `midi-device` ID directly identifies the same
                # score-instrument route; preserve the complete source map as
                # evidence instead of guessing a device from its display name.
                "midi_device": (declaration.get("midi_devices") or {}).get(score_instrument_id),
                "midi_devices": dict(declaration.get("midi_devices") or {}),
                "virtual_instrument_library": source.get("virtual_instrument_library"),
                "virtual_instrument_name": source.get("virtual_instrument_name"),
                "native_midi_program": native_program,
                "native_midi_channel": native_channel,
                "native_midi_bank": native_bank,
                "midi_unpitched_notes": list(native.get("midi_unpitched_notes") or []),
                "source_midi": {
                    "channel": native_channel,
                    "bank": native_bank,
                    "program": native_program,
                    "unpitched_notes": list(native.get("midi_unpitched_notes") or []),
                },
                "playback_preset": preset,
                "resolved_gm_program": preset["program"] if preset else None,
                "program_source": program_source,
                "program_evidence": ["midi-channel"] if program_source == "standard_gm_percussion" else ["midi-program"] if program_source == "musicxml_gm_program" else [],
                "direction_words": list(declaration.get("direction_words") or []),
                "eligible_for_instrumental_midi": eligible,
                "is_percussion": percussion,
                "synthetic": synthetic,
            }
        )
    return result


def _portable_preset(*, channel: Any, bank: Any, program: Any) -> tuple[Dict[str, Any] | None, str]:
    """Resolve only source facts with portable General MIDI semantics."""
    if isinstance(bank, int):
        return None, "unresolved_nonportable_bank"
    if channel == 10:
        return _preset(FLUIDR3_PERCUSSION_BANK, 0, "percussion_kit"), "standard_gm_percussion"
    if isinstance(program, int) and 1 <= program <= 128:
        return _preset(FLUIDR3_MELODIC_BANK, program - 1, "melodic"), "musicxml_gm_program"
    return None, "unresolved_missing_melodic_program"


def _preset(bank: int, program: int, kind: str) -> Dict[str, Any]:
    return {
        "soundfont_id": FLUIDR3_SOUNDFONT_ID,
        "bank": bank,
        "program": program,
        "kind": kind,
    }


def _normalise_preset(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    soundfont_id = value.get("soundfont_id")
    bank = value.get("bank")
    program = value.get("program")
    kind = value.get("kind")
    if (
        soundfont_id != FLUIDR3_SOUNDFONT_ID
        or isinstance(bank, bool)
        or not isinstance(bank, int)
        or isinstance(program, bool)
        or not isinstance(program, int)
        or not isinstance(kind, str)
    ):
        return None
    return _preset(bank, program, kind)


def _valid_preset_for_instrument(preset: Mapping[str, Any], instrument: Mapping[str, Any]) -> bool:
    if instrument.get("is_percussion"):
        return (
            preset.get("bank") == FLUIDR3_PERCUSSION_BANK
            and preset.get("kind") == "percussion_kit"
            and preset.get("program") in FLUIDR3_PERCUSSION_PROGRAMS
        )
    return (
        preset.get("bank") == FLUIDR3_MELODIC_BANK
        and preset.get("kind") == "melodic"
        and isinstance(preset.get("program"), int)
        and 0 <= preset["program"] <= 127
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children_named(element: ElementTree.Element | None, name: str) -> Iterable[ElementTree.Element]:
    if element is None:
        return []
    return [child for child in element if _local_name(child.tag) == name]


def _descendants_named(element: ElementTree.Element, name: str) -> Iterable[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _first_child_named(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next(iter(_children_named(element, name)), None)


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    child = _first_child_named(element, name)
    value = (child.text or "").strip() if child is not None else ""
    return value or None


def _child_text_values(element: ElementTree.Element, name: str) -> Iterable[str | None]:
    return [((child.text or "").strip() or None) for child in _children_named(element, name)]


def _positive_int(value: str | None) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if isinstance(parsed, int) and parsed > 0 else None


def _nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, int) and parsed >= 0 else None


def _unique_ints(values: Iterable[str | None]) -> List[int]:
    result: List[int] = []
    for value in values:
        parsed = _nonnegative_int(value)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result
