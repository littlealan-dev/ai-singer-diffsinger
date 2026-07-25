from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from src.musicxml import parse_musicxml
from src.musicxml.part_reference import resolve_part_reference
from src.musicxml.parser import parse_musicxml_with_summary


TEST_XML = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "test_data"
    / "amazing-grace-satb-verse1.xml"
)
TEST_CHRISTMAS_MXL = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "test_data"
    / "all-i-want-for-christmas-is-you-mariah-carey.mxl"
)
TEST_SOLFEGE_MXL = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "test_data"
    / "amazing-grace-solfege.mxl"
)


class MusicXmlParserTests(unittest.TestCase):
    def test_summary_includes_bounded_lyric_samples_per_part_and_verse(self) -> None:
        _, summary = parse_musicxml_with_summary(TEST_SOLFEGE_MXL)
        soprano = next(part for part in summary["parts"] if part["part_id"] == "Soprano")

        verses = {
            entry["verse_number"]: entry["sample"]
            for entry in soprano["lyric_verses"]
        }
        self.assertEqual(list(verses), ["1", "2", "3"])
        self.assertTrue(all(len(sample) <= 20 for sample in verses.values()))
        self.assertEqual(
            verses["3"][:6],
            ["sol", "doh", "mi", "re", "doh", "mi"],
        )
        self.assertEqual(len(verses["3"]), 20)
        self.assertNotIn("+", verses["3"])

    def test_parse_basic(self) -> None:
        score = parse_musicxml(TEST_XML)
        self.assertEqual(score.title, "Amazing Grace— How Sweet the Sound")
        self.assertEqual(len(score.parts), 2)
        self.assertGreater(len(score.tempos), 0)
        self.assertEqual(score.tempos[0].bpm, 120.0)
        self.assertEqual(score.parts[0].part_name, "SOPRANO ALTO")

    def test_offsets_use_absolute_beats(self) -> None:
        score = parse_musicxml(TEST_XML)
        part = score.parts[0]
        offsets = [event.offset_beats for event in part.notes]
        self.assertTrue(all(b >= a for a, b in zip(offsets, offsets[1:])))
        self.assertGreater(offsets[-1], offsets[0])

    def test_lyrics_only_filters_primary_part(self) -> None:
        score = parse_musicxml(TEST_XML, lyrics_only=True)
        part = score.parts[0]
        self.assertGreater(len(part.notes), 0)
        self.assertEqual(part.notes[0].lyric, "1.A")
        self.assertTrue(all(event.lyric is not None for event in part.notes))

    def test_strips_redundant_whitespace_separated_verse_label(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type>
      <lyric number="1"><text>1.&#160;每</text></lyric>
    </note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "numbered-chinese-lyric.xml"
            path.write_text(xml, encoding="utf-8")
            score = parse_musicxml(path, lyrics_only=False)

        self.assertEqual(score.parts[0].notes[0].lyric, "每")

    def test_slur_notes_use_plus_marker(self) -> None:
        score = parse_musicxml(TEST_XML, lyrics_only=True)
        part = score.parts[0]
        slur_notes = [event for event in part.notes if event.lyric == "+"]
        self.assertTrue(slur_notes)
        for event in slur_notes:
            self.assertTrue(event.lyric_is_extended)
        ing_notes = [
            event for event in part.notes
            if event.lyric and event.lyric.lower() == "ing"
        ]
        self.assertTrue(ing_notes)
        ing_note = ing_notes[0]
        ing_end = ing_note.offset_beats + ing_note.duration_beats
        matches = [
            event for event in part.notes
            if event.lyric == "+"
            and event.voice == ing_note.voice
            and abs(event.offset_beats - ing_end) < 1e-6
        ]
        self.assertTrue(matches)

    def test_lyrics_only_keeps_non_lyric_parts(self) -> None:
        score = parse_musicxml(TEST_XML, lyrics_only=True, part_index=1)
        part = score.parts[0]
        self.assertGreater(len(part.notes), 0)
        self.assertTrue(any(event.lyric is None for event in part.notes))

    def test_tie_type_extraction(self) -> None:
        score = parse_musicxml(TEST_XML)
        part = score.parts[0]
        tie_types = [event.tie_type for event in part.notes if event.tie_type is not None]
        self.assertTrue(tie_types)
        self.assertIn("start", tie_types)

    def test_dot_count_extraction(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>2</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>3</duration>
        <voice>1</voice>
        <type>quarter</type>
        <dot/>
        <lyric><text>Dot</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dotted.xml"
            path.write_text(xml, encoding="utf-8")
            score = parse_musicxml(path, lyrics_only=False)

        self.assertEqual(score.parts[0].notes[0].dot_count, 1)

    def test_keep_rests_includes_rest_events(self) -> None:
        score = parse_musicxml(TEST_XML, keep_rests=True)
        part = score.parts[0]
        self.assertTrue(any(event.is_rest for event in part.notes))
        offsets = [event.offset_beats for event in part.notes]
        self.assertTrue(all(b >= a for a, b in zip(offsets, offsets[1:])))

    def test_keep_rests_false_excludes_rests(self) -> None:
        score = parse_musicxml(TEST_XML, keep_rests=False)
        part = score.parts[0]
        self.assertFalse(any(event.is_rest for event in part.notes))

    def test_tempo_offsets_use_absolute_beats_in_mxl(self) -> None:
        score = parse_musicxml(TEST_CHRISTMAS_MXL)
        tempos = list(score.tempos)
        self.assertGreaterEqual(len(tempos), 2)
        self.assertAlmostEqual(tempos[0].offset_beats, 0.0, places=6)
        self.assertAlmostEqual(tempos[0].bpm, 69.0, places=6)
        tempo_148 = [event for event in tempos if abs(event.bpm - 148.0) < 1e-6]
        self.assertTrue(tempo_148, "Expected a 148 BPM tempo event.")
        self.assertAlmostEqual(tempo_148[0].offset_beats, 40.0, places=6)

    def test_harmony_symbols_do_not_become_note_events(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 2.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="2.0">
  <part-list>
    <score-part id="P1">
      <part-name>Voice</part-name>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <harmony>
        <root><root-step>C</root-step></root>
        <kind text="">major</kind>
      </harmony>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>whole</type>
        <lyric><text>Hello</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "harmony-symbol.xml"
            path.write_text(xml, encoding="utf-8")
            score = parse_musicxml(path, lyrics_only=False)

        self.assertEqual(len(score.parts), 1)
        notes = [event for event in score.parts[0].notes if not event.is_rest]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pitch_midi, 60.0)
        self.assertEqual(notes[0].lyric, "Hello")

    def test_raw_xml_voice_fallback_applies_when_music21_voice_context_missing(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>whole</type>
        <lyric><text>la</text></lyric>
      </note>
    </measure>
    <measure number="2">
      <note>
        <rest/>
        <duration>4</duration>
        <voice>1</voice>
        <type>whole</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "single-voice.xml"
            path.write_text(xml, encoding="utf-8")
            score = parse_musicxml(path, keep_rests=True, lyrics_only=False)

        self.assertEqual(len(score.parts), 1)
        voices = {event.voice for event in score.parts[0].notes}
        self.assertEqual(voices, {"1"})

    def test_raw_xml_voice_fallback_skips_multi_voice_parts(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>2</duration>
        <voice>1</voice>
        <type>half</type>
      </note>
      <backup><duration>2</duration></backup>
      <note>
        <pitch><step>E</step><octave>3</octave></pitch>
        <duration>2</duration>
        <voice>2</voice>
        <type>half</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "multi-voice.xml"
            path.write_text(xml, encoding="utf-8")
            score = parse_musicxml(path, keep_rests=False, lyrics_only=False)

        self.assertEqual(len(score.parts), 1)
        voices = {event.voice for event in score.parts[0].notes}
        self.assertEqual(voices, {"1", "2"})

    def test_lyric_selection_maps_derived_parts_after_staff_expansion(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Soprano</part-name></score-part>
    <score-part id="P2"><part-name>Piano</part-name></score-part>
    <score-part id="P_DERIVED_1"><part-name>Soprano - voice part 1 (Derived)</part-name></score-part>
    <score-part id="P_DERIVED_2"><part-name>Soprano - voice part 2 (Derived)</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="1"><attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>source</text></lyric></note></measure></part>
  <part id="P2"><measure number="1"><attributes><divisions>1</divisions><staves>2</staves><time><beats>1</beats><beat-type>4</beat-type></time><clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><staff>1</staff></note><backup><duration>1</duration></backup><note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><staff>2</staff><lyric><text>piano</text></lyric></note></measure></part>
  <part id="P_DERIVED_1"><measure number="1"><attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes><note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>high</text></lyric></note></measure></part>
  <part id="P_DERIVED_2"><measure number="1"><attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes><note><pitch><step>B</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>low</text></lyric></note></measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "derived-with-piano.xml"
            path.write_text(xml, encoding="utf-8")
            _parsed, summary = parse_musicxml_with_summary(path)
            derived_index = next(
                index
                for index, part in enumerate(summary["parts"])
                if part["part_name"] == "Soprano - voice part 2 (Derived)"
            )
            selection = summary["parts"][derived_index]["lyric_selections"][0]
            selected = parse_musicxml(path, lyric_selection=selection, lyrics_only=False)
            reference = resolve_part_reference(
                source_path=path, part_id=summary["parts"][derived_index]["part_id"]
            )
            with self.assertRaises(ValueError):
                resolve_part_reference(source_path=path, part_id="P_DERIVED_2")

        self.assertEqual(summary["parts"][derived_index]["raw_part_id"], "P_DERIVED_2")
        self.assertEqual(selected.parts[derived_index].raw_part_id, "P_DERIVED_2")
        self.assertEqual(selection["number"], "1")
        self.assertEqual(
            [event.lyric for event in selected.parts[derived_index].notes], ["low"]
        )
        self.assertEqual(reference.raw_part_id, "P_DERIVED_2")
        self.assertEqual(reference.raw_part_index, 3)
        self.assertEqual(reference.parser_part_index, derived_index)

    def test_part_reference_maps_expanded_part_without_unique_name(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P5"><part-name>Keyboard</part-name></score-part>
    <score-part id="P9"><part-name>Keyboard</part-name></score-part>
  </part-list>
  <part id="P5"><measure number="1"><attributes><divisions>1</divisions><staves>2</staves><time><beats>1</beats><beat-type>4</beat-type></time><clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef></attributes><note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><staff>1</staff></note><backup><duration>1</duration></backup><note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><staff>2</staff></note></measure></part>
  <part id="P9"><measure number="1"><attributes><divisions>1</divisions><time><beats>1</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note></measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicate-names.xml"
            path.write_text(xml, encoding="utf-8")
            parsed = parse_musicxml(path, lyrics_only=False)
            expanded_index = next(
                index
                for index, part in enumerate(parsed.parts)
                if part.part_id != "P5" and part.part_id.startswith("P5")
            )
            reference = resolve_part_reference(
                source_path=path, part_id=parsed.parts[expanded_index].part_id
            )

        self.assertEqual(reference.raw_part_id, "P5")
        self.assertEqual(reference.parser_part_index, expanded_index)


if __name__ == "__main__":
    unittest.main()
