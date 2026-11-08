"""Synthetic score coverage for the planner's same-staff lyric line."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.api import parse_score
from src.api.preprocess_assembler import assemble_plan, expand, first_option_choices
from src.api.preprocess_sheet import build_sheet, clash_bars, staff_lyric_lines
from src.api.voice_parts import _propagate_lyrics, parse_voice_part_plan, preprocess_voice_parts


def _n(duration: float, lyric: str | None = None, *, rest: bool = False,
       tie: str | None = None, staff: int = 1) -> dict:
    return {"duration": duration, "lyric": lyric, "rest": rest, "tie": tie, "staff": staff}


def _score(tmp_path, bars: list[dict[str, list[dict]]]) -> dict:
    measures = []
    for bar_number, voices in enumerate(bars, 1):
        chunks = []
        for voice_index, (voice, events) in enumerate(sorted(voices.items())):
            assert sum(event["duration"] for event in events) == 4
            if voice_index:
                chunks.append("<backup><duration>8</duration></backup>")
            for event in events:
                pitch = ("<rest/>" if event["rest"] else
                         f"<pitch><step>{ {'1': 'G', '2': 'E', '3': 'C'}[voice] }</step><octave>4</octave></pitch>")
                lyric = (f"<lyric><text>{event['lyric']}</text></lyric>"
                         if event["lyric"] is not None else "")
                tie = f"<tie type=\"{event['tie']}\"/>" if event["tie"] else ""
                chunks.append(f"<note>{pitch}<duration>{int(2 * event['duration'])}</duration>"
                              f"<voice>{voice}</voice><type>{ {0.5: 'eighth', 1: 'quarter', 2: 'half', 4: 'whole'}[event['duration']] }</type>"
                              f"<staff>{event['staff']}</staff>{tie}{lyric}</note>")
        attributes = ("<attributes><divisions>2</divisions><time><beats>4</beats>"
                      "<beat-type>4</beat-type></time>"
                      "<clef><sign>G</sign><line>2</line></clef></attributes>"
                      if bar_number == 1 else "")
        measures.append(f"<measure number=\"{bar_number}\">{attributes}{''.join(chunks)}</measure>")
    xml = ('<score-partwise version="3.1"><part-list><score-part id="P1">'
           '<part-name>Synthetic</part-name></score-part></part-list>'
           f'<part id="P1">{"".join(measures)}</part></score-partwise>')
    path = tmp_path / "synthetic.xml"
    path.write_text(xml, encoding="utf-8")
    return parse_score(path)


def _build(score: dict, choices: dict | None = None):
    sheet = build_sheet(score, {"target": {"part_id": score["parts"][0]["part_id"]}})
    selected = first_option_choices(sheet)
    if choices:
        for kind, answer in choices.items():
            for slot in sheet.slots:
                if slot.kind == kind:
                    selected[slot.slot_id] = answer
    plan = assemble_plan(sheet, selected, score=score)
    result = preprocess_voice_parts(deepcopy(score), request={"plan": plan})
    return sheet, plan, result


def _lane(plan: dict, result: dict, voice: str):
    index = next(index for index, target in enumerate(plan["targets"])
                 if target["source"]["voice_part_id"] == voice)
    ref = result["targets"][index]["appended_part_ref"]["part_index"]
    notes = [note for note in result["score"]["parts"][ref]["notes"]
             if not note.get("is_rest") and note.get("pitch_midi") is not None]
    return plan["targets"][index], notes


def test_tied_lane_note_keeps_extension_at_other_voice_word(tmp_path):
    score = _score(tmp_path, [
        {"1": [_n(4, "luma")], "2": [_n(4, None, tie="start")]},
        {"1": [_n(4, "nora")], "2": [_n(4, None, tie="stop")]},
    ])
    _, plan, result = _build(score)
    target, notes = _lane(plan, result, "voice part 2")
    assert result["status"] == "ready"
    assert [note.get("lyric") for note in notes] == ["luma", "+"]
    assert all(section["lyric_strategy"] == "staff_line" for section in target["sections"])


@pytest.mark.parametrize("other_voice_words", [3, 1])
def test_own_words_only_uses_phrase_start_word_independent_of_voice_word_count(
    tmp_path, other_voice_words,
):
    third = [_n(0.5, "rilo"), _n(0.5, "mira" if other_voice_words == 3 else None),
             _n(2, rest=True), _n(1, "vona" if other_voice_words == 3 else None)]
    score = _score(tmp_path, [
        {"1": [_n(4, "sura")], "2": [_n(4, "sura")], "3": [_n(4)]},
        {"1": [_n(1, rest=True), _n(1, "nora"), _n(1, "pema"), _n(1, rest=True)],
         "2": [_n(2, rest=True), _n(1), _n(1, rest=True)],
         "3": third},
    ])
    sheet, plan, result = _build(score)
    assert any(slot.kind == "lyric_reading" for slot in sheet.slots)
    target, notes = _lane(plan, result, "voice part 2")
    assert expand(target["sections"])[2]["lyric_source_line"] == {
        "staff": None, "voice_part_id": "voice part 2"}
    assert result["status"] != "action_required"
    assert [note.get("lyric") for note in notes if note["measure_number"] == 2] == ["pema"]


def test_clash_line_choice_applies_only_in_clash_bar(tmp_path):
    score = _score(tmp_path, [
        {"1": [_n(2, "luma"), _n(2, "nora")],
         "2": [_n(2, "sela"), _n(2, "tavi")]},
        {"1": [_n(2, "mira"), _n(2, "vona")],
         "2": [_n(2), _n(2)]},
    ])
    assert clash_bars(staff_lyric_lines(score)[(0, "1")]) == {1}
    sheet, plan, result = _build(score)
    assert any(slot.kind == "lyric_line" for slot in sheet.slots)
    target, notes = _lane(plan, result, "voice part 2")
    actions = expand(target["sections"])
    assert actions[1]["lyric_source_line"] == {"staff": None, "voice_part_id": "voice part 2"}
    assert actions[2]["lyric_source_line"] == {"staff": None, "voice_part_id": None}
    assert [note.get("lyric") for note in notes] == ["sela", "tavi", "mira", "vona"]


def test_each_own_line_stays_separate_across_clash_bars(tmp_path):
    score = _score(tmp_path, [
        {"1": [_n(2, "luma"), _n(2, "nora")],
         "2": [_n(2, "sela"), _n(2, "tavi")]},
        {"1": [_n(2, "mira"), _n(2, "vona")],
         "2": [_n(2, "rilo"), _n(2, "pema")]},
    ])
    _, plan, result = _build(score)
    assert result["status"] == "ready"
    for voice, expected in (("voice part 1", ["luma", "nora", "mira", "vona"]),
                            ("voice part 2", ["sela", "tavi", "rilo", "pema"])):
        target, notes = _lane(plan, result, voice)
        assert [note.get("lyric") for note in notes] == expected
        assert all(section["lyric_source_line"]["voice_part_id"] == voice
                   for section in target["sections"])


def test_ambiguous_bar_own_words_only_vs_share_line(tmp_path):
    score = _score(tmp_path, [{
        "1": [_n(1, rest=True), _n(1, "nora"), _n(1, "pema"), _n(1, rest=True)],
        "2": [_n(1, "sela"), _n(1), _n(2, rest=True)],
    }])
    own_sheet, own_plan, own_result = _build(score)
    assert any(slot.kind == "lyric_reading" for slot in own_sheet.slots)
    own_target, own_notes = _lane(own_plan, own_result, "voice part 2")
    assert own_target["sections"][0]["lyric_source_line"] == {
        "staff": None, "voice_part_id": "voice part 2"}
    assert [note.get("lyric") for note in own_notes] == ["sela", "+"]

    _, shared_plan, shared_result = _build(score, {"lyric_reading": "share_line"})
    shared_target, shared_notes = _lane(shared_plan, shared_result, "voice part 2")
    assert shared_target["sections"][0]["lyric_source_line"] == {
        "staff": None, "voice_part_id": None}
    assert [note.get("lyric") for note in shared_notes] == ["sela", "nora"]


def test_lane_notes_read_words_on_each_staff():
    # Passes today: staff_line already scopes word lookup by each target note's staff.
    source = [
        {"voice": "1", "staff": "1", "measure_number": 1, "offset_beats": 0.0,
         "duration_beats": 1.0, "pitch_midi": 60, "lyric": "luma"},
        {"voice": "1", "staff": "2", "measure_number": 1, "offset_beats": 1.0,
         "duration_beats": 1.0, "pitch_midi": 62, "lyric": "nora"},
    ]
    target = [
        {"voice": "2", "staff": "1", "measure_number": 1, "offset_beats": 0.0,
         "duration_beats": 1.0, "pitch_midi": 65},
        {"voice": "2", "staff": "2", "measure_number": 1, "offset_beats": 1.0,
         "duration_beats": 1.0, "pitch_midi": 67},
    ]
    copied, _ = _propagate_lyrics(
        target_notes=target, source_notes=source, strategy="staff_line", policy="replace_all",
        verse_number=None, copy_all_verses=False, measure_range=(1, 1),
    )
    assert [note.get("lyric") for note in copied] == ["luma", "nora"]


def test_one_text_line_reads_words_from_either_voice(tmp_path):
    # Passes today: the new line field leaves an unambiguous shared reading intact.
    score = _score(tmp_path, [{
        "1": [_n(2, "luma"), _n(2)],
        "2": [_n(2, "LUMA"), _n(2, "nora")],
    }])
    assert clash_bars(staff_lyric_lines(score)[(0, "1")]) == set()
    _, plan, result = _build(score)
    assert result["status"] == "ready"
    for voice in ("voice part 1", "voice part 2"):
        target, notes = _lane(plan, result, voice)
        assert target["sections"][0]["lyric_source_line"] == {
            "staff": None, "voice_part_id": None}
        assert [note.get("lyric") for note in notes] == ["luma", "nora"]


def test_staff_line_own_voice_exception_only_at_phrase_start():
    source = [
        {"voice": "1", "staff": "1", "measure_number": 1, "offset_beats": 0.0,
         "duration_beats": 1.0, "pitch_midi": 64, "lyric": "luma"},
        {"voice": "1", "staff": "1", "measure_number": 1, "offset_beats": 2.0,
         "duration_beats": 1.0, "pitch_midi": 64, "lyric": "nora"},
        {"voice": "2", "staff": "1", "measure_number": 1, "offset_beats": 1.0,
         "duration_beats": 1.0, "pitch_midi": 60, "lyric": "sela"},
    ]
    target = [
        {"voice": "2", "staff": "1", "measure_number": 1, "offset_beats": 0.0,
         "duration_beats": 1.0, "pitch_midi": 60},
        {"voice": "2", "staff": "1", "measure_number": 1, "offset_beats": 1.0,
         "duration_beats": 1.0, "pitch_midi": 60, "lyric": "sela"},
        {"voice": "2", "staff": "1", "measure_number": 1, "offset_beats": 2.0,
         "duration_beats": 1.0, "pitch_midi": 60},
    ]
    own, diagnostics = _propagate_lyrics(
        target_notes=target, source_notes=source, strategy="staff_line", policy="replace_all",
        verse_number=None, copy_all_verses=False, measure_range=(1, 1),
        line_voice="2", own_voice_exception=True,
    )
    shared, _ = _propagate_lyrics(
        target_notes=target, source_notes=source, strategy="staff_line", policy="replace_all",
        verse_number=None, copy_all_verses=False, measure_range=(1, 1),
    )
    assert [note.get("lyric") for note in own] == ["luma", "sela", "+"]
    assert [note.get("lyric") for note in shared] == ["luma", "sela", "nora"]
    assert diagnostics["source_lyric_candidates_count"] == 1
    assert diagnostics["dropped_source_lyrics_count"] == 0


def test_source_line_validation_and_legacy_section(tmp_path):
    score = _score(tmp_path, [{"1": [_n(4, "luma")], "2": [_n(4)]}])
    _, plan, _ = _build(score)
    normalized = parse_voice_part_plan(plan, score=score)
    assert normalized["ok"]
    assert any("lyric_source_line" in item for target in normalized["plan"]["targets"]
               for item in target["sections"])
    legacy = deepcopy(plan)
    for target in legacy["targets"]:
        for item in target["sections"]:
            item.pop("lyric_source_line", None)
    assert parse_voice_part_plan(legacy, score=score)["ok"]
    for bad in ({"staff": "1", "voice_part_id": None},
                {"staff": None, "voice_part_id": None, "extra": 1},
                {"staff": None, "voice_part_id": ""}, None):
        malformed = deepcopy(plan)
        first = next(item for target in malformed["targets"] for item in target["sections"]
                     if item["mode"] == "derive")
        first["lyric_source_line"] = bad
        assert parse_voice_part_plan(malformed, score=score)["error"]["code"] == "malformed_section_source"
    malformed = deepcopy(plan)
    first = next(item for target in malformed["targets"] for item in target["sections"]
                 if item["mode"] == "derive")
    first["lyric_strategy"] = "strict_onset"
    assert parse_voice_part_plan(malformed, score=score)["error"]["code"] == "malformed_section_source"
    unresolved = deepcopy(plan)
    first = next(item for target in unresolved["targets"] for item in target["sections"]
                 if item["mode"] == "derive")
    first["lyric_source_line"] = {"staff": None, "voice_part_id": "absent voice"}
    assert parse_voice_part_plan(unresolved, score=score)["ok"]
    assert preprocess_voice_parts(deepcopy(score), request={"plan": unresolved})["action"] == (
        "invalid_section_source")


def test_staff_line_defaults_preserve_existing_direct_call():
    source = [{"voice": "1", "staff": "1", "measure_number": 1,
               "offset_beats": 0.0, "duration_beats": 1.0, "pitch_midi": 60, "lyric": "luma"}]
    target = [dict(source[0]), {"voice": "2", "staff": "1", "measure_number": 1,
                                "offset_beats": 1.0, "duration_beats": 1.0, "pitch_midi": 64}]
    kwargs = dict(target_notes=target, source_notes=source, strategy="staff_line",
                  policy="replace_all", verse_number=None, copy_all_verses=False,
                  measure_range=(1, 1))
    assert _propagate_lyrics(**kwargs) == _propagate_lyrics(
        **kwargs, line_voice=None, own_voice_exception=False)
