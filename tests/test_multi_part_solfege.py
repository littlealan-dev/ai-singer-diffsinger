"""Adding generated solfege lines to several parts in one call."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from src.api.solfege import add_solfege_lyric_verse
from src.musicxml.solfege import GENERATED_LYRIC_NAME, add_solfege_lyric_verses

AMAZING_GRACE = Path(__file__).resolve().parents[1] / "ui" / "public" / "demo-scores" / "amazing-grace.xml"


def _parts_with_a_line(path: Path) -> set[str]:
    root = ElementTree.parse(path).getroot()
    return {
        part.get("id")
        for part in root.findall("part")
        if any(lyric.get("name") == GENERATED_LYRIC_NAME for lyric in part.iter("lyric"))
    }


def test_every_listed_part_gets_its_line_in_one_pass(tmp_path: Path) -> None:
    output = tmp_path / "solfege.xml"
    result = add_solfege_lyric_verse(
        AMAZING_GRACE, output, part_ids=["Sopran", "Alt", "Tenor", "Bass"]
    )

    assert result["status"] == "ready"
    assert [target["part_id"] for target in result["completed_targets"]] == ["Sopran", "Alt", "Tenor", "Bass"]
    assert result["already_present"] == [] and result["skipped"] == []
    assert _parts_with_a_line(output) == {"P1", "P2", "P3", "P4"}
    # Each part's line has its own selection, as the quote needs it.
    selections = [target["lyric_selection"] for target in result["completed_targets"]]
    assert len({selection["id"] for selection in selections}) == 4
    assert {selection["name"] for selection in selections} == {GENERATED_LYRIC_NAME}
    # The score opens on the first new line.
    assert result["derived_score"]["selected_lyric_selection"] == selections[0]


def test_parts_that_already_have_a_line_or_cannot_take_one_are_reported(tmp_path: Path) -> None:
    first = tmp_path / "first.xml"
    add_solfege_lyric_verse(AMAZING_GRACE, first, part_ids=["Sopran"])
    second = tmp_path / "second.xml"

    result = add_solfege_lyric_verse(
        first, second, part_ids=["Sopran", "Alt", "P5-Staff1", "P5-Staff2", "Nobody"]
    )

    assert result["status"] == "ready"
    assert [target["part_id"] for target in result["completed_targets"]] == ["Alt"]
    assert [target["part_id"] for target in result["already_present"]] == ["Sopran"]
    assert result["already_present"][0]["lyric_selection"]["name"] == GENERATED_LYRIC_NAME
    # The piano's two staves are one MusicXML part: checked once, reported for each staff.
    assert [(entry["part_id"], entry["code"]) for entry in result["skipped"]] == [
        ("Nobody", "target_not_found"),
        ("P5-Staff1", "complex_target_requires_preparation"),
        ("P5-Staff2", "complex_target_requires_preparation"),
    ]
    assert _parts_with_a_line(second) == {"P1", "P2"}


def test_nothing_is_written_when_no_line_is_added(tmp_path: Path) -> None:
    first = tmp_path / "first.xml"
    add_solfege_lyric_verse(AMAZING_GRACE, first, part_ids=["Sopran"])
    output = tmp_path / "unchanged.xml"

    only_present = add_solfege_lyric_verse(first, output, part_ids=["Sopran"])
    assert only_present["status"] == "ready"
    assert only_present["completed_targets"] == []
    assert [target["part_id"] for target in only_present["already_present"]] == ["Sopran"]
    assert "derived_musicxml_path" not in only_present
    assert not output.exists()

    # A single part that needs preparing is refused exactly as before.
    refused = add_solfege_lyric_verse(AMAZING_GRACE, output, part_ids=["P5-Staff1"])
    assert refused["status"] == "action_required"
    assert refused["code"] == "complex_target_requires_preparation"
    assert [entry["part_id"] for entry in refused["skipped"]] == ["P5-Staff1"]
    assert not output.exists()


def test_a_part_without_pitched_notes_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "rests.xml"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
    <score-part id="P2"><part-name>Silent</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice><type>quarter</type></note>
  </measure></part>
  <part id="P2"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time></attributes>
    <note><rest/><duration>1</duration><voice>1</voice><type>quarter</type></note>
  </measure></part>
</score-partwise>""",
        encoding="utf-8",
    )
    result = add_solfege_lyric_verses(source, tmp_path / "out.xml", raw_part_ids=["P1", "P2"])
    assert [entry["part_id"] for entry in result["completed"]] == ["P1"]
    assert [(entry["part_id"], entry["code"]) for entry in result["skipped"]] == [("P2", "no_pitched_notes")]
