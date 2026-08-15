from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from music21 import converter, tempo

from src.musicxml.performance_midi import build_instrumental_performance_midis


PIANO_REPEAT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1">
      <part-name>Piano</part-name>
      <score-instrument id="P1-I1"><instrument-name>Acoustic Grand Piano</instrument-name></score-instrument>
      <midi-instrument id="P1-I1"><midi-channel>1</midi-channel><midi-program>1</midi-program></midi-instrument>
    </score-part>
    <score-part id="P2"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1"><attributes><divisions>1</divisions></attributes><direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>88</per-minute></metronome></direction-type><sound tempo="88"/></direction><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note></measure>
    <measure number="2"><note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note><barline location="right"><repeat direction="backward"/></barline></measure>
  </part>
  <part id="P2">
    <measure number="1"><attributes><divisions>1</divisions></attributes><note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>sing</text></lyric></note></measure>
    <measure number="2"><note><pitch><step>D</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>now</text></lyric></note></measure>
  </part>
</score-partwise>"""


def test_builds_written_and_expanded_instrumental_midis() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "score.musicxml"
        source.write_text(PIANO_REPEAT_XML)
        written = root / "written.mid"
        expanded = root / "expanded.mid"

        result = build_instrumental_performance_midis(
            source,
            original_output_path=written,
            expanded_output_path=expanded,
        )

        assert result["has_instrumental_parts"] is True
        assert result["original_midi_available"] is True
        assert result["expanded_midi_available"] is True
        assert written.read_bytes().startswith(b"MThd")
        assert expanded.read_bytes().startswith(b"MThd")
        assert written.read_bytes() != expanded.read_bytes()
        for midi_path in (written, expanded):
            midi_score = converter.parse(str(midi_path))
            tempos = list(midi_score.recurse().getElementsByClass(tempo.MetronomeMark))
            assert tempos[0].getQuarterBPM() == 88
        piano, voice = result["instrumental_parts"]
        assert piano["eligible"] is True
        assert piano["midi_program"] == 0
        assert voice["eligible"] is False
        assert "lyrics" in voice["diagnostic"]
