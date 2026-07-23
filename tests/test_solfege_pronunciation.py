from pathlib import Path
from importlib import import_module
from unittest import mock

from src.api import syllable_alignment
from src.api.phonemize import patch_solfege_pronunciations
from src.api.synthesize import align_phonemes_to_notes
from src.mcp import handlers
from src.mcp.tools import list_tools

phonemize_api = import_module("src.api.phonemize")


class _FakePhonemeResult:
    def __init__(self, tokens) -> None:
        self.phonemes = list(tokens)
        self.ids = [1] * len(tokens)
        self.language_ids = [0] * len(tokens)
        self.word_boundaries = [1] * len(tokens)


class _CapturingPhonemizer:
    normalized_tokens = []
    needed_graphemes = set()

    @staticmethod
    def _normalize_grapheme(value: str) -> str:
        return value.strip().casefold()

    def __init__(self, *args, needed_graphemes=None, **kwargs) -> None:
        type(self).needed_graphemes = set(needed_graphemes or set())

    def phonemize_tokens(self, tokens):
        type(self).normalized_tokens.extend(tokens)
        return _FakePhonemeResult(tokens)


def test_solfege_patch_maps_whole_tokens_without_mutating_input():
    lyrics = ["do", "FI", "sol", "doing", "+", "doh"]

    patched = patch_solfege_pronunciations(lyrics)

    assert patched == ["doh", "fee", "soh", "doing", "+", "doh"]
    assert lyrics == ["do", "FI", "sol", "doing", "+", "doh"]


def test_alignment_rejects_non_boolean_patch_value():
    try:
        align_phonemes_to_notes(
            {"parts": []},
            Path("/tmp/TestBank"),
            solfege_pronunciation_patch="false",
        )
    except ValueError as exc:
        assert str(exc) == "solfege_pronunciation_patch must be a boolean."
    else:
        raise AssertionError("Expected non-boolean pronunciation patch to fail")


def test_phonemize_applies_patch_before_dictionary_loading(tmp_path, monkeypatch):
    _CapturingPhonemizer.normalized_tokens = []
    monkeypatch.setattr(phonemize_api, "Phonemizer", _CapturingPhonemizer)
    monkeypatch.setattr(
        phonemize_api,
        "load_voicebank_config",
        lambda _path: {"phonemes": "phonemes.json"},
    )
    monkeypatch.setattr(
        phonemize_api,
        "_find_dictionary",
        lambda _path, language="en": tmp_path / "dictionary.txt",
    )
    lyrics = ["do", "fi", "doing"]

    phonemize_api.phonemize(
        lyrics,
        tmp_path,
        solfege_pronunciation_patch=True,
    )

    assert _CapturingPhonemizer.normalized_tokens == ["doh", "fee", "doing"]
    assert _CapturingPhonemizer.needed_graphemes == {"doh", "fee", "doing"}
    assert lyrics == ["do", "fi", "doing"]


def test_phonemize_patch_is_english_only(tmp_path, monkeypatch):
    _CapturingPhonemizer.normalized_tokens = []
    monkeypatch.setattr(phonemize_api, "Phonemizer", _CapturingPhonemizer)
    monkeypatch.setattr(
        phonemize_api,
        "load_voicebank_config",
        lambda _path: {"phonemes": "phonemes.json"},
    )
    monkeypatch.setattr(
        phonemize_api,
        "_find_dictionary",
        lambda _path, language="en": tmp_path / "dictionary.txt",
    )

    phonemize_api.phonemize(
        ["do", "fi"],
        tmp_path,
        language="it",
        solfege_pronunciation_patch=True,
    )

    assert _CapturingPhonemizer.normalized_tokens == ["do", "fi"]


def test_v2_aligner_forwards_solfege_patch():
    phonemizer = mock.Mock()
    phonemizer._phoneme_to_id = {"SP": 0}
    phonemizer.is_vowel.side_effect = lambda phoneme: phoneme == "ow"
    phonemizer.is_glide.return_value = False
    phoneme_result = {
        "phonemes": ["d", "ow"],
        "phoneme_ids": [1, 2],
        "language_ids": [0, 0],
        "word_boundaries": [2],
    }
    note = {
        "is_rest": False,
        "lyric": "do",
        "syllabic": "single",
        "lyric_is_extended": False,
        "tie_type": None,
    }

    with mock.patch.object(
        syllable_alignment,
        "phonemize",
        return_value=phoneme_result,
    ) as phonemize_mock:
        syllable_alignment.align(
            notes=[note],
            start_frames=[0],
            end_frames=[10],
            timing_midi=[60.0],
            note_durations=[10],
            phonemizer=phonemizer,
            voicebank_path=Path("/tmp/TestBank"),
            solfege_pronunciation_patch=True,
        )

    assert phonemize_mock.call_args.kwargs["solfege_pronunciation_patch"] is True


def test_mcp_handler_forwards_solfege_patch(tmp_path):
    with mock.patch.object(
        handlers,
        "get_manifest_voicebank_metadata",
        return_value={},
    ), mock.patch.object(
        handlers,
        "resolve_voicebank_id",
        return_value=tmp_path,
    ), mock.patch.object(
        handlers,
        "synthesize",
        return_value={"waveform": [0.0], "sample_rate": 44100},
    ) as synthesize_mock:
        handlers.handle_synthesize(
            {
                    "score": {
                        "parts": [],
                        "selected_lyric_selection": {
                            "id": "lyr_test",
                            "number": "1",
                            "name": "",
                        },
                    },
                    "voicebank": "TestBank",
                    "lyric_selection": {"id": "lyr_test", "number": "1", "name": ""},
                "solfege_pronunciation_patch": True,
            },
            device="cpu",
        )

    assert synthesize_mock.call_args.kwargs["solfege_pronunciation_patch"] is True


def test_synthesize_mcp_schema_exposes_solfege_patch():
    tools = {tool["name"]: tool for tool in list_tools()}
    field = tools["synthesize"]["inputSchema"]["properties"][
        "solfege_pronunciation_patch"
    ]

    assert field["type"] == "boolean"
    assert field["default"] is False
    preprocess_field = tools["start_preprocess_voice_part_workflow"]["inputSchema"][
        "properties"
    ]["request"]["properties"]["solfege_pronunciation_patch"]
    assert preprocess_field["type"] == ["boolean", "null"]
