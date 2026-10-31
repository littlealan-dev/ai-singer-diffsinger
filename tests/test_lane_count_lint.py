from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.api import parse_score
from src.api.voice_part_lint_rules import render_lint_rules_for_prompt
from src.api.voice_parts import (
    _analyze_part_voice_parts,
    _run_preflight_plan_lint,
    parse_voice_part_plan,
)


def _note(voice: str | None, pitches: tuple[str, ...]) -> str:
    voice_tag = f"<voice>{voice}</voice>" if voice is not None else ""
    return "".join(
        "<note>"
        + ("<chord/>" if rank else "")
        + f"<pitch><step>{pitch[0]}</step><octave>{pitch[1]}</octave></pitch>"
        + f"<duration>1</duration>{voice_tag}<type>quarter</type><staff>1</staff>"
        + ("<lyric><text>la</text></lyric>" if rank == 0 else "")
        + "</note>"
        for rank, pitch in enumerate(pitches)
    )


def _score(measures: tuple[tuple[tuple[str | None, tuple[str, ...]], ...], ...]) -> dict:
    body = []
    voices = {voice for events in measures for voice, _ in events if voice is not None}
    for number, events in enumerate(measures, start=1):
        notes_by_voice = [_note(voice, pitches) for voice, pitches in events]
        present_voices = {voice for voice, _ in events}
        if None not in present_voices:
            notes_by_voice.extend(
                f"<note><rest/><duration>1</duration><voice>{voice}</voice>"
                "<type>quarter</type><staff>1</staff></note>"
                for voice in sorted(voices - present_voices)
            )
        notes = "<backup><duration>1</duration></backup>".join(notes_by_voice)
        attributes = (
            "<attributes><divisions>1</divisions><time><beats>4</beats>"
            "<beat-type>4</beat-type></time><clef><sign>G</sign>"
            "<line>2</line></clef></attributes>"
            if number == 1
            else ""
        )
        body.append(f'<measure number="{number}">{attributes}{notes}</measure>')
    xml = (
        '<score-partwise version="3.1"><part-list><score-part id="P1">'
        '<part-name>Choir</part-name></score-part></part-list>'
        f'<part id="P1">{"".join(body)}</part></score-partwise>'
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "lanes.xml"
        path.write_text(xml, encoding="utf-8")
        score = parse_score(path, verse_number=1)
    # music21 assigns an inherited voice to a note without <voice> when
    # adjacent measures contain explicit voices. Keep the lint input's
    # unnumbered note unassigned, as in the mixed-notation replay case.
    default_measures = {
        number
        for number, events in enumerate(measures, start=1)
        if any(voice is None for voice, _ in events)
    }
    for note in score["parts"][0]["notes"]:
        if note["measure_number"] in default_measures and not note["is_rest"]:
            note["voice"] = None
    return score


def _lint(score: dict, lanes: tuple[str, ...], *, chord_source: str | None = None) -> dict:
    voice_ids = {
        voice_part["source_voice_id"]: voice_part["voice_part_id"]
        for voice_part in _analyze_part_voice_parts(score["parts"][0], 0)["voice_parts"]
    }
    rank_by_source: dict[str, int] = {}
    targets = []
    end_measure = len({note["measure_number"] for note in score["parts"][0]["notes"]})
    for index, voice in enumerate(lanes):
        melody_voice = chord_source or voice
        rank = rank_by_source.get(melody_voice, 0)
        rank_by_source[melody_voice] = rank + 1
        target = {"part_index": 0, "voice_part_id": voice_ids[voice]}
        melody_source = {"part_index": 0, "voice_part_id": voice_ids[melody_voice]}
        targets.append(
            {
                "source": target,
                "output": {
                    "mode": "append_new_derived_lane",
                    "derived_lane_id": f"lane-{index}",
                },
                "split_coverage": "complete",
                "sections": [
                    {
                        "start_measure": 1,
                        "end_measure": end_measure,
                        "mode": "derive",
                        "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                        "method": "ranked",
                        "rank_index": rank,
                        "melody_source": melody_source,
                        "lyric_source": melody_source,
                    }
                ],
            }
        )
    parsed = parse_voice_part_plan({"targets": targets}, score=score)
    assert parsed["ok"], parsed
    return _run_preflight_plan_lint(score, parsed["plan"])


TURN_TAKING = (
    (("1", ("C5",)),),
    (("2", ("G4",)),),
    ((None, ("E4",)),),
)
CHORD = ((("1", ("C5", "E5", "G5")),),)
SOPRANO_ALTO = ((("1", ("C5",)), ("2", ("G4",))),)
TWO_VOICES_CHORD = (
    (("1", ("C5", "E5", "G5")),),
    (("2", ("G4",)),),
)
THREE_VOICES = (
    (("1", ("C5",)), ("2", ("G4",))),
    (("3", ("E4",)),),
)


@pytest.mark.parametrize(
    ("measures", "lanes", "density", "real_voices", "expected_rules", "chord_source"),
    [
        pytest.param(TURN_TAKING, ("1", "2"), 1, 2, set(), None, id="turn-taking-two-lanes"),
        pytest.param(TURN_TAKING, ("1",), 1, 2, {"same_part_target_completeness", "complete_split_scope_lane_count_mismatch"}, None, id="turn-taking-one-lane"),
        pytest.param(TURN_TAKING, ("1", "2", "1"), 1, 2, {"complete_split_scope_lane_count_mismatch"}, None, id="turn-taking-three-lanes"),
        pytest.param(CHORD, ("1", "1", "1"), 3, 1, set(), None, id="chord-three-lanes"),
        pytest.param(CHORD, ("1", "1", "1", "1"), 3, 1, {"complete_split_scope_lane_count_mismatch"}, None, id="chord-four-lanes"),
        pytest.param(SOPRANO_ALTO, ("1", "2"), 2, 2, set(), None, id="soprano-alto-two-lanes"),
        pytest.param(SOPRANO_ALTO, ("1", "2", "1"), 2, 2, {"complete_split_scope_lane_count_mismatch"}, None, id="soprano-alto-three-lanes"),
        pytest.param(TWO_VOICES_CHORD, ("1", "1", "2"), 3, 2, set(), "1", id="two-voices-chord-three-lanes"),
        pytest.param(TWO_VOICES_CHORD, ("1", "2"), 3, 2, {"complete_split_scope_lane_count_mismatch"}, "1", id="two-voices-chord-two-lanes"),
        pytest.param(THREE_VOICES, ("1", "2", "3"), 2, 3, set(), None, id="three-voices-three-lanes"),
    ],
)
def test_lane_count_for_staff(
    measures: tuple,
    lanes: tuple[str, ...],
    density: int,
    real_voices: int,
    expected_rules: set[str],
    chord_source: str | None,
) -> None:
    result = _lint(_score(measures), lanes, chord_source=chord_source)
    rules = {finding["rule"] for finding in result["findings"]}
    for rule in ("complete_split_scope_lane_count_mismatch", "same_part_target_completeness"):
        assert (rule in rules) == (rule in expected_rules), result["findings"]
    if not expected_rules:
        assert result["ok"], result["findings"]
    mismatch = next(
        (finding for finding in result["findings"] if finding["rule"] == "complete_split_scope_lane_count_mismatch"),
        None,
    )
    if mismatch:
        attributes = mismatch["failing_attributes"]
        assert attributes["maximum_simultaneous_note_count"] == density
        assert attributes["real_voice_part_count"] == real_voices
        assert attributes["expected_lane_count"] == max(density, real_voices)
        assert attributes["visible_derived_lane_count"] == len(lanes)


def test_prompt_describes_expected_lane_count() -> None:
    rendered = render_lint_rules_for_prompt()
    assert "complete_split_scope_lane_count_mismatch" in rendered
    assert "max(maximum simultaneous note count, real non-_default voice parts" in rendered
