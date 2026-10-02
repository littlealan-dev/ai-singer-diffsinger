"""The built-in demo songs are identified by a marker inside their MusicXML."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from src.api.score import parse_score
from src.api.solfege import add_solfege_lyric_verse
from src.musicxml.demo_songs import read_demo_song_id

DEMO_SCORES = Path(__file__).resolve().parents[1] / "ui" / "public" / "demo-scores"

UNMARKED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <identification>{misc}</identification>
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type><lyric><text>la</text></lyric></note>
    </measure>
  </part>
</score-partwise>
"""


@pytest.mark.parametrize("song_id", ["amazing-grace", "happy-birthday"])
def test_shipped_demo_scores_carry_their_marker(song_id: str) -> None:
    path = DEMO_SCORES / f"{song_id}.xml"
    assert read_demo_song_id(path) == song_id
    assert parse_score(path)["score_summary"]["demo_song"] == song_id


@pytest.mark.parametrize(
    "misc",
    [
        "",
        '<miscellaneous><miscellaneous-field name="sightsinger-demo">another-song</miscellaneous-field></miscellaneous>',
        '<miscellaneous><miscellaneous-field name="other-field">happy-birthday</miscellaneous-field></miscellaneous>',
    ],
)
def test_scores_without_a_known_demo_marker_are_not_demos(tmp_path: Path, misc: str) -> None:
    path = tmp_path / "score.xml"
    path.write_text(UNMARKED_XML.format(misc=misc), encoding="utf-8")
    assert read_demo_song_id(path) is None
    assert "demo_song" not in parse_score(path)["score_summary"]


def test_a_derived_demo_score_keeps_its_marker(tmp_path: Path) -> None:
    result = add_solfege_lyric_verse(
        DEMO_SCORES / "happy-birthday.xml",
        tmp_path / "solfege.xml",
        part_id="Alto",
    )
    assert result["score_summary"]["demo_song"] == "happy-birthday"


def test_happy_birthday_voices_use_twelve_divisions_so_triplet_eighths_fit() -> None:
    root = ElementTree.parse(DEMO_SCORES / "happy-birthday.xml").getroot()
    names = {
        part.get("id"): part.findtext("part-name")
        for part in root.find("part-list").findall("score-part")
    }
    assert names == {"P2": "Alto", "P3": "Men", "P4": "Piano"}
    for part_id in ("P2", "P3"):
        part = root.find(f"part[@id='{part_id}']")
        assert [d.text for d in part.iter("divisions")] == ["12"]
