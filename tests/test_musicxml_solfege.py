from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from src.api.score import parse_score
from src.musicxml.solfege import (
    GENERATED_LYRIC_NAME,
    add_solfege_lyric_verse,
    modify_generated_solfege_verses,
)


def _score_xml(*, fifths: int = 1) -> str:
    notes = "".join(
        f"""
        <note>
          <pitch><step>{step}</step><octave>4</octave></pitch>
          <duration>1</duration><voice>1</voice><type>quarter</type>
          <lyric number="1"><text>{word}</text></lyric>
        </note>
        """
        for step, word in zip(("G", "A", "B", "C", "D"), ("one", "two", "three", "four", "five"))
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Soprano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>{fifths}</fifths><mode>major</mode></key><time><beats>5</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
    {notes}
  </measure></part>
</score-partwise>"""


def _lyrics(path: Path, *, name: str | None = None) -> list[str]:
    root = ElementTree.parse(path).getroot()
    values: list[str] = []
    for lyric in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "lyric"):
        if name is not None and lyric.attrib.get("name") != name:
            continue
        text = next(
            (child for child in lyric if child.tag.rsplit("}", 1)[-1] == "text"),
            None,
        )
        if text is not None and text.text:
            values.append(text.text)
    return values


def test_add_movable_major_uses_key_tonic_and_preserves_existing_lyrics(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    output = tmp_path / "output.xml"
    source.write_text(_score_xml(fifths=1), encoding="utf-8")

    result = add_solfege_lyric_verse(source, output, part_id="Soprano")

    assert result["status"] == "ready"
    assert result["new_verse_number"] == "2"
    assert _lyrics(output, name=GENERATED_LYRIC_NAME) == ["do", "re", "mi", "fa", "so"]
    assert _lyrics(output)[:5] == ["one", "do", "two", "re", "three"]
    parsed = parse_score(output, verse_number="2")
    solfege_notes = [
        note
        for note in parsed["parts"][0]["notes"]
        if note.get("lyric_name") == GENERATED_LYRIC_NAME
    ]
    assert [note.get("lyric") for note in solfege_notes[:5]] == ["do", "re", "mi", "fa", "so"]
    soprano_summary = parsed["score_summary"]["parts"][0]
    generated_verse = next(
        verse
        for verse in soprano_summary["lyric_verses"]
        if verse["verse_number"] == "2"
    )
    assert generated_verse["lyric_names"] == [GENERATED_LYRIC_NAME]
    assert generated_verse["is_generated_solfege"] is True


def test_unknown_part_returns_action_required_instead_of_raising(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    output = tmp_path / "output.xml"
    source.write_text(_score_xml(), encoding="utf-8")

    result = add_solfege_lyric_verse(source, output, part_id="Contralto")

    assert result["status"] == "action_required"
    assert result["code"] == "target_not_found"
    assert not output.exists()


def test_minor_modes_map_relative_minor_differently(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    la_output = tmp_path / "la.xml"
    do_output = tmp_path / "do.xml"
    source.write_text(_score_xml(fifths=0), encoding="utf-8")

    add_solfege_lyric_verse(
        source,
        la_output,
        part_index=0,
        settings={"system": "movable_do", "mode": "minor_la_based"},
    )
    add_solfege_lyric_verse(
        source,
        do_output,
        part_index=0,
        settings={"system": "movable_do", "mode": "minor_do_based"},
    )

    assert _lyrics(la_output, name=GENERATED_LYRIC_NAME) == ["so", "la", "ti", "do", "re"]
    assert _lyrics(do_output, name=GENERATED_LYRIC_NAME) == ["te", "do", "re", "me", "fa"]


def test_modify_rewrites_generated_verse_to_fixed_do_only(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    generated = tmp_path / "generated.xml"
    output = tmp_path / "fixed.xml"
    source.write_text(_score_xml(fifths=1), encoding="utf-8")
    add_solfege_lyric_verse(source, generated, part_index=0)

    result = modify_generated_solfege_verses(
        generated,
        output,
        settings={"system": "fixed_do", "mode": "major"},
    )

    assert result["updated_generated_verses"] == [
        {"part_id": "P1", "part_index": 0, "verse_number": "2", "notes_updated": 5}
    ]
    assert _lyrics(output, name=GENERATED_LYRIC_NAME) == ["so", "la", "ti", "do", "re"]
    assert [value for value in _lyrics(output) if value in {"one", "two", "three", "four", "five"}] == [
        "one", "two", "three", "four", "five"
    ]


def test_existing_generated_verse_is_not_duplicated(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    generated = tmp_path / "generated.xml"
    duplicate = tmp_path / "duplicate.xml"
    source.write_text(_score_xml(), encoding="utf-8")
    add_solfege_lyric_verse(source, generated, part_index=0)

    result = add_solfege_lyric_verse(generated, duplicate, part_index=0)

    assert result["status"] == "action_required"
    assert result["code"] == "solfege_verse_already_exists"
    assert not duplicate.exists()


def test_chord_target_requires_preparation(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    output = tmp_path / "output.xml"
    xml = _score_xml().replace(
        "<pitch><step>A</step>",
        "<chord/><pitch><step>A</step>",
        1,
    )
    source.write_text(xml, encoding="utf-8")

    result = add_solfege_lyric_verse(source, output, part_index=0)

    assert result["status"] == "action_required"
    assert result["code"] == "complex_target_requires_preparation"
    assert not output.exists()
