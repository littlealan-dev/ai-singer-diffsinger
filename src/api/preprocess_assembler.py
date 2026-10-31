"""Assemble an offline preprocess plan from a deterministic decision sheet."""

from __future__ import annotations

from copy import deepcopy

from src.api.preprocess_sheet import Lane, Option, Segment, Sheet


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


def _action_for_measure(
    sheet: Sheet, selected: dict[str, Option], lane: Lane, segment: Segment, measure: int,
) -> dict:
    fields: dict = {}
    assigned_group_size: int | None = None
    for kind, option in _options_for_measure(sheet, selected, lane, measure):
        if kind == "split_shared_note_policy":
            continue
        for key, value in option.fields.items():
            if key == "melody_source_by_lane":
                source = value.get(lane.lane_id)
                if not isinstance(source, dict):
                    raise AssemblyError(f"Missing voice assignment for {lane.lane_id}")
                part_id, voice_part_id = source.get("part_id"), source.get("voice_part_id")
                if not isinstance(part_id, str) or not part_id or not isinstance(voice_part_id, str) or not voice_part_id:
                    raise AssemblyError(f"Invalid voice assignment for {lane.lane_id}")
                assigned_group_size = sum(item == source for item in value.values())
                key, value = "melody_source", {"part_id": part_id, "voice_part_id": voice_part_id}
            elif key == "rank_index_by_lane":
                if lane.lane_id not in value:
                    if kind == "voice_assignment":
                        continue
                    raise AssemblyError(f"Missing rank for {lane.lane_id}")
                key, value = "rank_index", value[lane.lane_id]
            elif key in {"melody_source", "lyric_source"}:
                value = _public_source_ref(option, value)
            if key in fields and fields[key] != value:
                raise AssemblyError(f"Conflicting {key} choices for {lane.lane_id}")
            fields[key] = deepcopy(value)

    mode = fields.get("mode")
    if mode == "rest":
        return {"mode": "rest", "decision_type": "INSERT_RESTS"}
    if mode != "derive":
        raise AssemblyError(f"Missing mode for {lane.lane_id} measure {measure}")
    melody, lyric = fields.get("melody_source"), fields.get("lyric_source")
    if not isinstance(melody, dict) or not isinstance(lyric, dict):
        raise AssemblyError(f"Derive needs melody and lyric sources for {lane.lane_id}")

    lyric_kind = next((kind for lane_id, _, kind in segment.signature.lanes
                       if lane_id == lane.lane_id), None)
    if lyric_kind is None:
        raise AssemblyError(f"Missing lane facts for {lane.lane_id}")
    if lyric_kind == "extension_only" and "lyric_policy" not in fields:
        raise AssemblyError(f"Extension-only lyric policy is unresolved for {lane.lane_id}")
    fields.setdefault("lyric_policy", "fill_missing_only" if lyric_kind == "words" else "replace_all")
    fields.setdefault("lyric_strategy", "strict_onset")

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
    return fields


def assemble_plan(
    sheet: Sheet,
    choices: dict[str, str],
    *,
    freeform_sections: dict[str, list[dict]] | None = None,
) -> dict:
    """Fill code-owned fields and merge equal per-measure actions exactly."""
    selected = _selected_options(sheet, choices)
    freeform = {slot.slot_id: slot for slot in sheet.slots if slot.kind == "freeform"}
    supplied = freeform_sections or {}
    missing = sorted(freeform.keys() - supplied.keys())
    if missing:
        raise FreeformUnresolved(missing)
    if supplied.keys() - freeform.keys():
        raise ValueError(f"Unknown freeform slot IDs: {sorted(supplied.keys() - freeform.keys())}")
    freeform_actions: dict[str, dict[int, dict]] = {}
    for slot_id, slot in freeform.items():
        actions = expand(supplied[slot_id])
        if set(actions) != set(range(slot.start_measure, slot.end_measure + 1)):
            raise AssemblyError(f"Freeform sections do not cover {slot_id} exactly")
        freeform_actions[slot_id] = actions

    targets: list[dict] = []
    for lane in sheet.lanes:
        segments = [segment for segment in sheet.segments
                    if segment.part_index == lane.part_index and segment.scope_key == lane.scope_key]
        per_measure: dict[int, dict] = {}
        for segment in segments:
            freeform_slot = next((slot for slot in freeform.values()
                                  if lane.lane_id in slot.lane_ids
                                  and (slot.start_measure, slot.end_measure) ==
                                      (segment.start_measure, segment.end_measure)), None)
            for measure in range(segment.start_measure, segment.end_measure + 1):
                if measure in per_measure:
                    raise AssemblyError(f"Overlapping segments for {lane.lane_id} at {measure}")
                per_measure[measure] = (
                    deepcopy(freeform_actions[freeform_slot.slot_id][measure])
                    if freeform_slot is not None else
                    _action_for_measure(sheet, selected, lane, segment, measure)
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
            if kind == "split_shared_note_policy"
        }
        if len(policies) > 1:
            raise AssemblyError(f"Conflicting shared-note policies for {lane.lane_id}")
        if policies:
            target["split_shared_note_policy"] = policies.pop()
        targets.append(target)
    return {"targets": targets}
