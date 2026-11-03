"""Original MusicXML evidence sent to planner v2."""

from __future__ import annotations

from copy import deepcopy
from xml.etree import ElementTree as ET

import pytest

from src.api import parse_score
from src.api.preprocess_evidence import EvidenceUnavailable, trim_musicxml
from tests.test_preprocess_sheet import SATB


def _xml() -> str:
    return '''<score-partwise version="3.1"><defaults><scaling><millimeters>7</millimeters>
<tenths>40</tenths></scaling></defaults><credit><credit-words>Layout</credit-words></credit>
<part-list><score-part id="P1"><part-name>Choir</part-name>
<midi-instrument id="I1"><midi-channel>1</midi-channel></midi-instrument></score-part>
<score-part id="P2"><part-name>Other</part-name></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>1</divisions>
<key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4</beat-type></time>
<clef><sign>G</sign><line>2</line></clef></attributes>
<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
<voice>1</voice><type>quarter</type><lyric><text>one</text></lyric></note>
<backup><duration>1</duration></backup>
<note><pitch><step>E</step><octave>3</octave></pitch><duration>1</duration>
<voice>2</voice><type>quarter</type></note></measure>
<measure number="2"><attributes><divisions>2</divisions><key><fifths>1</fifths></key>
<time><beats>3</beats><beat-type>4</beat-type></time>
<clef><sign>F</sign><line>4</line></clef></attributes>
<note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration>
<voice>1</voice><type>quarter</type><lyric><text>two</text></lyric></note>
<backup><duration>2</duration></backup>
<note><pitch><step>F</step><octave>3</octave></pitch><duration>2</duration>
<voice>2</voice><type>quarter</type></note></measure>
<measure number="3"><print new-system="yes"/><direction placement="above">
<direction-type><words default-y="10">softly</words></direction-type></direction>
<note default-x="20" print-object="yes"><pitch><step>E</step><octave>4</octave></pitch>
<duration>2</duration><voice>1</voice><type>quarter</type><stem>up</stem>
<notations><slur type="start" bezier-x="1"/></notations>
<lyric><text>three</text></lyric></note><backup><duration>2</duration></backup>
<note><pitch><step>G</step><octave>3</octave></pitch><duration>2</duration>
<voice>2</voice><type>quarter</type><tie type="start"/></note></measure></part>
<part id="P2"><measure number="1"><attributes><divisions>1</divisions></attributes>
<note><rest/><duration>1</duration><type>quarter</type></note></measure>
<measure number="2"><note><rest/><duration>1</duration><type>quarter</type></note></measure>
<measure number="3"><note><rest/><duration>1</duration><type>quarter</type></note>
</measure></part></score-partwise>'''


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def test_trim_keeps_music_and_carries_attributes(tmp_path):
    source = tmp_path / "source.xml"
    source.write_text(_xml(), encoding="utf-8")
    score = parse_score(source, verse_number=1)
    trimmed = trim_musicxml(score, ["P1"], {3})
    root = ET.fromstring(trimmed)
    assert [_local(part.tag) for part in root if _local(part.tag) == "part"] == ["part"]
    assert [entry.get("id") for entry in root.iter() if _local(entry.tag) == "score-part"] == ["P1"]
    attributes = next(item for item in root.iter() if _local(item.tag) == "attributes")
    assert next(item for item in attributes if _local(item.tag) == "divisions").text == "2"
    assert next(item for item in attributes.iter() if _local(item.tag) == "fifths").text == "1"
    assert next(item for item in attributes.iter() if _local(item.tag) == "beats").text == "3"
    assert next(item for item in attributes.iter() if _local(item.tag) == "sign").text == "F"
    assert all(_local(item.tag) not in {"defaults", "credit", "print", "midi-instrument"}
               for item in root.iter())
    assert all(key not in trimmed for key in ("default-x", "default-y", "print-object", "bezier-x"))
    assert all(tag in trimmed for tag in ("slur", "lyric", "direction", "stem", "tie"))

    trimmed_path = tmp_path / "trimmed.xml"
    trimmed_path.write_text(trimmed, encoding="utf-8")
    reparsed = parse_score(trimmed_path, verse_number=1)
    original_notes = [note for note in score["parts"][0]["notes"] if note["measure_number"] == 3]
    new_notes = reparsed["parts"][0]["notes"]
    assert len(original_notes) == len(new_notes)
    original_start = min(note["offset_beats"] for note in original_notes)
    new_start = min(note["offset_beats"] for note in new_notes)
    fields = ("pitch_midi", "duration_beats", "voice", "staff", "tie_type", "lyric")
    for old, new in zip(original_notes, new_notes):
        assert tuple(old.get(key) for key in fields) == tuple(new.get(key) for key in fields)
        assert old["offset_beats"] - original_start == new["offset_beats"] - new_start


def test_trim_uses_raw_part_id_for_split_staff_and_derived_part(tmp_path):
    score = parse_score(SATB, verse_number=1)
    assert score["parts"][0]["raw_part_id"] == score["parts"][1]["raw_part_id"] == "P1"
    assert score["parts"][0]["part_id"] != "P1"
    xml = trim_musicxml(score, ["P1"], {2})
    assert len([item for item in ET.fromstring(xml).iter() if _local(item.tag) == "part"]) == 1
    assert '<staff>2</staff>' in xml

    source = tmp_path / "with-derived.xml"
    root = ET.fromstring(_xml())
    part_list = next(item for item in root if item.tag == "part-list")
    derived_entry = deepcopy(next(item for item in part_list if item.get("id") == "P1"))
    derived_entry.set("id", "P3")
    part_list.append(derived_entry)
    derived_part = deepcopy(next(item for item in root if item.tag == "part" and item.get("id") == "P1"))
    derived_part.set("id", "P3")
    root.append(derived_part)
    source.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    derived = parse_score(source, verse_number=1)
    derived["parts"][2]["is_derived_part"] = True
    assert derived["parts"][2]["raw_part_id"] == "P3"
    assert _local(ET.fromstring(trim_musicxml(derived, ["P3"], {3})).tag) == "score-partwise"
    assert 'id="P3"' in trim_musicxml(derived, ["P3"], {3})


def test_trim_carries_state_before_midbar_attribute_change(tmp_path):
    source = tmp_path / "midbar.xml"
    source.write_text('''<score-partwise version="3.1"><part-list><score-part id="P1">
<part-name>Voice</part-name></score-part></part-list><part id="P1">
<measure number="1"><attributes><divisions>1</divisions><key><fifths>0</fifths></key>
</attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
<type>quarter</type></note></measure>
<measure number="2"><note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration>
<type>quarter</type></note><attributes><divisions>2</divisions><key><fifths>1</fifths></key>
</attributes><note><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration>
<type>quarter</type></note></measure>
<measure number="3"><note><pitch><step>F</step><octave>4</octave></pitch><duration>2</duration>
<type>quarter</type></note></measure></part></score-partwise>''', encoding="utf-8")
    score = parse_score(source, verse_number=1)
    trimmed = trim_musicxml(score, ["P1"], {2})
    measure = next(item for item in ET.fromstring(trimmed).iter() if _local(item.tag) == "measure")
    assert [_local(item.tag) for item in measure] == ["attributes", "note", "attributes", "note"]
    first, second = [item for item in measure if _local(item.tag) == "attributes"]
    assert next(item for item in first if _local(item.tag) == "divisions").text == "1"
    assert next(item for item in second if _local(item.tag) == "divisions").text == "2"
    path = tmp_path / "trimmed.xml"
    path.write_text(trimmed, encoding="utf-8")
    reparsed = parse_score(path, verse_number=1)
    old = [note for note in score["parts"][0]["notes"] if note["measure_number"] == 2]
    new = reparsed["parts"][0]["notes"]
    assert [(note["pitch_midi"], note["duration_beats"]) for note in old] == [
        (note["pitch_midi"], note["duration_beats"]) for note in new
    ]
    after = trim_musicxml(score, ["P1"], {3})
    carried = next(item for item in ET.fromstring(after).iter() if _local(item.tag) == "attributes")
    assert next(item for item in carried if _local(item.tag) == "divisions").text == "2"
    assert next(item for item in carried.iter() if _local(item.tag) == "fifths").text == "1"


def test_trim_fails_closed_for_missing_duplicate_and_unreadable_bars(tmp_path):
    source = tmp_path / "source.xml"
    source.write_text(_xml(), encoding="utf-8")
    score = parse_score(source, verse_number=1)
    missing = deepcopy(score)
    missing["source_musicxml_path"] = str(tmp_path / "absent.xml")
    with pytest.raises(EvidenceUnavailable, match="unreadable_path"):
        trim_musicxml(missing, ["P1"], {3})
    with pytest.raises(EvidenceUnavailable, match="missing_measure"):
        trim_musicxml(score, ["P1"], {9})
    source.write_text(_xml().replace('<measure number="2">', '<measure number="1">', 1),
                      encoding="utf-8")
    with pytest.raises(EvidenceUnavailable, match="duplicate_measure_number"):
        trim_musicxml(score, ["P1"], {3})
    source.write_text(_xml().replace('<measure number="2">', '<measure number="12a">', 1),
                      encoding="utf-8")
    with pytest.raises(EvidenceUnavailable, match="unreadable_measure_number"):
        trim_musicxml(score, ["P1"], {3})
