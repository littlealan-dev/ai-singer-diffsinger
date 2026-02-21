from __future__ import annotations

import unittest
from pathlib import Path

from src.musicxml import parse_musicxml


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


class MusicXmlParserTests(unittest.TestCase):
    def test_parse_basic(self) -> None:
        score = parse_musicxml(TEST_XML)
        self.assertEqual(score.title, "Amazing Grace— How Sweet the Sound")
        self.assertEqual(len(score.parts), 1)
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


if __name__ == "__main__":
    unittest.main()
