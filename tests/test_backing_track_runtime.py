from __future__ import annotations

from pathlib import Path

from src.backend.backing_track import generate_backing_track, write_backing_track_prompt
from src.backend.config import Settings
from src.backend.llm_client import StaticLlmClient
from src.backend.llm_prompt import PromptBundle


def _settings() -> Settings:
    return Settings.from_env()


def _metadata() -> dict:
    return {
        "title": "Happy Birthday to You - 4/4 - G key",
        "key_signature": "G major",
        "time_signature": "4/4",
        "beats_per_measure": 4,
        "beat_type": 4,
        "bpm": 120,
        "measure_count": 17,
        "duration_seconds": 34.0,
        "duration_mmss": "0:34",
        "pickup": {
            "has_pickup": True,
            "starts_on_beat": 4,
            "second_bar_downbeat_note": "E4",
        },
        "chord_progression": [
            {"measure": 2, "chords": ["G"]},
            {"measure": 3, "chords": ["D"]},
        ],
    }


def test_write_backing_track_prompt_uses_llm_json_output() -> None:
    prompt = write_backing_track_prompt(
        metadata=_metadata(),
        style_request="bossa nova",
        additional_requirements="soft nylon guitar",
        settings=_settings(),
        llm_client=StaticLlmClient(
            response_text='{"prompt":"Create an original instrumental bossa nova backing track."}'
        ),
    )
    assert prompt == "Create an original instrumental bossa nova backing track."


def test_write_backing_track_prompt_strips_pickup_and_adds_first_measure_chord() -> None:
    captured_bundle: PromptBundle | None = None

    class CapturingClient:
        def generate(self, prompt_bundle: PromptBundle | str, history):
            nonlocal captured_bundle
            assert isinstance(prompt_bundle, PromptBundle)
            captured_bundle = prompt_bundle
            return '{"prompt":"Create an original instrumental backing track."}'

    prompt = write_backing_track_prompt(
        metadata=_metadata(),
        style_request="rock",
        additional_requirements="",
        settings=_settings(),
        llm_client=CapturingClient(),
    )

    assert prompt == "Create an original instrumental backing track."
    assert captured_bundle is not None
    assert '"pickup"' not in captured_bundle.dynamic_prompt_text
    assert '"measure": 1' in captured_bundle.dynamic_prompt_text
    assert '"chords": [\n        "G"\n      ]' in captured_bundle.dynamic_prompt_text


def test_write_backing_track_prompt_raises_on_invalid_llm_response() -> None:
    try:
        write_backing_track_prompt(
            metadata=_metadata(),
            style_request="lo-fi",
            additional_requirements="warm electric piano",
            settings=_settings(),
            llm_client=StaticLlmClient(response_text="not json"),
        )
    except RuntimeError as exc:
        assert str(exc) == "Backing-track prompt writer returned an invalid response."
    else:
        raise AssertionError("Expected RuntimeError for invalid prompt-writer response.")


def test_generate_backing_track_writes_audio_file(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "score.xml"
    source.write_text("<score-partwise version='4.0'/>", encoding="utf-8")

    monkeypatch.setattr(
        "src.backend.backing_track.extract_backing_track_metadata",
        lambda file_path: _metadata(),
    )
    monkeypatch.setattr(
        "src.backend.backing_track._compose_with_elevenlabs",
        lambda **kwargs: [b"ID3", b"TESTDATA"],
    )

    result = generate_backing_track(
        file_path=source,
        output_path=tmp_path / "backing_track.mp3",
        style_request="jazz trio",
        additional_requirements=None,
        settings=_settings(),
        llm_client=StaticLlmClient(
            response_text='{"prompt":"Create an original instrumental jazz trio backing track."}'
        ),
    )

    assert result.output_path.exists()
    assert result.output_path.read_bytes().startswith(b"ID3")
    assert result.prompt == "Create an original instrumental jazz trio backing track."
    assert result.duration_seconds == 34.0
