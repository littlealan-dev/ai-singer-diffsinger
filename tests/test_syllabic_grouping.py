from __future__ import annotations

import unittest

from src.api.synthesize import _group_notes, _resolve_group_lyric


class TestSyllabicGrouping(unittest.TestCase):
    def test_groups_begin_end_into_single_word(self) -> None:
        notes = [
            {"is_rest": False, "lyric": "voic", "syllabic": "begin", "lyric_is_extended": False, "tie_type": None},
            {"is_rest": False, "lyric": "es", "syllabic": "end", "lyric_is_extended": False, "tie_type": None},
        ]
        groups = _group_notes(notes)
        self.assertEqual(len(groups), 1)
        self.assertEqual(_resolve_group_lyric(groups[0]), "voices")

    def test_groups_begin_middle_end_into_single_word(self) -> None:
        notes = [
            {"is_rest": False, "lyric": "in", "syllabic": "begin", "lyric_is_extended": False, "tie_type": None},
            {"is_rest": False, "lyric": "ter", "syllabic": "middle", "lyric_is_extended": False, "tie_type": None},
            {"is_rest": False, "lyric": "na", "syllabic": "end", "lyric_is_extended": False, "tie_type": None},
        ]
        groups = _group_notes(notes)
        self.assertEqual(len(groups), 1)
        self.assertEqual(_resolve_group_lyric(groups[0]), "interna")

    def test_keeps_extension_as_same_group(self) -> None:
        notes = [
            {"is_rest": False, "lyric": "glo", "syllabic": "begin", "lyric_is_extended": False, "tie_type": None},
            {"is_rest": False, "lyric": "+", "syllabic": None, "lyric_is_extended": True, "tie_type": "continue"},
            {"is_rest": False, "lyric": "ry", "syllabic": "end", "lyric_is_extended": False, "tie_type": None},
        ]
        groups = _group_notes(notes)
        self.assertEqual(len(groups), 1)
        self.assertEqual(_resolve_group_lyric(groups[0]), "glory")


if __name__ == "__main__":
    unittest.main()
