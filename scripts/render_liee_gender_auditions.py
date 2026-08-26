#!/usr/bin/env python3
"""Render a controlled LIEE warm-up at three GENC settings."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import parse_score, save_audio, synthesize
from src.backend.llm_client import StaticLlmClient
from src.backend.llm_prompt import parse_llm_response


VOICEBANK_ID = "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"
VOICEBANK = ROOT / "assets" / "voicebanks" / VOICEBANK_ID
FIXTURE = ROOT / "tests" / "fixtures" / "liee_tension_warmup.xml"
OUTPUT_DIR = ROOT / "tests" / "output" / "liee_gender_auditions"
GENDERS = (0.0, -25.0, -50.0)
TENSION = 0.75


def _fake_synthesize_request(gender: float) -> dict[str, object]:
    """Return and validate one deterministic fake-LLM synthesis call."""
    client = StaticLlmClient(
        response_text=json.dumps(
            {
                "tool_calls": [{"name": "synthesize", "arguments": {
                    "voicebank": VOICEBANK_ID,
                    "part_index": 0,
                    "lyric_selection": {"number": "1"},
                    "language": "en",
                    "intensity": TENSION,
                    "gender": gender,
                    "pitch_expression": 0.75,
                    "solfege_pronunciation_patch": True,
                    "require_solfege_lyrics": True,
                }}],
                "final_message": "Rendering the LIEE gender warm-up.",
                "include_score": False,
            }
        )
    )
    payload = parse_llm_response(client.generate("", []))
    if payload is None or len(payload.tool_calls) != 1:
        raise RuntimeError("Static LLM did not return exactly one tool call.")
    call = payload.tool_calls[0]
    if (
        call.name != "synthesize"
        or call.arguments.get("gender") != gender
        or call.arguments.get("intensity") != TENSION
    ):
        raise RuntimeError(f"Static LLM returned an invalid request: {call}")
    return dict(call.arguments)


def main() -> None:
    if not VOICEBANK.is_dir():
        raise FileNotFoundError(f"LIEE voicebank not found: {VOICEBANK}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    score = parse_score(FIXTURE, verse_number=1)
    requests: list[dict[str, object]] = []
    for gender in GENDERS:
        request = _fake_synthesize_request(gender)
        result = synthesize(
            score, VOICEBANK, part_index=int(request["part_index"]),
            language=str(request["language"]), intensity=float(request["intensity"]),
            gender=float(request["gender"]),
            pitch_expression=float(request["pitch_expression"]),
            solfege_pronunciation_patch=bool(request["solfege_pronunciation_patch"]),
            require_solfege_lyrics=bool(request["require_solfege_lyrics"]),
        )
        if result.get("status") == "action_required":
            raise RuntimeError(f"Unexpected action required: {result}")
        audio_path = OUTPUT_DIR / f"liee_warmup_gender_{gender:+.0f}.wav"
        saved = save_audio(result["waveform"], audio_path, sample_rate=result["sample_rate"])
        requests.append({
            "fixture": str(FIXTURE.relative_to(ROOT)),
            "audio": str(audio_path.relative_to(ROOT)),
            "duration_seconds": saved["duration_seconds"],
            "fake_llm_request": request,
        })
        print(f"Rendered gender={gender:+.0f}: {audio_path}")
    (OUTPUT_DIR / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")


if __name__ == "__main__":
    main()
