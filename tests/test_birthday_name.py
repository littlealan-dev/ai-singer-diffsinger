"""Writing a name into the Happy Birthday demo's placeholder."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.birthday_name import replace_birthday_name
from src.api.score import parse_score
from src.musicxml.birthday_name import NamePlaceholderError, write_birthday_name

DEMO = Path(__file__).resolve().parents[1] / "ui" / "public" / "demo-scores" / "happy-birthday.xml"
VOICEBANK_ID = "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"


def _measure_7(path: Path, part_name: str) -> list[tuple]:
    """(midi, beats, lyric, syllabic) for each sung note in measure 7, up to "hap"."""
    parts = {part["part_name"]: part for part in parse_score(path)["parts"]}
    notes = [
        note for note in parts[part_name]["notes"]
        if str(note.get("measure_number")) == "7" and not note.get("is_rest")
    ]
    rows = []
    for note in notes:
        if note.get("lyric") == "hap":
            break
        rows.append((note["pitch_midi"], round(note["duration_beats"], 3), note.get("lyric"), note.get("syllabic")))
    return rows


@pytest.mark.parametrize(
    ("words", "alto", "men"),
    [
        (
            [["Tom"]],
            [(64.0, 1.0, "Tom", "single"), (62.0, 1.0, "+", None)],
            [(48.0, 1.0, "Tom", "single"), (50.0, 1.0, "+", None)],
        ),
        (
            [["Hen", "ry"]],
            [(64.0, 1.0, "Hen", "begin"), (62.0, 1.0, "ry", "end")],
            [(48.0, 1.0, "Hen", "begin"), (50.0, 1.0, "ry", "end")],
        ),
        (
            [["Jo", "na", "than"]],
            [(64.0, 0.5, "Jo", "begin"), (64.0, 0.5, "na", "middle"), (62.0, 1.0, "than", "end")],
            [(48.0, 0.5, "Jo", "begin"), (48.0, 0.5, "na", "middle"), (50.0, 1.0, "than", "end")],
        ),
        (
            [["A", "le", "xan", "der"]],
            [(64.0, 0.333, "A", "begin"), (64.0, 0.333, "le", "middle"), (64.0, 0.333, "xan", "middle"),
             (62.0, 1.0, "der", "end")],
            [(48.0, 0.333, "A", "begin"), (48.0, 0.333, "le", "middle"), (48.0, 0.333, "xan", "middle"),
             (50.0, 1.0, "der", "end")],
        ),
        (
            [["An", "na"], ["Ma", "rie"]],
            [(64.0, 0.333, "An", "begin"), (64.0, 0.333, "na", "end"), (64.0, 0.333, "Ma", "begin"),
             (62.0, 1.0, "rie", "end")],
            [(48.0, 0.333, "An", "begin"), (48.0, 0.333, "na", "end"), (48.0, 0.333, "Ma", "begin"),
             (50.0, 1.0, "rie", "end")],
        ),
    ],
)
def test_each_syllable_count_has_its_rhythm_in_both_voices(tmp_path, words, alto, men) -> None:
    output = tmp_path / "named.xml"
    assert write_birthday_name(DEMO, output, words=words)["status"] == "ready"
    assert _measure_7(output, "Alto") == alto
    assert _measure_7(output, "Men") == men
    # Two beats are replaced by two beats: the song's length and the piano are unchanged.
    summary = parse_score(output)["score_summary"]
    original = parse_score(DEMO)["score_summary"]
    assert summary["duration_seconds"] == original["duration_seconds"]
    assert summary["timing_signature"] == original["timing_signature"]
    signatures = lambda s: {part["part_name"]: part["part_signature"] for part in s["parts"]}
    assert signatures(summary)["Piano"] == signatures(original)["Piano"]
    assert signatures(summary)["Alto"] != signatures(original)["Alto"]


def test_a_second_name_or_none_rebuilds_from_the_original_notes(tmp_path) -> None:
    long_name = tmp_path / "alexander.xml"
    write_birthday_name(DEMO, long_name, words=[["A", "le", "xan", "der"]])
    renamed = tmp_path / "tom.xml"
    write_birthday_name(long_name, renamed, words=[["Tom"]])
    assert _measure_7(renamed, "Alto") == [(64.0, 1.0, "Tom", "single"), (62.0, 1.0, "+", None)]

    restored = tmp_path / "restored.xml"
    assert write_birthday_name(long_name, restored, words=None)["status"] == "ready"
    assert _measure_7(restored, "Alto") == _measure_7(DEMO, "Alto")
    assert _measure_7(restored, "Men") == _measure_7(DEMO, "Men")


def test_writing_what_the_score_already_says_changes_nothing(tmp_path) -> None:
    assert write_birthday_name(DEMO, tmp_path / "same.xml", words=None) == {"status": "unchanged"}
    henry = tmp_path / "henry.xml"
    write_birthday_name(DEMO, henry, words=[["Hen", "ry"]])
    assert write_birthday_name(henry, tmp_path / "again.xml", words=[["Hen", "ry"]]) == {"status": "unchanged"}
    assert not (tmp_path / "again.xml").exists()


def test_only_the_untouched_demo_placeholder_can_be_replaced(tmp_path) -> None:
    text = DEMO.read_text(encoding="utf-8")
    not_demo = tmp_path / "not-demo.xml"
    not_demo.write_text(text.replace('name="sightsinger-demo"', 'name="other"'), encoding="utf-8")
    with pytest.raises(NamePlaceholderError):
        write_birthday_name(not_demo, tmp_path / "out.xml", words=[["Tom"]])

    # A user edit to measure 7 (the Alto's first note re-pitched) is refused, not overwritten.
    part = text.index('<part id="P2">')
    measure = text.index('<measure number="7"', part)
    step = text.index("<step>E</step>", measure)
    edited = tmp_path / "edited.xml"
    edited.write_text(text[:step] + "<step>F</step>" + text[step + len("<step>E</step>"):], encoding="utf-8")
    with pytest.raises(NamePlaceholderError):
        write_birthday_name(edited, tmp_path / "out.xml", words=[["Tom"]])


@pytest.fixture
def voicebank_path():
    from src.mcp.resolve import resolve_voicebank_id

    try:
        return resolve_voicebank_id(VOICEBANK_ID)
    except Exception:
        pytest.skip(f"Voicebank {VOICEBANK_ID} is not installed.")


@pytest.mark.parametrize(
    ("name", "sung_text", "status", "code", "expected"),
    [
        ("Henry", "Hen-ry", "name_ready", None, None),
        ("Jack", "Jack", "name_ready", None, None),
        ("Anna Marie", "An-na Ma-rie", "name_ready", None, None),
        ("Henry", "Henry", "action_required", "name_syllables_mismatch", 2),
        ("Henry", "Hen-ri", "action_required", "name_syllables_mismatch", None),
        ("Maximiliano", "Ma-xi-mi-li-a-no", "action_required", "name_too_long", None),
        ("李明", "李明", "action_required", "name_not_singable", None),
        ("123", "123", "action_required", "name_not_singable", None),
    ],
)
def test_names_are_checked_with_the_singing_voices_pronunciation(
    tmp_path, voicebank_path, name, sung_text, status, code, expected
) -> None:
    result = replace_birthday_name(
        DEMO, tmp_path / "out.xml", name=name, sung_text=sung_text, voicebank_path=voicebank_path
    )
    assert result["status"] == status
    assert result.get("code") == code
    if expected is not None:
        assert result["diagnostics"]["expected_syllables"] == expected
    if status == "name_ready":
        assert result["sung_text"] == sung_text
        assert result["score_summary"]["demo_song"] == "happy-birthday"


def test_restoring_the_placeholder_needs_no_validation(tmp_path, voicebank_path) -> None:
    henry = tmp_path / "henry.xml"
    replace_birthday_name(DEMO, henry, name="Henry", sung_text="Hen-ry", voicebank_path=voicebank_path)
    restored = replace_birthday_name(
        henry, tmp_path / "restored.xml", name=None, sung_text=None, voicebank_path=voicebank_path
    )
    assert restored["status"] == "name_ready" and restored["sung_text"] == "you"
    assert _measure_7(tmp_path / "restored.xml", "Alto") == _measure_7(DEMO, "Alto")
