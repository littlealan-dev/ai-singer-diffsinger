from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from src.api.score import parse_score
from src.musicxml.solfege import (
    GENERATED_LYRIC_NAME,
    GENERATED_LYRIC_NUMBER,
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
    assert result["new_verse_number"] == GENERATED_LYRIC_NUMBER
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
    # The summary lists the generated line under its own number, not under the
    # position music21 gives it on each note.
    assert [verse["verse_number"] for verse in soprano_summary["lyric_verses"]] == ["1", GENERATED_LYRIC_NUMBER]
    generated_verse = next(
        verse
        for verse in soprano_summary["lyric_verses"]
        if verse["verse_number"] == GENERATED_LYRIC_NUMBER
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
        {
            "part_id": "P1",
            "part_index": 0,
            "verse_number": GENERATED_LYRIC_NUMBER,
            "notes_updated": 5,
        }
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


def _generated_lyrics_by_note(path: Path, part_id: str) -> list[tuple[str, str | None]]:
    """(pitch, generated syllable) for every pitched note of one part, in order."""
    root = ElementTree.parse(path).getroot()
    part = root.find(f"part[@id='{part_id}']")
    rows = []
    for note in part.iter("note"):
        pitch = note.find("pitch")
        if pitch is None:
            continue
        generated = [
            lyric.findtext("text")
            for lyric in note.findall("lyric")
            if lyric.get("name") == "SightSinger Solfege"
        ]
        rows.append((pitch.findtext("step") + pitch.findtext("octave"), generated[0] if generated else None))
    return rows


def test_regenerating_a_solfege_line_follows_edited_notes(tmp_path: Path) -> None:
    from src.api.solfege import add_solfege_lyric_verse, regenerate_solfege_verses

    demo = Path(__file__).resolve().parents[1] / "ui" / "public" / "demo-scores" / "happy-birthday.xml"
    with_solfege = tmp_path / "solfege.xml"
    added = add_solfege_lyric_verse(demo, with_solfege, part_id="Alto")
    alto_line = next(
        selection
        for part in added["score_summary"]["parts"]
        if part["part_id"] == "Alto"
        for selection in part["lyric_selections"]
        if selection["name"] == "SightSinger Solfege"
    )
    men_before = _generated_lyrics_by_note(with_solfege, "P3")

    # Edit the Alto: re-pitch its first note, and drop one note's generated
    # syllable, as splitting a note leaves the new note without one.
    text = with_solfege.read_text(encoding="utf-8")
    part_start = text.index('<part id="P2">')
    first_step = text.index("<step>C</step>", part_start)
    text = text[:first_step] + "<step>D</step>" + text[first_step + len("<step>C</step>"):]
    first_generated = text.index('name="SightSinger Solfege"', part_start)
    generated_lyric = text.index('name="SightSinger Solfege"', first_generated + 1)
    lyric_start = text.rindex("<lyric", 0, generated_lyric)
    lyric_end = text.index("</lyric>", generated_lyric) + len("</lyric>")
    text = text[:lyric_start] + text[lyric_end:]
    edited = tmp_path / "edited.xml"
    edited.write_text(text, encoding="utf-8")
    stale = _generated_lyrics_by_note(edited, "P2")
    # F major, movable do: the re-pitched note still carries C's "so", and
    # the second note has lost its syllable.
    assert stale[0] == ("D4", "so")
    assert stale[1][1] is None

    regenerated = regenerate_solfege_verses(edited, tmp_path / "regenerated.xml", part_ids=["P2"])

    rows = _generated_lyrics_by_note(tmp_path / "regenerated.xml", "P2")
    assert all(syllable for _, syllable in rows)
    assert rows[0] == ("D4", "la")
    # The line keeps its verse number, so its lyric selection id is unchanged.
    alto = next(part for part in regenerated["score_summary"]["parts"] if part["part_id"] == "Alto")
    identity = lambda selection: (selection["id"], selection["number"], selection["name"])
    assert identity(alto_line) in [identity(selection) for selection in alto["lyric_selections"]]
    # Other parts are untouched.
    assert _generated_lyrics_by_note(tmp_path / "regenerated.xml", "P3") == men_before


def _tied_three_verse_xml() -> str:
    """Three verses on most notes, a melisma note, and a tie across the barline."""
    def verses(*words: str) -> str:
        return "".join(
            f'<lyric number="{index}"><syllabic>single</syllabic><text>{word}</text></lyric>'
            for index, word in enumerate(words, start=1)
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Sopran</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>0</fifths><mode>major</mode></key><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><voice>1</voice><type>quarter</type>{verses("A", "Twas", "Through")}</note>
      <note><pitch><step>D</step><octave>5</octave></pitch><duration>1</duration><voice>1</voice><type>quarter</type></note>
      <note><pitch><step>E</step><octave>5</octave></pitch><duration>2</duration><tie type="start"/><voice>1</voice><type>half</type><notations><tied type="start"/></notations>{verses("me", "fear", "come")}</note>
    </measure>
    <measure number="2">
      <note><pitch><step>E</step><octave>5</octave></pitch><duration>2</duration><tie type="stop"/><voice>1</voice><type>half</type><notations><tied type="stop"/></notations></note>
      <note><pitch><step>G</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice><type>half</type>{verses("grace")}</note>
    </measure>
  </part>
</score-partwise>"""


def _generated_lyric_elements(path: Path) -> list[list[ElementTree.Element]]:
    root = ElementTree.parse(path).getroot()
    return [
        [lyric for lyric in note.findall("lyric") if lyric.get("name") == GENERATED_LYRIC_NAME]
        for note in root.iter("note")
    ]


def test_a_tie_holds_its_solfege_syllable_and_leaves_the_music_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    source.write_text(_tied_three_verse_xml(), encoding="utf-8")
    output = tmp_path / "solfege.xml"
    assert add_solfege_lyric_verse(source, output, part_id="Sopran")["status"] == "ready"

    # The note that starts the tie carries the syllable and the extender line;
    # the note that continues it carries no solfege lyric at all.
    by_note = _generated_lyric_elements(output)
    tie_start, tie_stop = by_note[2], by_note[3]
    assert [lyric.findtext("text") for lyric in tie_start] == ["mi"]
    assert tie_start[0].find("extend") is not None
    assert tie_stop == []

    before = parse_score(source)["score_summary"]["parts"][0]
    after_summary = parse_score(output)["score_summary"]
    after = after_summary["parts"][0]
    # A generated line is not an edit to the music.
    assert after["part_signature"] == before["part_signature"]
    # Each lyric line is listed once: the three verses, then the solfege line,
    # however many verses share a note with it.
    assert [(verse["verse_number"], verse["is_generated_solfege"]) for verse in after["lyric_verses"]] == [
        ("1", False), ("2", False), ("3", False), (GENERATED_LYRIC_NUMBER, True),
    ]
    assert after["lyric_verses"][0]["sample"] == ["A", "me", "grace"]
    assert after["lyric_verses"][3]["sample"] == ["do", "re", "mi", "so"]

    # Sung on the solfege line, the tie holds "mi".
    selection = next(item for item in after["lyric_selections"] if item["is_generated_solfege"])
    sung = parse_score(output, part_id="Sopran", lyric_selection={
        key: selection[key] for key in ("id", "number", "name")
    })["parts"][0]["notes"]
    assert [note.get("lyric") for note in sung if not note.get("is_rest")] == ["do", "re", "mi", "+", "so"]


def test_regenerating_drops_the_text_less_lyric_an_earlier_version_left_on_a_tie(tmp_path: Path) -> None:
    from src.api.solfege import regenerate_solfege_verses

    source = tmp_path / "source.xml"
    source.write_text(_tied_three_verse_xml(), encoding="utf-8")
    added = tmp_path / "solfege.xml"
    add_solfege_lyric_verse(source, added, part_id="Sopran")
    # Earlier versions wrote an extend-only lyric on the note continuing a tie.
    text = added.read_text(encoding="utf-8")
    stop = text.index('<tie type="stop"')
    note_end = text.index("</note>", stop)
    old_form = (
        text[:note_end]
        + f'<lyric number="{GENERATED_LYRIC_NUMBER}" name="{GENERATED_LYRIC_NAME}"><extend /></lyric>'
        + text[note_end:]
    )
    old = tmp_path / "old.xml"
    old.write_text(old_form, encoding="utf-8")
    assert _generated_lyric_elements(old)[3] != []

    regenerate_solfege_verses(old, tmp_path / "regenerated.xml", part_ids=["P1"])

    assert _generated_lyric_elements(tmp_path / "regenerated.xml")[3] == []
    assert (
        parse_score(tmp_path / "regenerated.xml")["score_summary"]["parts"][0]["part_signature"]
        == parse_score(source)["score_summary"]["parts"][0]["part_signature"]
    )
