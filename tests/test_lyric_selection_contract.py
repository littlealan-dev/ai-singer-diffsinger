from pathlib import Path

from src.api.score import parse_score
from src.api.solfege import add_solfege_lyric_verse
from src.api.voice_parts import _selected_part_solfege_diagnostics


_SCORE = """<?xml version='1.0' encoding='UTF-8'?>
<score-partwise version='3.1'>
 <part-list>
  <score-part id='P1'><part-name>Soprano</part-name></score-part>
  <score-part id='P2'><part-name>Alto</part-name></score-part>
 </part-list>
 <part id='P1'><measure number='1'><attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>2</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
  <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric number='part1verse1'><text>word</text></lyric></note>
  <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric number='part1verse1'><text>word</text></lyric></note>
 </measure></part>
 <part id='P2'><measure number='1'><attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>2</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
  <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric number='part2verse1'><text>word</text></lyric></note>
  <note><pitch><step>F</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric number='part2verse1'><text>word</text></lyric></note>
 </measure></part>
</score-partwise>"""


_DERIVED_LANES_WITH_STAFF_SCORE = """<?xml version='1.0' encoding='UTF-8'?>
<score-partwise version='3.1'>
 <part-list>
  <score-part id='P1'><part-name>Piano</part-name></score-part>
  <score-part id='P_DERIVED_1'><part-name>Soprano - voice part 1 (Derived)</part-name></score-part>
  <score-part id='P_DERIVED_2'><part-name>Soprano - voice part 2 (Derived)</part-name></score-part>
 </part-list>
 <part id='P1'><measure number='1'><attributes><divisions>1</divisions><staves>2</staves><time><beats>1</beats><beat-type>4</beat-type></time><clef number='1'><sign>G</sign><line>2</line></clef><clef number='2'><sign>F</sign><line>4</line></clef></attributes>
  <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><staff>1</staff></note>
  <backup><duration>1</duration></backup><note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><staff>2</staff></note>
 </measure></part>
 <part id='P_DERIVED_1'><measure number='1'><attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>1</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
  <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>high</text></lyric></note>
 </measure></part>
 <part id='P_DERIVED_2'><measure number='1'><attributes><divisions>1</divisions><key><fifths>0</fifths></key><time><beats>1</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
  <note><pitch><step>B</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>low</text></lyric></note>
 </measure></part>
</score-partwise>"""


def _parser_part_id(path: Path, raw_part_id: str) -> str:
    summary = parse_score(path)["score_summary"]
    return next(
        part["part_id"]
        for part in summary["parts"]
        if part["raw_part_id"] == raw_part_id
    )


def test_generated_solfege_uses_exact_raw_selection_per_part(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    first = tmp_path / "first.xml"
    second = tmp_path / "second.xml"
    source.write_text(_SCORE, encoding="utf-8")

    soprano = add_solfege_lyric_verse(
        source, first, part_id=_parser_part_id(source, "P1")
    )
    alto = add_solfege_lyric_verse(
        first, second, part_id=_parser_part_id(first, "P2")
    )

    soprano_selection = soprano["lyric_selection"]
    alto_selection = alto["lyric_selection"]
    assert soprano_selection["number"] == alto_selection["number"] == "SSSolfege"
    assert soprano_selection["name"] == alto_selection["name"] == "SightSinger Solfege"
    assert soprano_selection["id"] != alto_selection["id"]

    parsed = parse_score(second, lyric_selection=alto_selection)
    alto_notes = [note for note in parsed["parts"][1]["notes"] if note.get("lyric")]
    assert [note["lyric"] for note in alto_notes] == ["mi", "fa"]
    assert all(note["lyric_name"] == "SightSinger Solfege" for note in alto_notes)
    assert _selected_part_solfege_diagnostics(parsed["parts"][1])["is_solfege"] is True


def test_mixed_collision_cannot_pass_generated_solfege_validation(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    output = tmp_path / "output.xml"
    source.write_text(_SCORE, encoding="utf-8")
    add_solfege_lyric_verse(
        source, output, part_id=_parser_part_id(source, "P1")
    )

    default_parse = parse_score(output)
    assert _selected_part_solfege_diagnostics(default_parse["parts"][0])["is_solfege"] is False


def test_solfege_targets_each_derived_lane_after_staff_expansion(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    first = tmp_path / "first.xml"
    second = tmp_path / "second.xml"
    source.write_text(_DERIVED_LANES_WITH_STAFF_SCORE, encoding="utf-8")

    upper_part_id = _parser_part_id(source, "P_DERIVED_1")
    upper = add_solfege_lyric_verse(source, first, part_id=upper_part_id)
    lower_part_id = _parser_part_id(first, "P_DERIVED_2")
    lower = add_solfege_lyric_verse(first, second, part_id=lower_part_id)

    assert upper["target"] == {
        "part_id": upper_part_id,
        "raw_part_id": "P_DERIVED_1",
        "part_index": 2,
        "part_name": "Soprano - voice part 1 (Derived)",
    }
    assert lower["target"] == {
        "part_id": lower_part_id,
        "raw_part_id": "P_DERIVED_2",
        "part_index": 3,
        "part_name": "Soprano - voice part 2 (Derived)",
    }
    summary = parse_score(second)["score_summary"]
    for part_index in (2, 3):
        assert any(
            selection["name"] == "SightSinger Solfege"
            for selection in summary["parts"][part_index]["lyric_selections"]
        )
