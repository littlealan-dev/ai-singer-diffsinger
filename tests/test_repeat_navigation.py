"""MusicXML repeat/navigation fixtures used by synthesis-only expansion tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree

from src.api.score import expanded_score_for_synthesis, parse_score
from src.mcp import handlers
from src.mcp.tools import list_tools

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "repeat_navigation"
FIXTURE_FILENAMES = {
    "da_capo_al_coda": "da_capo_al_coda.xml",
    "dal_segno_al_coda": "dal_segno_al_coda.xml",
}


def _part_pitch_steps(score: dict) -> str:
    """Return the non-rest pitch steps from the first parsed score part."""
    return "".join(
        str(note_event["pitch_step"])
        for note_event in score["parts"][0]["notes"]
        if not note_event["is_rest"]
    )


class RepeatNavigationParsingTests(unittest.TestCase):
    """Short MusicXML scores for every repeat/navigation form supported in V1."""

    CASES = {
        "forward_repeat": "CDCDEFG",
        "volta_endings": "CDC EFG".replace(" ", ""),
        "da_capo": "CDEFCDEFG",
        "da_capo_al_fine": "CDE FCD".replace(" ", ""),
        "da_capo_al_coda": "CDEFCDEGA",
        "dal_segno": "CDEFDEFG",
        "dal_segno_al_fine": "CDEFDE",
        "dal_segno_al_coda": "CDEFGEFAB",
    }

    def _parse_case(self, label: str) -> tuple[dict, dict]:
        source_path = FIXTURE_DIRECTORY / FIXTURE_FILENAMES.get(label, f"{label}.musicxml")
        display_score = parse_score(source_path, expand_repeats=False)
        expanded_score = expanded_score_for_synthesis(display_score)
        return display_score, expanded_score

    def test_expands_each_supported_navigation_form(self) -> None:
        for label, expected_pitches in self.CASES.items():
            with self.subTest(label=label):
                _display_score, expanded_score = self._parse_case(label)
                pitches = _part_pitch_steps(expanded_score)
                self.assertEqual(pitches, expected_pitches)

    def test_navigation_directions_precede_their_measure_notes(self) -> None:
        """Keep notation symbols over the intended measure, not the next one."""
        navigation_words = ("da capo", "dal segno", "fine")
        fixture_paths = sorted(
            [*FIXTURE_DIRECTORY.glob("*.musicxml"), *FIXTURE_DIRECTORY.glob("*.xml")]
        )
        for fixture_path in fixture_paths:
            root = ElementTree.parse(fixture_path).getroot()
            for measure in root.iter("measure"):
                children = list(measure)
                first_note_index = next(
                    (
                        index
                        for index, child in enumerate(children)
                        if child.tag == "note"
                    ),
                    None,
                )
                if first_note_index is None:
                    continue
                for index, child in enumerate(children):
                    if child.tag != "direction":
                        continue
                    navigation_text = " ".join(
                        text.strip().lower()
                        for text in child.itertext()
                        if text and text.strip()
                    )
                    has_navigation_symbol = any(
                        element.tag in {"segno", "coda"} for element in child.iter()
                    )
                    if has_navigation_symbol or any(
                        token in navigation_text for token in navigation_words
                    ):
                        with self.subTest(
                            fixture=fixture_path.name,
                            measure=measure.attrib.get("number"),
                            direction=navigation_text,
                        ):
                            self.assertLess(index, first_note_index)

    def test_expansion_does_not_change_the_display_score(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "repeat-case.musicxml"
            fixture_path = FIXTURE_DIRECTORY / "volta_endings.musicxml"
            source_path.write_bytes(fixture_path.read_bytes())
            display_score = parse_score(source_path, expand_repeats=False)
            original_snapshot = deepcopy(display_score)
            source_path.unlink()
            expanded_score = expanded_score_for_synthesis(display_score)

        display_pitches = _part_pitch_steps(display_score)
        expanded_pitches = _part_pitch_steps(expanded_score)

        self.assertEqual(display_pitches, "CDEFG")
        self.assertEqual(expanded_pitches, "CDCEFG")
        self.assertEqual(display_score, original_snapshot)
        self.assertIs(expanded_score, display_score["expanded_score"])

    def test_duration_estimation_selects_written_or_played_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "repeat-case.musicxml"
            fixture_path = FIXTURE_DIRECTORY / "forward_repeat.musicxml"
            source_path.write_bytes(fixture_path.read_bytes())
            display_score = parse_score(source_path, expand_repeats=False)

        self.assertEqual(_part_pitch_steps(display_score), "CDEFG")
        self.assertEqual(_part_pitch_steps(display_score["expanded_score"]), "CDCDEFG")
        self.assertEqual(handlers._calculate_score_duration(display_score), 3.5)
        self.assertEqual(
            handlers._calculate_score_duration(display_score, expand_repeats=False), 2.5
        )

    def test_synthesize_schema_defaults_repeat_expansion_to_true(self) -> None:
        synthesize_tool = next(
            tool for tool in list_tools() if tool["name"] == "synthesize"
        )
        setting = synthesize_tool["inputSchema"]["properties"]["expand_repeats"]
        self.assertEqual(setting["type"], "boolean")
        self.assertIs(setting["default"], True)

    def test_mcp_synthesize_forwards_default_and_explicit_repeat_setting(self) -> None:
        score = {
            "parts": [{"part_id": "P1", "notes": []}],
            "selected_lyric_selection": {"id": "line-1", "number": "1", "name": "Verse"},
        }
        base_params = {
            "score": score,
            "voicebank": "TestBank",
            "part_id": "P1",
            "language": "en",
            "lyric_selection": score["selected_lyric_selection"],
        }
        with mock.patch.object(
            handlers, "get_manifest_voicebank_metadata", return_value={}
        ), mock.patch.object(
            handlers,
            "resolve_manifest_synthesis_control_defaults",
            return_value={"airiness": 0.0, "clarity": 100.0, "gender": 0.0},
        ), mock.patch.object(
            handlers, "resolve_voicebank_id", return_value=Path("/tmp/TestBank")
        ), mock.patch.object(
            handlers,
            "synthesize",
            return_value={"waveform": [0.0], "sample_rate": 44100},
        ) as synthesize_mock:
            handlers.handle_synthesize(dict(base_params), device="cpu")
            self.assertIs(synthesize_mock.call_args.kwargs["expand_repeats"], True)

            handlers.handle_synthesize(
                {**base_params, "expand_repeats": False}, device="cpu"
            )
            self.assertIs(synthesize_mock.call_args.kwargs["expand_repeats"], False)


if __name__ == "__main__":
    unittest.main()
