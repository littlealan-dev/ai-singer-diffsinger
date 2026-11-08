"""Assemble an offline preprocess plan from a deterministic decision sheet."""

from __future__ import annotations

from copy import deepcopy

from src.api.preprocess_sheet import (
    Lane, Option, Segment, Sheet, StaffLyricLine, clash_bars, line_voice_for_staff, melody_line_staffs,
    note_staffs_by_measure, staff_lyric_lines, lyric_bar_cases,
)


class AssemblyError(ValueError):
    """The chosen sheet cannot be represented as an exact public plan."""


class FreeformUnresolved(AssemblyError):
    def __init__(self, slot_ids: list[str]):
        self.slot_ids = sorted(slot_ids)
        super().__init__(f"Freeform sections required for: {', '.join(self.slot_ids)}")


def first_option_choices(sheet: Sheet) -> dict[str, str]:
    """Choose each menu's deterministic first option; freeform remains unresolved."""
    return {
        slot.slot_id: slot.options[0].option_id
        for slot in sheet.slots if slot.kind != "freeform" and slot.options
    }


def run_length_merge(per_measure: dict[int, dict]) -> list[dict]:
    """Merge equal actions only across consecutive measure numbers."""
    sections: list[dict] = []
    for measure, action in sorted(per_measure.items()):
        if not isinstance(measure, int) or isinstance(measure, bool) or measure < 1:
            raise AssemblyError(f"Invalid measure number: {measure!r}")
        if not isinstance(action, dict) or "start_measure" in action or "end_measure" in action:
            raise AssemblyError(f"Invalid action at measure {measure}")
        if (sections and measure == sections[-1]["end_measure"] + 1
                and action == {key: value for key, value in sections[-1].items()
                               if key not in {"start_measure", "end_measure"}}):
            sections[-1]["end_measure"] = measure
        else:
            sections.append({"start_measure": measure, "end_measure": measure, **deepcopy(action)})
    return sections


def expand(sections: list[dict]) -> dict[int, dict]:
    """Expand inclusive sections, rejecting malformed ranges and overlaps."""
    per_measure: dict[int, dict] = {}
    for section in sections:
        if not isinstance(section, dict):
            raise AssemblyError("Section must be an object")
        start, end = section.get("start_measure"), section.get("end_measure")
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise AssemblyError(f"Invalid section range: {start!r}-{end!r}")
        action = {key: deepcopy(value) for key, value in section.items()
                  if key not in {"start_measure", "end_measure"}}
        for measure in range(start, end + 1):
            if measure in per_measure:
                raise AssemblyError(f"Overlapping section at measure {measure}")
            per_measure[measure] = deepcopy(action)
    return per_measure


def _selected_options(sheet: Sheet, choices: dict[str, str]) -> dict[str, Option]:
    menus = {slot.slot_id: slot for slot in sheet.slots if slot.kind != "freeform"}
    if len(menus) != sum(slot.kind != "freeform" for slot in sheet.slots):
        raise AssemblyError("Duplicate slot IDs")
    if not isinstance(choices, dict):
        raise ValueError("Choices must be an object")
    missing = sorted(menus.keys() - choices.keys())
    unknown = sorted(choices.keys() - menus.keys())
    if missing or unknown:
        raise ValueError(f"Missing choices: {missing}; unknown slot IDs: {unknown}")
    selected = dict(sheet.fixed)
    for slot_id, slot in menus.items():
        option = next((option for option in slot.options
                       if option.option_id == choices[slot_id]), None)
        if option is None:
            raise ValueError(f"Unknown option ID {choices[slot_id]!r} for {slot_id}")
        selected[slot_id] = option
    return selected


def _covers_fixed(slot_id: str, option: Option, lane: Lane, measure: int) -> bool:
    try:
        prefix, span, kind = slot_id.rsplit("|", 2)
        start_text, end_text = span.split("-", 1)
        start, end = int(start_text), int(end_text)
    except (ValueError, TypeError) as exc:
        raise AssemblyError(f"Invalid fixed slot ID: {slot_id}") from exc
    if not start <= measure <= end:
        return False
    return prefix == lane.lane_id or (
        kind == "rank_assignment"
        and lane.lane_id in option.fields.get("rank_index_by_lane", {})
    ) or (
        kind == "voice_assignment"
        and lane.lane_id in option.fields.get("melody_source_by_lane", {})
    )


def _options_for_measure(
    sheet: Sheet, selected: dict[str, Option], lane: Lane, measure: int,
) -> list[tuple[str, Option]]:
    covered = [
        (slot.kind, selected[slot.slot_id])
        for slot in sheet.slots
        if slot.kind != "freeform" and lane.lane_id in slot.lane_ids
        and slot.start_measure <= measure <= slot.end_measure
    ]
    covered.extend(
        (slot_id.rsplit("|", 1)[-1], option)
        for slot_id, option in sheet.fixed.items()
        if _covers_fixed(slot_id, option, lane, measure)
    )
    return covered


def _public_source_ref(option: Option, source: dict) -> dict[str, str]:
    voice_part_id = source.get("voice_part_id") if isinstance(source, dict) else None
    if not isinstance(voice_part_id, str) or not voice_part_id:
        raise AssemblyError(f"Invalid source option: {option.option_id}")
    suffix = f"|{voice_part_id}"
    if not option.option_id.endswith(suffix):
        raise AssemblyError(f"Source option ID does not match its fields: {option.option_id}")
    part_id = option.option_id[:-len(suffix)]
    if not part_id:
        raise AssemblyError(f"Missing source part ID: {option.option_id}")
    return {"part_id": part_id, "voice_part_id": voice_part_id}


def _line_voice(sheet: Sheet, selected: dict[str, Option], lane: Lane, measure: int) -> tuple[int, str] | None:
    """The lyric_line voice chosen for a lane at a measure, if any."""
    voices = {
        (option.fields["lyric_line_voice"]["part_index"], option.fields["lyric_line_voice"]["voice_part_id"])
        for kind, option in _options_for_measure(sheet, selected, lane, measure)
        if kind == "lyric_line"
    }
    if len(voices) > 1:
        raise AssemblyError(f"Conflicting lyric_line choices for {lane.lane_id}")
    return voices.pop() if voices else None


def chosen_line_voices(sheet: Sheet, choices: dict[str, str]) -> dict[str, dict[int, tuple[int, str]]]:
    """Per lane and measure, the (part_index, voice_part_id) whose line was chosen."""
    selected = _selected_options(sheet, choices)
    result: dict[str, dict[int, tuple[int, str]]] = {}
    for lane in sheet.lanes:
        for segment in sheet.segments:
            if (segment.part_index, segment.scope_key) != (lane.part_index, lane.scope_key):
                continue
            for measure in range(segment.start_measure, segment.end_measure + 1):
                voice = _line_voice(sheet, selected, lane, measure)
                if voice is not None:
                    result.setdefault(lane.lane_id, {})[measure] = voice
    return result


def _action_for_measure(
    sheet: Sheet, selected: dict[str, Option], lane: Lane, segment: Segment, measure: int,
    score: dict, lines: dict[tuple[int, str], StaffLyricLine], staff_index: dict,
) -> tuple[dict, tuple | None]:
    """The measure's action, and its lyric reading (staffs, line voice, melody ref) when code reads the page."""
    fields: dict = {}
    assigned_group_size: int | None = None
    melody_ref: tuple[int, str] | None = None
    missing_assignment = False
    covered = _options_for_measure(sheet, selected, lane, measure)
    for kind, option in covered:
        if kind in {"split_shared_note_policy", "unison_default", "one_line", "lyric_line", "lyric_reading"}:
            continue
        for key, value in option.fields.items():
            if key == "melody_source_by_lane":
                source = value.get(lane.lane_id)
                if not isinstance(source, dict):
                    missing_assignment = True
                    continue
                part_id, voice_part_id = source.get("part_id"), source.get("voice_part_id")
                if not isinstance(part_id, str) or not part_id or not isinstance(voice_part_id, str) or not voice_part_id:
                    raise AssemblyError(f"Invalid voice assignment for {lane.lane_id}")
                assigned_group_size = sum(item == source for item in value.values())
                melody_ref = (source.get("part_index"), voice_part_id)
                key, value = "melody_source", {"part_id": part_id, "voice_part_id": voice_part_id}
            elif key == "rank_index_by_lane":
                if lane.lane_id not in value:
                    if kind == "voice_assignment":
                        continue
                    raise AssemblyError(f"Missing rank for {lane.lane_id}")
                key, value = "rank_index", value[lane.lane_id]
            elif key in {"melody_source", "lyric_source"}:
                if key == "melody_source":
                    melody_ref = (value.get("part_index"), value.get("voice_part_id"))
                value = _public_source_ref(option, value)
            if key in fields and fields[key] != value:
                raise AssemblyError(f"Conflicting {key} choices for {lane.lane_id}")
            fields[key] = deepcopy(value)

    if "mode" not in fields and any(kind == "one_line" for kind, _ in covered):
        # The staff writes one line here and no bar answer overrides: the lane's unison default decides.
        default = next((option for kind, option in covered if kind == "unison_default"), None)
        fields["mode"] = "rest" if default is not None and default.option_id == "top_lane_only" else "derive"
    mode = fields.get("mode")
    if mode == "rest":
        return {"mode": "rest", "decision_type": "INSERT_RESTS"}, None
    if missing_assignment:
        raise AssemblyError(f"Missing voice assignment for {lane.lane_id}")
    if mode != "derive":
        raise AssemblyError(f"Missing mode for {lane.lane_id} measure {measure}")
    melody = fields.get("melody_source")
    if not isinstance(melody, dict) or melody_ref is None or not isinstance(melody_ref[0], int):
        raise AssemblyError(f"Derive needs a melody source for {lane.lane_id}")

    staffs = (melody_line_staffs(staff_index, *melody_ref, measure, measure)
              or melody_line_staffs(staff_index, *melody_ref, segment.start_measure, segment.end_measure))
    reading = None
    if staffs and staffs <= lines.keys():
        # The staff lyric line is filled in after the measure's reading is known.
        fields.pop("lyric_source", None)
        fields.pop("lyric_strategy", None)
        reading_choice = next((option.fields["lyric_reading"] for kind, option in
                               _options_for_measure(sheet, selected, lane, measure)
                               if kind == "lyric_reading"), "own_words_only")
        reading = (staffs, _line_voice(sheet, selected, lane, measure), melody_ref, reading_choice)
    elif not isinstance(fields.get("lyric_source"), dict):
        raise AssemblyError(f"Derive needs a lyric source for {lane.lane_id}")
    else:
        fields.setdefault("lyric_strategy", "strict_onset")
    # Rule 19: copied lyrics always replace the lane's own marks.
    fields["lyric_policy"] = "replace_all"

    same_part = melody["part_id"] == lane.part_id
    siblings = sorted(
        (other for other in sheet.lanes
         if other.part_id == melody["part_id"]
         and other.scope_key == lane.scope_key
         and other.source_voice_part_id == melody["voice_part_id"]),
        key=lambda other: other.default_rank,
    )
    if ((assigned_group_size is not None and assigned_group_size > 1)
            or (assigned_group_size is None and len(siblings) > 1)):
        fields["decision_type"] = "SPLIT_CHORDS_SELECT_NOTES"
        fields["method"] = "ranked"
        fields.setdefault("rank_index", next(
            (index for index, sibling in enumerate(siblings) if sibling.lane_id == lane.lane_id), 0
        ))
        fields.setdefault("rank_fallback", "greedy")
    elif same_part and melody["voice_part_id"] == lane.source_voice_part_id:
        fields["decision_type"] = "EXTRACT_FROM_VOICE"
        fields["method"] = "trivial"
    else:
        fields["decision_type"] = "COPY_UNISON_SECTION"
        fields["method"] = "trivial"
    # as_lane: the bar's marking is about something else, so the lane's answer applies.
    bar = next((option for kind, option in covered if kind == "unison" and option.option_id != "as_lane"), None)
    if fields["method"] != "ranked":
        # A unison answer only applies where the lane splits chords by rank.
        fields.pop("split_shared_note_policy", None)
    elif bar is not None:
        # The bar's unison answer overrides the lane's either way; write it only where they differ.
        lane_policy = next((option.fields["split_shared_note_policy"] for kind, option in covered
                            if kind in {"split_shared_note_policy", "unison_default"}
                            and "split_shared_note_policy" in option.fields), "duplicate_to_all")
        wanted = "assign_primary_only" if bar.option_id == "top_lane_only" else "duplicate_to_all"
        if wanted == lane_policy:
            fields.pop("split_shared_note_policy", None)
        else:
            fields["split_shared_note_policy"] = wanted
    return fields, reading


def _reading_line_voice(
    score: dict, lines: dict[tuple[int, str], StaffLyricLine],
    measure: int, reading: tuple,
    case_cache: dict[tuple[int, str, str, str | None], dict],
) -> str | None:
    staffs, line_voice, melody_ref, reading_choice = reading
    if line_voice is not None and line_voice[0] == melody_ref[0]:
        for staff in sorted(staffs):
            if (measure in clash_bars(lines[staff])
                    and line_voice_for_staff(lines[staff], staff[0], line_voice) is not None):
                return line_voice[1]
    if len(staffs) == 1 and reading_choice == "own_words_only":
        staff = next(iter(staffs))
        only = (line_voice_for_staff(lines[staff], staff[0], line_voice)
                if measure in clash_bars(lines[staff]) else None)
        case_key = (staff[0], staff[1], melody_ref[1], only)
        if case_key not in case_cache:
            case_cache[case_key] = lyric_bar_cases(score, lines, *case_key)
        case = case_cache[case_key].get(measure)
        if case is not None and case.case == "ambiguous":
            return melody_ref[1]
    return None


def assemble_plan(
    sheet: Sheet,
    choices: dict[str, str],
    *,
    freeform_sections: dict[str, list[dict] | dict[str, list[dict]]] | None = None,
    score: dict,
) -> dict:
    """Fill code-owned fields and merge equal per-measure actions exactly.

    A freeform slot's sections are one list for all its lanes, or one list per lane id.
    """
    selected = _selected_options(sheet, choices)
    lines = staff_lyric_lines(score)
    staff_index = note_staffs_by_measure(score)
    freeform = {slot.slot_id: slot for slot in sheet.slots if slot.kind == "freeform"}
    supplied = freeform_sections or {}
    missing = sorted(freeform.keys() - supplied.keys())
    if missing:
        raise FreeformUnresolved(missing)
    if supplied.keys() - freeform.keys():
        raise ValueError(f"Unknown freeform slot IDs: {sorted(supplied.keys() - freeform.keys())}")
    freeform_actions: dict[tuple[str, str], dict[int, dict]] = {}
    for slot_id, slot in freeform.items():
        value = supplied[slot_id]
        by_lane = value if isinstance(value, dict) else {lane_id: value for lane_id in slot.lane_ids}
        if set(by_lane) != set(slot.lane_ids):
            raise AssemblyError(f"Freeform sections must name each lane of {slot_id} once")
        for lane_id, sections in by_lane.items():
            actions = expand(sections)
            if set(actions) != set(range(slot.start_measure, slot.end_measure + 1)):
                raise AssemblyError(f"Freeform sections do not cover {slot_id} exactly")
            freeform_actions[(slot_id, lane_id)] = actions

    targets: list[dict] = []
    for lane in sheet.lanes:
        segments = [segment for segment in sheet.segments
                    if segment.part_index == lane.part_index and segment.scope_key == lane.scope_key]
        per_measure: dict[int, dict] = {}
        readings: dict[int, tuple] = {}
        for segment in segments:
            freeform_slot = next((slot for slot in freeform.values()
                                  if lane.lane_id in slot.lane_ids
                                  and (slot.start_measure, slot.end_measure) ==
                                      (segment.start_measure, segment.end_measure)), None)
            for measure in range(segment.start_measure, segment.end_measure + 1):
                if measure in per_measure:
                    raise AssemblyError(f"Overlapping segments for {lane.lane_id} at {measure}")
                if freeform_slot is not None:
                    per_measure[measure] = deepcopy(
                        freeform_actions[(freeform_slot.slot_id, lane.lane_id)][measure]
                    )
                    continue
                per_measure[measure], reading = _action_for_measure(
                    sheet, selected, lane, segment, measure, score, lines, staff_index
                )
                if reading is not None:
                    readings[measure] = reading
        case_cache: dict[tuple[int, str, str, str | None], dict] = {}
        for measure, reading in readings.items():
            melody_ref = reading[2]
            per_measure[measure].update(
                lyric_source={"part_id": str(score["parts"][melody_ref[0]]["part_id"]),
                              "voice_part_id": melody_ref[1]},
                lyric_strategy="staff_line",
                lyric_source_line={"staff": None, "voice_part_id": _reading_line_voice(
                    score, lines, measure, reading, case_cache)},
            )
        if not per_measure:
            raise AssemblyError(f"No measures for {lane.lane_id}")
        measure_numbers = sorted(per_measure)
        if measure_numbers != list(range(1, measure_numbers[-1] + 1)):
            raise AssemblyError(f"Gap in target span for {lane.lane_id}")
        sections = run_length_merge(per_measure)
        if expand(sections) != per_measure:
            raise AssemblyError(f"Round-trip mismatch for {lane.lane_id}")
        output: dict[str, str] = {"mode": lane.output_mode}
        if lane.output_mode == "update_existing_derived_lane":
            if not lane.existing_derived_lane_id:
                raise AssemblyError(f"Update lane lacks derived_lane_id: {lane.lane_id}")
            output["derived_lane_id"] = lane.existing_derived_lane_id
        target: dict = {
            "source": {"part_id": lane.part_id, "voice_part_id": lane.source_voice_part_id},
            "output": output,
            "split_coverage": "complete",
            "sections": sections,
        }
        if sheet.verse_number is not None:
            target["verse_number"] = sheet.verse_number
        policies = {
            option.fields["split_shared_note_policy"]
            for kind, option in _options_for_measure(sheet, selected, lane, measure_numbers[0])
            if kind in {"split_shared_note_policy", "unison_default"}
            and "split_shared_note_policy" in option.fields
        }
        if len(policies) > 1:
            raise AssemblyError(f"Conflicting shared-note policies for {lane.lane_id}")
        if policies:
            target["split_shared_note_policy"] = policies.pop()
        targets.append(target)
    return {"targets": targets}
