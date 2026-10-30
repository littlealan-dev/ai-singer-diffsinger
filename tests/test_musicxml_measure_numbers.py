from pathlib import Path

from src.api.score import parse_score
from src.api.voice_parts import _normalize_timeline_sections, _resolve_part_measure_span


FIXTURE = Path(__file__).parent / "fixtures" / "all_zero_measure_numbers.musicxml"


def _renumber_or_identity(xml_text: str) -> tuple[str, int]:
    # The guard cases also run against origin/main, where the helper is absent.
    try:
        from src.musicxml.measure_numbers import renumber_unnumbered_measures
    except ModuleNotFoundError:
        return xml_text, 0
    return renumber_unnumbered_measures(xml_text)


def test_all_zero_fixture_renumbers_only_measure_values():
    from src.musicxml.measure_numbers import renumber_unnumbered_measures

    original = FIXTURE.read_text(encoding="utf-8")
    updated, count = renumber_unnumbered_measures(original)
    expected = original
    for number in (1, 2, 3, 1, 2):
        expected = expected.replace('<measure number="0">', f'<measure number="{number}">', 1)

    assert count == 5
    assert updated == expected
    assert "<measure-style><multiple-rest>1</multiple-rest></measure-style>" in updated
    assert "<measure-numbering>measure</measure-numbering>" in updated


def test_missing_negative_and_single_quoted_numbers_are_renumbered():
    from src.musicxml.measure_numbers import renumber_unnumbered_measures

    original = (
        '<score-partwise><part id="P1"><measure implicit="no"></measure>'
        '<measure number="-1"></measure><measure number=\'0\'></measure>'
        '</part><part id="P2"><measure number=""></measure></part></score-partwise>'
    )
    expected = (
        '<score-partwise><part id="P1"><measure number="1" implicit="no"></measure>'
        '<measure number="2"></measure><measure number=\'3\'></measure>'
        '</part><part id="P2"><measure number="1"></measure></part></score-partwise>'
    )
    assert renumber_unnumbered_measures(original) == (expected, 4)


def test_renumbered_score_has_addressable_section_range(tmp_path):
    from src.musicxml.measure_numbers import renumber_unnumbered_measures

    original = FIXTURE.read_text(encoding="utf-8")
    source = tmp_path / "score.xml"
    source.write_text(original, encoding="utf-8")
    old_score = parse_score(source)
    section = {"start_measure": 1, "end_measure": 3, "mode": "rest"}
    _, old_error = _normalize_timeline_sections(
        [section], target_ref={"part_index": 0}, score=old_score, field_name="sections"
    )
    assert old_error is not None and old_error["code"] == "invalid_section_range"

    updated, count = renumber_unnumbered_measures(original)
    assert count == 5
    source.write_text(updated, encoding="utf-8")
    score = parse_score(source)
    assert all(note["measure_number"] >= 1 for part in score["parts"] for note in part["notes"])
    assert _resolve_part_measure_span(score, 0) == (1, 3)
    _, error = _normalize_timeline_sections(
        [section], target_ref={"part_index": 0}, score=score, field_name="sections"
    )
    assert error is None


def test_ordinary_and_unsupported_scores_are_unchanged():
    cases = [
        '<score-partwise><part><measure number="0"/><measure number="1"/><measure number="2"/></part></score-partwise>',
        '<score-partwise><part><measure number="X1"/><measure number="X1"/></part></score-partwise>',
        '<score-timewise><measure number="0"/></score-timewise>',
        '<score-partwise><part/></score-partwise>',
    ]
    for original in cases:
        assert _renumber_or_identity(original) == (original, 0)


def test_non_utf8_file_is_unchanged(tmp_path):
    import src.backend.main as backend_main

    path = tmp_path / "score.xml"
    original = b'<score-partwise><part><measure number="0"/></part>\xe9</score-partwise>'
    path.write_bytes(original)
    renumber_file = getattr(backend_main, "_renumber_unnumbered_measures_in_file", lambda _: 0)
    assert renumber_file(path) == 0
    assert path.read_bytes() == original
