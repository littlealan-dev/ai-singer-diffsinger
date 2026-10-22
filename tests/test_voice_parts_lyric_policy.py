import unittest

from src.api.voice_parts import _should_apply_policy


class VoicePartLyricPolicyTests(unittest.TestCase):
    def test_policy_for_each_lyric_state(self) -> None:
        policies = (
            "replace_all",
            "fill_missing_only",
            "preserve_existing",
            "something_else",
        )
        cases = (
            ("missing key", {}, (True, True, True, True)),
            ("None", {"lyric": None}, (True, True, True, True)),
            ('""', {"lyric": ""}, (True, True, True, True)),
            ('"   "', {"lyric": "   "}, (True, True, True, True)),
            ('"la"', {"lyric": "la"}, (True, False, False, False)),
            ('"+"', {"lyric": "+"}, (True, False, False, False)),
            (
                '"la" with lyric_is_extended=True',
                {"lyric": "la", "lyric_is_extended": True},
                (True, False, False, False),
            ),
        )

        for lyric_value, note, expected_results in cases:
            for policy, expected in zip(policies, expected_results):
                with self.subTest(lyric=lyric_value, policy=policy):
                    self.assertIs(_should_apply_policy(note, policy), expected)
