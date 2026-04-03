"""Asset-light orchestration coverage adjacent to the real end-to-end suite."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.api import parse_score


SIMPLE_SCORE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<score-partwise version='3.1'>
  <part-list>
    <score-part id='P1'><part-name>Soprano</part-name></score-part>
  </part-list>
  <part id='P1'>
    <measure number='1'>
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>1</duration>
        <type>quarter</type>
        <lyric><text>la</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""

SATB_SCORE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<score-partwise version='3.1'>
  <part-list>
    <score-part id='P1'><part-name>Soprano</part-name></score-part>
    <score-part id='P2'><part-name>Alto</part-name></score-part>
    <score-part id='P3'><part-name>Tenor</part-name></score-part>
    <score-part id='P4'><part-name>Bass</part-name></score-part>
  </part-list>
  <part id='P1'>
    <measure number='1'>
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>so</text></lyric></note>
    </measure>
  </part>
  <part id='P2'>
    <measure number='1'>
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>A</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>al</text></lyric></note>
    </measure>
  </part>
  <part id='P3'>
    <measure number='1'>
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>te</text></lyric></note>
    </measure>
  </part>
  <part id='P4'>
    <measure number='1'>
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>ba</text></lyric></note>
    </measure>
  </part>
</score-partwise>
"""


class TestEndToEndOrchestration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _write_score(self, name: str, xml: str) -> Path:
        path = self.root / name
        path.write_text(xml, encoding="utf-8")
        return path

    def _fake_synthesize(self, score, voicebank_path: Path, *, part_index: int = 0):
        return {
            "waveform": [0.0, 0.1, 0.0],
            "sample_rate": 44100,
            "duration_seconds": 0.01,
            "part_index": part_index,
            "voicebank": str(voicebank_path),
            "score_parts": len(score.get("parts") or []),
        }

    def _fake_save_audio(self, waveform, output_path: Path, *, sample_rate: int):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFFTESTDATA")
        return {
            "path": str(output_path),
            "sample_rate": sample_rate,
            "duration_seconds": 0.01,
            "waveform_len": len(waveform),
        }

    def test_asset_light_full_render_flow(self) -> None:
        score_path = self._write_score("simple_score.xml", SIMPLE_SCORE_XML)
        voicebank_path = self.root / "voicebanks" / "Dummy"
        output_path = self.root / "output" / "simple.wav"

        score = parse_score(score_path, verse_number="1")
        result = self._fake_synthesize(score, voicebank_path)
        saved = self._fake_save_audio(result["waveform"], output_path, sample_rate=result["sample_rate"])

        self.assertEqual(result["part_index"], 0)
        self.assertEqual(result["score_parts"], 1)
        self.assertTrue(Path(saved["path"]).exists())
        self.assertGreater(Path(saved["path"]).stat().st_size, 0)

    def test_asset_light_tenor_part_selection_flow(self) -> None:
        score_path = self._write_score("satb_score.xml", SATB_SCORE_XML)
        voicebank_path = self.root / "voicebanks" / "DummyTenor"
        output_path = self.root / "output" / "tenor.wav"

        summary_score = parse_score(score_path)
        parts = summary_score.get("score_summary", {}).get("parts", [])
        tenor_part = next(
            (part for part in parts if "tenor" in (part.get("part_name") or "").lower()),
            None,
        )
        self.assertIsNotNone(tenor_part)
        tenor_index = tenor_part["part_index"]

        score = parse_score(score_path, verse_number="1")
        result = self._fake_synthesize(score, voicebank_path, part_index=tenor_index)
        saved = self._fake_save_audio(result["waveform"], output_path, sample_rate=result["sample_rate"])

        self.assertEqual(tenor_index, 2)
        self.assertEqual(result["part_index"], tenor_index)
        self.assertTrue(Path(saved["path"]).exists())

    def test_asset_light_soprano_part_selection_flow(self) -> None:
        score_path = self._write_score("satb_score.xml", SATB_SCORE_XML)
        voicebank_path = self.root / "voicebanks" / "DummySoprano"
        output_path = self.root / "output" / "soprano.wav"

        summary_score = parse_score(score_path)
        parts = summary_score.get("score_summary", {}).get("parts", [])
        soprano_part = next(
            (part for part in parts if "soprano" in (part.get("part_name") or "").lower()),
            None,
        )
        self.assertIsNotNone(soprano_part)
        soprano_index = soprano_part["part_index"]

        score = parse_score(score_path, verse_number="1")
        result = self._fake_synthesize(score, voicebank_path, part_index=soprano_index)
        saved = self._fake_save_audio(result["waveform"], output_path, sample_rate=result["sample_rate"])

        self.assertEqual(soprano_index, 0)
        self.assertEqual(result["part_index"], soprano_index)
        self.assertTrue(Path(saved["path"]).exists())
