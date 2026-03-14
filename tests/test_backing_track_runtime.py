from __future__ import annotations

from pathlib import Path
import numpy as np
import soundfile as sf

from src.backend.backing_track import (
    generate_backing_track,
    generate_combined_backing_track,
    write_backing_track_prompt,
)
from src.backend.config import Settings
from src.backend.llm_client import StaticLlmClient
from src.backend.llm_prompt import PromptBundle


def _settings() -> Settings:
    return Settings.from_env()


def _metadata(*, has_pickup: bool = True) -> dict:
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
            "has_pickup": has_pickup,
            "starts_on_beat": 4 if has_pickup else None,
            "second_bar_downbeat_note": "E4" if has_pickup else None,
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
    captured: dict = {}

    monkeypatch.setattr(
        "src.backend.backing_track.extract_backing_track_metadata",
        lambda file_path: _metadata(has_pickup=False),
    )

    def _fake_compose(**kwargs):
        captured["output_format"] = kwargs["output_format"]
        waveform = (np.ones((4410, 2), dtype=np.float32) * 0.25 * 32767.0).astype("<i2")
        return [waveform.tobytes()]

    monkeypatch.setattr("src.backend.backing_track._compose_with_elevenlabs", _fake_compose)

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

    assert captured["output_format"] == "pcm_44100"
    assert result.output_path.exists()
    assert result.output_path.read_bytes().startswith(b"ID3")
    assert result.wav_output_path is not None
    assert result.wav_output_path.exists()
    assert result.prompt == "Create an original instrumental jazz trio backing track."
    assert 0.09 <= result.duration_seconds <= 0.11


def test_generate_backing_track_prepends_measure_silence_for_pickup(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "score.xml"
    source.write_text("<score-partwise version='4.0'/>", encoding="utf-8")
    captured: dict = {}

    monkeypatch.setattr(
        "src.backend.backing_track.extract_backing_track_metadata",
        lambda file_path: _metadata(has_pickup=True),
    )

    def _fake_compose(**kwargs):
        captured["output_format"] = kwargs["output_format"]
        waveform = (np.ones((4410, 2), dtype=np.float32) * 0.25 * 32767.0).astype("<i2")
        return [waveform.tobytes()]

    monkeypatch.setattr(
        "src.backend.backing_track._compose_with_elevenlabs",
        _fake_compose,
    )

    result = generate_backing_track(
        file_path=source,
        output_path=tmp_path / "backing_track.mp3",
        style_request="simple pop rock",
        additional_requirements=None,
        settings=_settings(),
        llm_client=StaticLlmClient(
            response_text='{"prompt":"Create an original instrumental pop rock backing track."}'
        ),
    )

    assert captured["output_format"] == "pcm_44100"
    assert result.output_path.exists()
    assert result.output_path.suffix == ".mp3"
    assert result.output_path.read_bytes().startswith(b"ID3")
    assert result.wav_output_path is not None
    assert result.wav_output_path.exists()
    assert 2.05 <= result.duration_seconds <= 2.15


def test_generate_combined_backing_track_reuses_existing_backing_wav(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "score.xml"
    source.write_text("<score-partwise version='4.0'/>", encoding="utf-8")
    singing_path = tmp_path / "singing.wav"
    backing_wav_path = tmp_path / "existing_backing.wav"
    backing_mp3_path = tmp_path / "existing_backing.mp3"
    sf.write(str(singing_path), np.ones((2205, 2), dtype=np.float32) * 0.5, 44100)
    sf.write(str(backing_wav_path), np.ones((2205, 2), dtype=np.float32) * 0.25, 44100)
    backing_mp3_path.write_bytes(b"ID3EXISTING")

    monkeypatch.setattr(
        "src.backend.backing_track.extract_backing_track_metadata",
        lambda file_path: _metadata(has_pickup=False),
    )
    monkeypatch.setattr(
        "src.backend.backing_track._compose_with_elevenlabs",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("compose should not be called")),
    )

    result = generate_combined_backing_track(
        file_path=source,
        output_path=tmp_path / "combined.mp3",
        singing_audio_path=singing_path,
        existing_backing_output_path=backing_mp3_path,
        existing_backing_wav_path=backing_wav_path,
        style_request="simple pop rock",
        additional_requirements=None,
        settings=_settings(),
        llm_client=StaticLlmClient(response_text='{"prompt":"unused"}'),
    )

    assert result.reused_existing_backing_track is True
    assert result.output_path.exists()
    assert result.output_path.read_bytes().startswith(b"ID3")
    assert result.backing_track_wav_output_path == backing_wav_path.resolve()


def test_generate_combined_backing_track_generates_backing_when_missing(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "score.xml"
    source.write_text("<score-partwise version='4.0'/>", encoding="utf-8")
    singing_path = tmp_path / "singing.wav"
    sf.write(str(singing_path), np.ones((2205, 2), dtype=np.float32) * 0.5, 44100)
    captured: dict = {}

    monkeypatch.setattr(
        "src.backend.backing_track.extract_backing_track_metadata",
        lambda file_path: _metadata(has_pickup=False),
    )

    def _fake_compose(**kwargs):
        captured["output_format"] = kwargs["output_format"]
        waveform = (np.ones((2205, 2), dtype=np.float32) * 0.25 * 32767.0).astype("<i2")
        return [waveform.tobytes()]

    monkeypatch.setattr("src.backend.backing_track._compose_with_elevenlabs", _fake_compose)

    result = generate_combined_backing_track(
        file_path=source,
        output_path=tmp_path / "combined.mp3",
        singing_audio_path=singing_path,
        style_request="simple pop rock",
        additional_requirements=None,
        settings=_settings(),
        llm_client=StaticLlmClient(
            response_text='{"prompt":"Create an original instrumental pop rock backing track."}'
        ),
    )

    assert captured["output_format"] == "pcm_44100"
    assert result.reused_existing_backing_track is False
    assert result.output_path.exists()
    assert result.output_path.read_bytes().startswith(b"ID3")
    assert result.backing_track_output_path.exists()
    assert result.backing_track_wav_output_path.exists()
