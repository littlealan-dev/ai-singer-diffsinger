from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from music21 import midi

from src.api.score import parse_score
from src.musicxml.instrument_programs import apply_llm_program_assignments
from src.musicxml.performance_midi import build_instrumental_performance_midis


MISSING_PROGRAM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1">
      <part-name>Lead Guitar</part-name>
      <score-instrument id="P1-I1">
        <instrument-name>Electric Guitar</instrument-name>
        <instrument-sound>pluck.guitar.electric</instrument-sound>
      </score-instrument>
    </score-part>
    <score-part id="P2"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <direction placement="above"><direction-type><words>clean electric guitar</words></direction-type></direction>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>sing</text></lyric></note>
    </measure>
  </part>
</score-partwise>"""


STANDARD_PERCUSSION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1">
      <part-name>Tambourine</part-name>
      <score-instrument id="P1-I1"><instrument-name>Tambourine</instrument-name></score-instrument>
      <midi-instrument id="P1-I1"><midi-channel>10</midi-channel><midi-unpitched>55</midi-unpitched></midi-instrument>
    </score-part>
  </part-list>
  <part id="P1"><measure number="1"><attributes><divisions>1</divisions></attributes>
    <note><unpitched><display-step>E</display-step><display-octave>4</display-octave></unpitched><duration>1</duration><type>quarter</type><instrument id="P1-I1"/></note>
  </measure></part>
</score-partwise>"""


BANKED_PERCUSSION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1">
      <part-name>Drums</part-name>
      <midi-device id="P1-I1">Example Device</midi-device>
      <score-instrument id="P1-I1">
        <instrument-name>Electronic drums</instrument-name>
        <virtual-instrument><virtual-library>Example Library</virtual-library><virtual-name>Example Kit</virtual-name></virtual-instrument>
      </score-instrument>
      <midi-instrument id="P1-I1"><midi-channel>10</midi-channel><midi-bank>16384</midi-bank><midi-program>26</midi-program><midi-unpitched>36</midi-unpitched></midi-instrument>
    </score-part>
  </part-list>
  <part id="P1"><measure number="1"><attributes><divisions>1</divisions></attributes>
    <note><unpitched><display-step>C</display-step><display-octave>5</display-octave></unpitched><duration>1</duration><type>quarter</type><instrument id="P1-I1"/></note>
  </measure></part>
</score-partwise>"""


SYNTHETIC_DRUM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Drumset</part-name></score-part></part-list>
  <part id="P1"><measure number="1"><attributes><divisions>1</divisions></attributes>
    <note><unpitched><display-step>C</display-step><display-octave>5</display-octave></unpitched><duration>1</duration><type>quarter</type></note>
  </measure></part>
</score-partwise>"""


def fluidr3_preset(*, bank: int = 0, program: int = 29, kind: str = "melodic") -> dict[str, object]:
    return {"soundfont_id": "FluidR3_GM", "bank": bank, "program": program, "kind": kind}


def test_parse_summary_exposes_facts_once_and_indexes_unresolved_instruments() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "missing-program.musicxml"
        source.write_text(MISSING_PROGRAM_XML, encoding="utf-8")
        parsed = parse_score(source)

    summary = parsed["score_summary"]
    guitar = summary["parts"][0]["instruments"][0]
    assert guitar["score_instrument_id"] == "P1-I1"
    assert guitar["instrument_name"] == "Electric Guitar"
    assert guitar["instrument_sound"] == "pluck.guitar.electric"
    assert guitar["native_midi_program"] is None
    assert guitar["resolved_gm_program"] is None
    assert guitar["program_source"] == "unresolved_missing_melodic_program"
    assert guitar["direction_words"] == [
        {"text": "clean electric guitar", "measure_number": "1", "placement": "above"}
    ]
    assert summary["instrument_program_resolution"] == {
        "instrumental_score_instrument_ids": ["P1-I1"],
        "unresolved_score_instrument_ids": ["P1-I1"],
        "unresolved_reasons": {"P1-I1": "unresolved_missing_melodic_program"},
        "playback_profile": "FluidR3_GM",
    }


def test_llm_assignment_requires_every_unresolved_id_and_persists_source() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "missing-program.musicxml"
        source.write_text(MISSING_PROGRAM_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]

    unchanged, action = apply_llm_program_assignments(summary, None)
    assert unchanged == summary
    assert action == {
        "status": "action_required",
        "action": "instrument_program_resolution_required",
        "message": "A FluidR3 playback preset is required for every listed instrumental route.",
        "unresolved_score_instrument_ids": ["P1-I1"],
    }

    resolved, action = apply_llm_program_assignments(
        summary,
        [
            {
                "score_instrument_id": "P1-I1",
                "playback_preset": fluidr3_preset(),
                "source": "llm_inferred",
                "evidence": ["instrument-name: Electric Guitar"],
            }
        ],
    )
    assert action is None
    assert resolved["instrument_program_resolution"]["unresolved_score_instrument_ids"] == []
    guitar = resolved["parts"][0]["instruments"][0]
    assert guitar["resolved_gm_program"] == 29
    assert guitar["playback_preset"] == fluidr3_preset()
    assert guitar["program_source"] == "llm_inferred"


def test_midi_export_uses_llm_assignment_without_name_based_fallback() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "missing-program.musicxml"
        source.write_text(MISSING_PROGRAM_XML, encoding="utf-8")
        written = root / "written.mid"
        expanded = root / "expanded.mid"
        result = build_instrumental_performance_midis(
            source,
            original_output_path=written,
            expanded_output_path=expanded,
            instrument_program_assignments={"P1-I1": fluidr3_preset()},
        )

        assert result["instrumental_parts"][0]["midi_program"] == 29
        midi_file = midi.MidiFile()
        midi_file.open(str(written))
        midi_file.read()
        midi_file.close()
        assert any(
            event.type == midi.ChannelVoiceMessages.PROGRAM_CHANGE and event.data == 29
            for track in midi_file.tracks
            for event in track.events
        )


def test_midi_export_never_defaults_an_unresolved_part_to_piano() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "missing-program.musicxml"
        source.write_text(MISSING_PROGRAM_XML, encoding="utf-8")
        result = build_instrumental_performance_midis(
            source,
            original_output_path=root / "written.mid",
            expanded_output_path=root / "expanded.mid",
        )

    guitar = result["instrumental_parts"][0]
    assert guitar["eligible"] is False
    assert guitar["midi_program"] is None
    assert guitar["diagnostic"] == "Part has no resolved General MIDI program."
    assert result["has_instrumental_parts"] is False


def test_channel_ten_without_a_bank_resolves_as_standard_gm_percussion() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "standard-percussion.musicxml"
        source.write_text(STANDARD_PERCUSSION_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]

    percussion = summary["parts"][0]["instruments"][0]
    assert percussion["is_percussion"] is True
    assert percussion["native_midi_program"] is None
    assert percussion["midi_unpitched_notes"] == [55]
    assert percussion["program_source"] == "standard_gm_percussion"
    assert percussion["playback_preset"] == fluidr3_preset(
        bank=128, program=0, kind="percussion_kit"
    )
    assert summary["instrument_program_resolution"]["unresolved_score_instrument_ids"] == []


def test_banked_percussion_keeps_source_facts_and_requires_fluidr3_preset() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "banked-percussion.musicxml"
        source.write_text(BANKED_PERCUSSION_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]

    drum = summary["parts"][0]["instruments"][0]
    assert drum["midi_device"] == "Example Device"
    assert drum["midi_devices"] == {"P1-I1": "Example Device"}
    assert drum["virtual_instrument_library"] == "Example Library"
    assert drum["virtual_instrument_name"] == "Example Kit"
    assert drum["source_midi"] == {
        "channel": 10,
        "bank": 16384,
        "program": 26,
        "unpitched_notes": [36],
    }
    assert drum["playback_preset"] is None
    assert summary["instrument_program_resolution"]["unresolved_reasons"] == {
        "P1-I1": "unresolved_nonportable_bank"
    }

    resolved, action = apply_llm_program_assignments(
        summary,
        [{
            "score_instrument_id": "P1-I1",
            "playback_preset": fluidr3_preset(bank=128, program=24, kind="percussion_kit"),
            "source": "llm_inferred",
            "evidence": ["instrument-name: Electronic drums"],
        }],
    )
    assert action is None
    assert resolved["parts"][0]["instruments"][0]["playback_preset"] == fluidr3_preset(
        bank=128, program=24, kind="percussion_kit"
    )


def test_banked_percussion_rejects_melodic_or_non_fluidr3_drum_presets() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "banked-percussion.musicxml"
        source.write_text(BANKED_PERCUSSION_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]

    _, action = apply_llm_program_assignments(
        summary,
        [{
            "score_instrument_id": "P1-I1",
            "playback_preset": fluidr3_preset(bank=0, program=24, kind="melodic"),
            "source": "llm_inferred",
            "evidence": [],
        }],
    )
    assert action is not None
    assert action["action"] == "instrument_program_resolution_required"


def test_synthetic_undeclared_drum_route_does_not_block_synthesis() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "synthetic-drum.musicxml"
        source.write_text(SYNTHETIC_DRUM_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]

    drum = summary["parts"][0]["instruments"][0]
    assert drum["synthetic"] is True
    assert drum["eligible_for_instrumental_midi"] is False
    assert summary["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
    assert summary["instrument_program_resolution"]["unresolved_score_instrument_ids"] == []
