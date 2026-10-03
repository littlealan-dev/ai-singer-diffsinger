from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from music21 import midi

from src.api.score import parse_score
from src.backend.synthesis_pricing import score_has_instrumental_parts
from src.musicxml.instrument_programs import (
    apply_llm_program_assignments,
    drop_resolved_program_assignments,
    instrumental_programs_by_part,
    llm_program_assignments_from_summary,
)
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


@pytest.mark.parametrize("sound_id", ["voice.vocals", "voice.female", "voice.male"])
@pytest.mark.parametrize("has_lyrics", [True, False])
@pytest.mark.parametrize("midi_fields", [
    "<midi-channel>1</midi-channel><midi-program>69</midi-program>",
    "<midi-channel>1</midi-channel>",
    "<midi-channel>1</midi-channel><midi-bank>2</midi-bank><midi-program>69</midi-program>",
])
def test_explicit_vocal_role_excluded_from_program_preflight(
    tmp_path: Path, sound_id: str, has_lyrics: bool, midi_fields: str,
) -> None:
    xml = MISSING_PROGRAM_XML.replace(
        '<score-part id="P2"><part-name>Voice</part-name></score-part>',
        f'''<score-part id="P2"><part-name>Uninformative name</part-name>
        <score-instrument id="P2-I1"><instrument-name>Stimme</instrument-name>
        <instrument-sound>{sound_id}</instrument-sound></score-instrument>
        <midi-instrument id="P2-I1">{midi_fields}</midi-instrument></score-part>''',
    )
    if not has_lyrics:
        xml = xml.replace('<lyric><text>sing</text></lyric>', '')
    source = tmp_path / "vocal-preset.musicxml"
    source.write_text(xml, encoding="utf-8")
    summary = parse_score(source)["score_summary"]
    vocal = summary["parts"][1]["instruments"][0]
    assert vocal["is_explicit_vocal"] is True
    assert vocal["eligible_for_instrumental_midi"] is False
    resolution = summary["instrument_program_resolution"]
    assert resolution["instrumental_score_instrument_ids"] == ["P1-I1"]
    assert resolution["unresolved_score_instrument_ids"] == ["P1-I1"]
    assert "P2" not in instrumental_programs_by_part(source)
    # An old/stale assignment cannot turn an explicitly vocal route into MIDI.
    facts = instrumental_programs_by_part(
        source, assignments={"P2-I1": fluidr3_preset()}, include_ineligible=True,
    )["P2"]
    assert facts["eligible_for_instrumental_midi"] is False
    assert facts["program_source"] != "llm_inferred"


def test_vocal_sounding_part_name_does_not_override_instrument_facts(tmp_path: Path) -> None:
    xml = MISSING_PROGRAM_XML.replace("Lead Guitar", "Voice").replace(
        "</score-instrument>",
        '</score-instrument><midi-instrument id="P1-I1"><midi-program>25</midi-program></midi-instrument>',
        1,
    )
    source = tmp_path / "instrument-named-voice.musicxml"
    source.write_text(xml, encoding="utf-8")
    facts = instrumental_programs_by_part(source)["P1"]
    assert facts["is_explicit_vocal"] is False
    assert facts["eligible_for_instrumental_midi"] is True
    assert facts["resolved_gm_program"] == 24


def test_lyrics_make_a_part_vocal_whatever_program_it_declares(tmp_path: Path) -> None:
    """Like assets/test_data/amazing-grace-with-piano.xml: the voices declare a piano program."""
    xml = MISSING_PROGRAM_XML.replace(
        "</score-instrument>",
        '</score-instrument><midi-instrument id="P1-I1"><midi-program>25</midi-program></midi-instrument>',
        1,
    ).replace(
        '<score-part id="P2"><part-name>Voice</part-name></score-part>',
        """<score-part id="P2"><part-name>Soprano</part-name>
        <score-instrument id="P2-I1"><instrument-name>ARIA Player</instrument-name></score-instrument>
        <midi-instrument id="P2-I1"><midi-channel>2</midi-channel><midi-program>1</midi-program></midi-instrument>
        </score-part>""",
    )
    source = tmp_path / "voice-with-piano-program.musicxml"
    source.write_text(xml, encoding="utf-8")

    summary = parse_score(source)["score_summary"]
    soprano = summary["parts"][1]["instruments"][0]
    assert soprano["native_midi_program"] == 1
    assert soprano["eligible_for_instrumental_midi"] is False
    resolution = summary["instrument_program_resolution"]
    assert resolution["instrumental_score_instrument_ids"] == ["P1-I1"]
    assert resolution["unresolved_score_instrument_ids"] == []
    assert instrumental_programs_by_part(source, include_ineligible=True)["P2"][
        "eligible_for_instrumental_midi"
    ] is False

    result = build_instrumental_performance_midis(
        source,
        original_output_path=tmp_path / "written.mid",
        expanded_output_path=tmp_path / "expanded.mid",
    )
    guitar, voice = result["instrumental_parts"]
    assert guitar["eligible"] is True
    assert voice["eligible"] is False
    assert voice["has_lyrics"] is True
    assert "lyrics" in voice["diagnostic"]


# A two-staff part declaring a piano, with lyrics under the upper staff only,
# as in a hymn printed on two staves.
TWO_STAFF_LYRICS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1">
      <part-name>Hymn</part-name>
      <score-instrument id="P1-I1"><instrument-name>Piano</instrument-name></score-instrument>
      <midi-instrument id="P1-I1"><midi-channel>1</midi-channel><midi-program>1</midi-program></midi-instrument>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><staves>2</staves>
        <clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef>
      </attributes>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type><staff>1</staff><lyric><text>Praise</text></lyric></note>
      <backup><duration>4</duration></backup>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration><voice>5</voice><type>whole</type><staff>2</staff></note>
    </measure>
  </part>
</score-partwise>"""


def test_lyrics_on_one_staff_make_every_staff_of_the_part_vocal(tmp_path: Path) -> None:
    source = tmp_path / "two-staff-hymn.musicxml"
    source.write_text(TWO_STAFF_LYRICS_XML, encoding="utf-8")

    summary = parse_score(source)["score_summary"]
    staves = [part for part in summary["parts"] if part.get("raw_part_id") == "P1"]
    assert len(staves) >= 2
    assert any(not staff["has_lyrics"] for staff in staves)
    for staff in staves:
        assert all(not item["eligible_for_instrumental_midi"] for item in staff["instruments"])
    assert summary["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
    assert score_has_instrumental_parts(summary) is False

    result = build_instrumental_performance_midis(
        source,
        original_output_path=tmp_path / "written.mid",
        expanded_output_path=tmp_path / "expanded.mid",
    )
    assert len(result["instrumental_parts"]) == 2
    assert all(part["has_lyrics"] and not part["eligible"] for part in result["instrumental_parts"])
    assert result["has_instrumental_parts"] is False


# Like assets/test_data/happy-birthday-transcribed.xml: the voices have lyrics
# and no part declares an instrument, including the two-staff Piano.
UNDECLARED_ACCOMPANIMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Sop</part-name></score-part>
    <score-part id="P2"><part-name>Tenor</part-name></score-part>
    <score-part id="P3"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>4</duration><type>whole</type><lyric><text>Hap</text></lyric></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
  <part id="P3">
    <measure number="1">
      <attributes><divisions>1</divisions><staves>2</staves><clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type><staff>1</staff></note>
      <backup><duration>4</duration></backup>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration><type>whole</type><staff>2</staff></note>
    </measure>
  </part>
</score-partwise>"""


def _undeclared_accompaniment(tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "undeclared-accompaniment.xml"
    source.write_text(UNDECLARED_ACCOMPANIMENT_XML, encoding="utf-8")
    return source, parse_score(source)["score_summary"]


def _instruments_by_id(summary: dict) -> dict[str, dict]:
    return {
        instrument["score_instrument_id"]: instrument
        for part in summary["parts"]
        for instrument in part.get("instruments", [])
    }


def test_parts_without_an_instrument_or_lyrics_await_the_llm_role_decision(tmp_path: Path) -> None:
    """Only the LLM can tell a Piano from a Tenor written without lyrics."""
    _source, summary = _undeclared_accompaniment(tmp_path)
    resolution = summary["instrument_program_resolution"]
    assert resolution["instrumental_score_instrument_ids"] == []
    assert resolution["unresolved_score_instrument_ids"] == ["P2:default", "P3:default"]
    assert resolution["unresolved_reasons"] == {
        "P2:default": "undeclared_instrument_role",
        "P3:default": "undeclared_instrument_role",
    }
    assert score_has_instrumental_parts(summary) is False
    # A sung part is never a candidate.
    assert "P1:default" not in resolution["unresolved_score_instrument_ids"]


def test_llm_role_decision_makes_the_piano_a_midi_track_and_keeps_the_tenor_singable(
    tmp_path: Path,
) -> None:
    source, summary = _undeclared_accompaniment(tmp_path)
    resolved, action = apply_llm_program_assignments(
        summary,
        [
            {
                "score_instrument_id": "P3:default",
                "playback_preset": fluidr3_preset(program=0),
                "source": "llm_inferred",
                "evidence": ["part-name: Piano"],
            },
            {
                "score_instrument_id": "P2:default",
                "role": "not_instrumental",
                "source": "llm_inferred",
                "evidence": ["part-name: Tenor"],
            },
        ],
    )
    assert action is None
    resolution = resolved["instrument_program_resolution"]
    assert resolution["unresolved_score_instrument_ids"] == []
    assert resolution["instrumental_score_instrument_ids"] == ["P3:default"]
    assert score_has_instrumental_parts(resolved) is True
    instruments = _instruments_by_id(resolved)
    assert instruments["P3:default"]["eligible_for_instrumental_midi"] is True
    assert instruments["P3:default"]["program_source"] == "llm_inferred"
    assert instruments["P2:default"]["eligible_for_instrumental_midi"] is False
    assert instruments["P2:default"]["instrumental_role"] == "not_instrumental"

    result = build_instrumental_performance_midis(
        source,
        original_output_path=tmp_path / "written.mid",
        expanded_output_path=tmp_path / "expanded.mid",
        instrument_program_assignments=llm_program_assignments_from_summary(resolved),
    )
    eligible = {part["raw_part_id"]: part["eligible"] for part in result["instrumental_parts"]}
    assert eligible["P3"] is True
    assert eligible.get("P2") is not True
    assert result["has_instrumental_parts"] is True
    assert (tmp_path / "written.mid").is_file()


def test_an_undeclared_part_the_llm_calls_vocal_gets_no_midi_or_charge(tmp_path: Path) -> None:
    source, summary = _undeclared_accompaniment(tmp_path)
    resolved, action = apply_llm_program_assignments(
        summary,
        [
            {"score_instrument_id": instrument_id, "role": "not_instrumental", "source": "llm_inferred"}
            for instrument_id in ("P2:default", "P3:default")
        ],
    )
    assert action is None
    assert resolved["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
    assert score_has_instrumental_parts(resolved) is False
    result = build_instrumental_performance_midis(
        source,
        original_output_path=tmp_path / "written.mid",
        expanded_output_path=tmp_path / "expanded.mid",
        instrument_program_assignments=llm_program_assignments_from_summary(resolved),
    )
    assert result["has_instrumental_parts"] is False


def test_an_undeclared_part_left_unanswered_asks_for_a_preset_or_role(tmp_path: Path) -> None:
    _source, summary = _undeclared_accompaniment(tmp_path)
    _unchanged, action = apply_llm_program_assignments(summary, None)
    assert action is not None
    assert action["unresolved_score_instrument_ids"] == ["P2:default", "P3:default"]
    assert "role not_instrumental" in action["message"]


def test_a_declared_instrument_cannot_be_marked_not_instrumental() -> None:
    with TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "missing-program.musicxml"
        source.write_text(MISSING_PROGRAM_XML, encoding="utf-8")
        summary = parse_score(source)["score_summary"]
    _unchanged, action = apply_llm_program_assignments(
        summary,
        [{"score_instrument_id": "P1-I1", "role": "not_instrumental", "source": "llm_inferred"}],
    )
    assert action is not None
    assert "needs a playback_preset" in action["message"]


HAPPY_BIRTHDAY_DEMO = Path(__file__).resolve().parents[1] / "ui" / "public" / "demo-scores" / "happy-birthday.xml"


def _piano_assignment(instrument_id: str = "P4-I1") -> dict[str, object]:
    return {
        "score_instrument_id": instrument_id,
        "playback_preset": fluidr3_preset(program=0),
        "source": "llm_inferred",
        "evidence": ["instrument-name: Piano"],
    }


def test_an_assignment_for_an_instrument_that_already_has_a_program_is_dropped() -> None:
    summary = parse_score(HAPPY_BIRTHDAY_DEMO)["score_summary"]
    assert summary["instrument_program_resolution"]["unresolved_score_instrument_ids"] == []

    kept, dropped = drop_resolved_program_assignments(summary, [_piano_assignment()])
    assert (kept, dropped) == (None, ["P4-I1"])
    # What is left passes: the quote is not rejected for a redundant assignment.
    assert apply_llm_program_assignments(summary, kept)[1] is None

    # An instrument the score lacks is kept, so the call is still rejected.
    unknown = _piano_assignment("P9-I1")
    kept, dropped = drop_resolved_program_assignments(summary, [_piano_assignment(), unknown])
    assert (kept, dropped) == ([unknown], ["P4-I1"])
    assert apply_llm_program_assignments(summary, kept)[1] is not None


def test_only_the_resolved_assignments_are_dropped_when_others_are_awaited() -> None:
    summary = parse_score(HAPPY_BIRTHDAY_DEMO)["score_summary"]
    summary["instrument_program_resolution"]["unresolved_score_instrument_ids"] = ["P4-I1"]
    awaited = _piano_assignment()
    kept, dropped = drop_resolved_program_assignments(summary, [_piano_assignment("P2-I1"), awaited])
    assert (kept, dropped) == ([awaited], ["P2-I1"])

    # Nothing to drop: the assignments come back as they were.
    assert drop_resolved_program_assignments(summary, [awaited]) == ([awaited], [])
    assert drop_resolved_program_assignments(summary, None) == (None, [])
