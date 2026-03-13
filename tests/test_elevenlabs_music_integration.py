from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from src.api.backing_track_prompt import (
    build_elevenlabs_backing_track_prompt,
    extract_backing_track_metadata,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SCORE = PROJECT_ROOT / "assets/test_data/happy-birthday-to-you-44-key-c.mxl"
OUTPUT_ROOT = PROJECT_ROOT / "tests/output/elevenlabs_music"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def test_compose_happy_birthday_backing_track_to_mp3() -> None:
    if os.getenv("RUN_REAL_ELEVENLABS_TESTS") != "1":
        pytest.skip("Set RUN_REAL_ELEVENLABS_TESTS=1 to run the real ElevenLabs music test.")

    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        pytest.skip("ELEVENLABS_API_KEY is not set.")

    if not TEST_SCORE.exists():
        pytest.skip(f"Test score not found: {TEST_SCORE}")

    metadata = extract_backing_track_metadata(TEST_SCORE)
    prompt = build_elevenlabs_backing_track_prompt(TEST_SCORE)

    run_dir = OUTPUT_ROOT / f"{_utc_stamp()}_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = run_dir / "happy_birthday.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    output_path = run_dir / "happy_birthday_backing_track.mp3"
    client = ElevenLabs(api_key=api_key)

    audio_chunks = client.music.compose(
        prompt=prompt,
        music_length_ms=int(round(float(metadata["duration_seconds"]) * 1000.0)),
        model_id="music_v1",
        force_instrumental=True,
        output_format="mp3_44100_128",
    )

    try:
        with output_path.open("wb") as handle:
            for chunk in audio_chunks:
                handle.write(chunk)
    except ApiError as exc:
        detail = exc.body.get("detail") if isinstance(exc.body, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if exc.status_code == 402 and code == "paid_plan_required":
            pytest.skip("ElevenLabs Music API requires a paid plan for this account.")
        raise

    assert output_path.exists(), f"Expected output file at {output_path}"
    assert output_path.stat().st_size > 0, "Generated MP3 file is empty."

    header = output_path.read_bytes()[:3]
    assert header in {b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}, (
        f"Unexpected MP3 header bytes: {header!r}"
    )
