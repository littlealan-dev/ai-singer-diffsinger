"""Separate singer layouts on synthetic, one-staff scores."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from src.api import preprocess_chooser as chooser
from src.api.preprocess_assembler import assemble_plan, first_option_choices
from src.api.preprocess_sheet import build_sheet
from src.api.score import parse_score
from src.api.voice_parts import preprocess_voice_parts
from tests.test_preprocess_chooser import _chord_beside_a_line
from tests.test_preprocess_sheet import _single_staff_score


def _note(step, octave, voice, *, chord=False, lyric=None):
    return (
        '<note>' + ('<chord/>' if chord else '')
        + f'<pitch><step>{step}</step><octave>{octave}</octave></pitch>'
        + '<duration>4</duration>'
        + (f'<voice>{voice}</voice>' if voice is not None else '')
        + '<type>whole</type>'
        + (f'<lyric><text>{lyric}</text></lyric>' if lyric else '') + '</note>'
    )


def _hidden_rest(voice):
    return f'<note print-object="no"><rest/><duration>4</duration><voice>{voice}</voice><type>whole</type></note>'


def _score(tmp_path):
    return _single_staff_score(tmp_path, [
        _note('C', 5, '1', lyric='first') + '<backup><duration>4</duration></backup>'
        + _note('G', 4, '2'),
        _note('E', 5, '1', lyric='second') + _note('G', 5, '1', chord=True)
        + '<backup><duration>4</duration></backup>' + _hidden_rest('2'),
        _note('F', 5, '1', lyric='third') + '<backup><duration>4</duration></backup>'
        + _note('A', 4, '2'),
    ])


def _context(score):
    return {'target': {'part_id': score['parts'][0]['part_id']}}


def test_lane_layout_slot_evidence_and_separate_skeleton(tmp_path):
    score = _score(tmp_path)
    sheet = build_sheet(score, _context(score))
    slot, = [slot for slot in sheet.slots if slot.kind == 'lane_layout']
    assert slot.slot_id == f"{score['parts'][0]['part_id']}|1|1-3|lane_layout"
    assert [option.option_id for option in slot.options] == ['shared', 'separate']
    assert len(sheet.lanes) == 2
    assert slot.lane_ids == tuple(lane.lane_id for lane in sheet.lanes)
    assert slot.evidence['staff_peak'] == 2
    assert slot.evidence['lanes_if_shared'] == 2
    assert slot.evidence['lanes_if_separate'] == 3
    assert sorted(voice['notes'] for voice in slot.evidence['voices'].values()) == [2, 4]
    assert sorted(voice['bars'] for voice in slot.evidence['voices'].values()) == [2, 3]
    assert sorted(voice['own_peak'] for voice in slot.evidence['voices'].values()) == [1, 2]

    separate = build_sheet(score, _context(score), pinned={slot.slot_id: 'separate'})
    assert len(separate.lanes) == 3
    assert [lane.source_voice_part_id for lane in separate.lanes].count('voice part 1') == 2
    assert [lane.source_voice_part_id for lane in separate.lanes].count('voice part 2') == 1
    assert separate.fixed[slot.slot_id].option_id == 'separate'


def test_separate_layout_assigns_each_voice_only_to_its_own_lanes(tmp_path):
    score = _score(tmp_path)
    context = _context(score)
    shared = build_sheet(score, context)
    layout, = [slot for slot in shared.slots if slot.kind == 'lane_layout']
    separate = build_sheet(score, context, pinned={layout.slot_id: 'separate'})
    assert not any(slot.kind == 'freeform' for slot in separate.slots)
    choices = first_option_choices(separate)
    plan = assemble_plan(separate, choices, score=score)
    assert len(plan['targets']) == 3
    assert all(target['split_coverage'] == 'complete' for target in plan['targets'])
    result = preprocess_voice_parts(deepcopy(score), request={'plan': plan})
    assert result['status'] == 'ready', result.get('section_quality_issues') or result.get('targets')
    parts = result['score']['parts']
    actual = []
    for target in result['targets']:
        index = target['appended_part_ref']['part_index']
        actual.append([(note['measure_number'], note['pitch_midi'])
                       for note in parts[index]['notes'] if not note.get('is_rest')])
    assert actual == [
        [(1, 72), (2, 79), (3, 77)],
        [(2, 76)],
        [(1, 67), (3, 69)],
    ]
    reparsed = parse_score(result['modified_musicxml_path'], verse_number=1)
    assert len(reparsed['parts']) == len(score['parts']) + 3
    assert [sum(not note.get('is_rest') for note in part['notes'])
            for part in reparsed['parts'][-3:]] == [3, 1, 2]


def _turn_taking_chords(tmp_path, *, voices, chord_size):
    notes = []
    pitches = [('C', 5), ('E', 5), ('G', 5)]
    for active in range(1, voices + 1):
        bars = []
        for voice in range(1, voices + 1):
            if bars:
                bars.append('<backup><duration>4</duration></backup>')
            if voice == active:
                bars.extend(_note(step, octave, str(voice), chord=rank > 0,
                                  lyric='word' if rank == 0 else None)
                            for rank, (step, octave) in enumerate(pitches[:chord_size]))
            else:
                bars.append(_hidden_rest(str(voice)))
        notes.append(''.join(bars))
    return _single_staff_score(tmp_path, notes)


def test_taking_turns_and_single_chord_voice_need_no_layout_question(tmp_path):
    turns = _turn_taking_chords(tmp_path, voices=2, chord_size=1)
    turn_sheet = build_sheet(turns, _context(turns))
    assert len(turn_sheet.lanes) == 2
    assert not any(slot.kind == 'lane_layout' for slot in turn_sheet.slots)

    single = _turn_taking_chords(tmp_path, voices=1, chord_size=2)
    single_sheet = build_sheet(single, _context(single))
    assert len(single_sheet.lanes) == 2
    assert not any(slot.kind == 'lane_layout' for slot in single_sheet.slots)


def test_two_and_three_chord_voices_build_without_freeform(tmp_path):
    for voices, size, lane_count in [(2, 2, 4), (3, 3, 9)]:
        case = tmp_path / f'voices-{voices}'
        case.mkdir()
        score = _turn_taking_chords(case, voices=voices, chord_size=size)
        shared = build_sheet(score, _context(score))
        layout, = [slot for slot in shared.slots if slot.kind == 'lane_layout']
        separate = build_sheet(score, _context(score), pinned={layout.slot_id: 'separate'})
        assert len(separate.lanes) == lane_count
        assert not any(slot.kind == 'freeform' for slot in separate.slots)
        plan = assemble_plan(separate, first_option_choices(separate), score=score)
        assert len(plan['targets']) == lane_count
        result = preprocess_voice_parts(deepcopy(score), request={'plan': plan})
        assert result['status'] == 'ready', [finding.get('rule') for finding in result.get('lint_findings', [])]
        parts = result['score']['parts']
        counts = [sum(not note.get('is_rest') for note in
                      parts[target['appended_part_ref']['part_index']]['notes'])
                  for target in result['targets']]
        assert counts == [1] * lane_count


def test_default_chord_uses_highest_separate_lanes(tmp_path):
    score = _single_staff_score(tmp_path, [
        _note('C', 5, None, lyric='first') + _note('E', 5, None, chord=True),
        _note('E', 5, '1', lyric='second') + _note('G', 5, '1', chord=True)
        + '<backup><duration>4</duration></backup>' + _hidden_rest('2'),
        _hidden_rest('1') + '<backup><duration>4</duration></backup>'
        + _note('G', 4, '2', lyric='third'),
    ])
    shared = build_sheet(score, _context(score))
    layout, = [slot for slot in shared.slots if slot.kind == 'lane_layout']
    separate = build_sheet(score, _context(score), pinned={layout.slot_id: 'separate'})
    assert len(separate.lanes) == 3
    plan = assemble_plan(separate, first_option_choices(separate), score=score)
    result = preprocess_voice_parts(deepcopy(score), request={'plan': plan})
    assert result['status'] == 'ready', [finding.get('rule') for finding in result.get('lint_findings', [])]
    actual = []
    for target in result['targets']:
        index = target['appended_part_ref']['part_index']
        actual.append([(note['measure_number'], note['pitch_midi'])
                       for note in result['score']['parts'][index]['notes'] if not note.get('is_rest')])
    assert actual == [[(1, 76), (2, 79)], [(1, 72), (2, 76)], [(3, 67)]]


def _answer(request, *, separate=False):
    return json.dumps({
        key: ('separate' if separate and request.key_to_slot[key].endswith('|lane_layout')
              else prop['enum'][0])
        for key, prop in request.response_schema['properties'].items()
    })


def test_separate_answer_rebuilds_in_second_pass(tmp_path):
    score = _score(tmp_path)
    requests = []

    async def call(request):
        requests.append(request)
        return _answer(request, separate=True)

    sheet, choices, stats = asyncio.run(chooser.choose(score, _context(score), call=call))
    assert len(sheet.lanes) == 3
    assert stats.second_pass_slots == 0
    assert len(requests) == 1
    assert not any(slot_id.endswith('|lane_layout') for request in requests[1:]
                   for slot_id in request.key_to_slot.values())
    plan = assemble_plan(sheet, choices, score=score)
    assert preprocess_voice_parts(deepcopy(score), request={'plan': plan})['status'] == 'ready'


def test_separate_discards_shared_voice_assignment_from_same_reply(tmp_path):
    score = _score(tmp_path)
    requests = []

    async def call(request):
        requests.append(request)
        answer = json.loads(_answer(request, separate=True))
        for key, slot_id in request.key_to_slot.items():
            if slot_id.endswith('|voice_assignment'):
                answer[key] = request.response_schema['properties'][key]['enum'][-1]
        return json.dumps(answer)

    sheet, choices, _ = asyncio.run(chooser.choose(score, _context(score), call=call))
    assert any(slot_id.endswith('|voice_assignment') for slot_id in requests[0].key_to_slot.values())
    assert len(sheet.lanes) == 3
    plan = assemble_plan(sheet, choices, score=score)
    result = preprocess_voice_parts(deepcopy(score), request={'plan': plan})
    assert result['status'] == 'ready'
    assert [sum(not note.get('is_rest') for note in
                result['score']['parts'][target['appended_part_ref']['part_index']]['notes'])
            for target in result['targets']] == [3, 1, 2]


def test_separate_keeps_other_staff_structure_answer(tmp_path):
    upper_path = tmp_path / 'upper'
    lower_path = tmp_path / 'lower'
    upper_path.mkdir()
    lower_path.mkdir()
    score = _score(upper_path)
    lower = _score(lower_path)
    score['parts'][0]['part_id'] = 'P1-Combined'
    score['parts'][0]['notes'] = [
        {**note, 'staff': '1'} for note in score['parts'][0]['notes']
    ] + [
        {**note, 'staff': '2', 'voice': str(int(note['voice']) + 2)}
        for note in lower['parts'][0]['notes']
    ]
    context = _context(score)
    initial = build_sheet(score, context)
    other = [slot for slot in initial.slots
             if slot.kind == 'voice_assignment' and '|2|' in slot.slot_id]
    assert other, [(slot.kind, slot.slot_id) for slot in initial.slots]
    other_assignment, = other
    assert len(other_assignment.options) > 1

    async def call(request):
        answer = json.loads(_answer(request))
        for key, slot_id in request.key_to_slot.items():
            if slot_id.endswith('|lane_layout') and '|1|' in slot_id:
                answer[key] = 'separate'
            if slot_id == other_assignment.slot_id:
                answer[key] = other_assignment.options[-1].option_id
        return json.dumps(answer)

    sheet, _, _ = asyncio.run(chooser.choose(score, context, call=call))
    assert len([lane for lane in sheet.lanes if lane.scope_key == '1']) == 3
    assert len([lane for lane in sheet.lanes if lane.scope_key == '2']) == 2
    assert sheet.fixed[other_assignment.slot_id].option_id == other_assignment.options[-1].option_id


def test_separate_reasks_only_changed_slots_and_keeps_turn_budget(tmp_path):
    score = _chord_beside_a_line(tmp_path)
    requests = []

    async def call(request):
        requests.append(request)
        return _answer(request, separate=True)

    sheet, _, stats = asyncio.run(chooser.choose(score, _context(score), call=call))
    assert len(sheet.lanes) == 5
    assert len(requests) == 2
    assert stats.requests == 2 and stats.second_pass_slots == len(requests[1].key_to_slot)
    assert not any(slot_id.endswith('|lane_layout') for slot_id in requests[1].key_to_slot.values())


def test_repair_keeps_separate_layout_and_lane_ids(tmp_path):
    score = _score(tmp_path)

    async def call(request):
        return _answer(request, separate=True)

    sheet, choices, _ = asyncio.run(chooser.choose(score, _context(score), call=call))
    failing_lane = sheet.lanes[1].lane_id
    repair_requests = []

    async def repair_call(request):
        repair_requests.append(request)
        return _answer(request)

    repaired, repaired_choices, _ = asyncio.run(chooser.choose_repair(
        score, _context(score), sheet=sheet, choices=choices,
        failing={failing_lane: [(2, 2)]}, findings_by_lane={}, pinned=None,
        call=repair_call,
    ))
    assert len(repaired.lanes) == 3
    assert [lane.lane_id for lane in repaired.lanes] == [lane.lane_id for lane in sheet.lanes]
    assert not any(slot_id.endswith('|lane_layout') for request in repair_requests
                   for slot_id in request.key_to_slot.values())
    plan = assemble_plan(repaired, repaired_choices, score=score)
    assert preprocess_voice_parts(deepcopy(score), request={'plan': plan})['status'] == 'ready'
