"""Score signatures change exactly when a part's music or the score's timing changes."""

from __future__ import annotations

from pathlib import Path

from src.api.score import parse_score
from src.api.solfege import add_solfege_lyric_verse

SCORE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
    <score-part id="P2"><part-name>Piano</part-name>
      <score-instrument id="P2-I1"><instrument-name>Piano</instrument-name></score-instrument>
      <midi-instrument id="P2-I1"><midi-channel>2</midi-channel><midi-program>1</midi-program></midi-instrument>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
      <direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>100</per-minute></metronome></direction-type><sound tempo="100"/></direction>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration><type>half</type><lyric number="1"><syllabic>single</syllabic><text>sing</text></lyric></note>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration><type>half</type><lyric number="1"><syllabic>single</syllabic><text>now</text></lyric></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
      <note><pitch><step>E</step><octave>3</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""


def _summary(tmp_path: Path, xml: str, name: str = "score.xml", **kwargs):
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")
    return parse_score(path, **kwargs)["score_summary"], path


def _by_part(summary, key):
    return {part["part_name"]: part[key] for part in summary["parts"]}


def test_every_part_has_a_part_and_take_signature(tmp_path: Path) -> None:
    summary, _ = _summary(tmp_path, SCORE_XML)
    assert summary["timing_signature"]
    for part in summary["parts"]:
        assert part["part_signature"] and part["take_signature"]


def test_a_note_edit_changes_only_that_parts_signatures(tmp_path: Path) -> None:
    before, _ = _summary(tmp_path, SCORE_XML)
    after, _ = _summary(
        tmp_path, SCORE_XML.replace("<step>E</step><octave>3</octave>", "<step>F</step><octave>3</octave>"), "edited.xml"
    )
    assert _by_part(after, "part_signature")["Voice"] == _by_part(before, "part_signature")["Voice"]
    assert _by_part(after, "part_signature")["Piano"] != _by_part(before, "part_signature")["Piano"]
    assert _by_part(after, "take_signature")["Voice"] == _by_part(before, "take_signature")["Voice"]
    assert after["timing_signature"] == before["timing_signature"]


def test_an_authored_lyric_edit_changes_the_part_signature(tmp_path: Path) -> None:
    before, _ = _summary(tmp_path, SCORE_XML)
    after, _ = _summary(tmp_path, SCORE_XML.replace("<text>now</text>", "<text>Tom</text>"), "edited.xml")
    assert _by_part(after, "part_signature")["Voice"] != _by_part(before, "part_signature")["Voice"]
    assert _by_part(after, "part_signature")["Piano"] == _by_part(before, "part_signature")["Piano"]


def test_a_tempo_change_changes_every_take_but_no_part(tmp_path: Path) -> None:
    before, _ = _summary(tmp_path, SCORE_XML)
    after, _ = _summary(
        tmp_path,
        SCORE_XML.replace("<per-minute>100</per-minute>", "<per-minute>60</per-minute>").replace(
            'tempo="100"', 'tempo="60"'
        ),
        "slower.xml",
    )
    assert after["timing_signature"] != before["timing_signature"]
    assert _by_part(after, "part_signature") == _by_part(before, "part_signature")
    for part_id, signature in _by_part(after, "take_signature").items():
        assert signature != _by_part(before, "take_signature")[part_id]


def test_solfege_lines_and_lyric_selection_leave_signatures_unchanged(tmp_path: Path) -> None:
    before, path = _summary(tmp_path, SCORE_XML)
    derived = add_solfege_lyric_verse(path, tmp_path / "solfege.xml", part_ids=["Voice"])
    with_solfege = derived["score_summary"]
    assert _by_part(with_solfege, "take_signature") == _by_part(before, "take_signature")

    solfege_line = next(
        selection
        for part in with_solfege["parts"]
        for selection in part.get("lyric_selections", [])
        if selection["number"] != "1"
    )
    selected = parse_score(tmp_path / "solfege.xml", lyric_selection=solfege_line)["score_summary"]
    assert _by_part(selected, "take_signature") == _by_part(before, "take_signature")
