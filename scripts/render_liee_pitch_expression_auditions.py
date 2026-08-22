#!/usr/bin/env python3
"""Render one LIEE solfege warm-up at three pitch-expression settings.

Each take begins as a deterministic ``synthesize`` tool call from the fake
LLM, then runs through SightSinger's real synthesis API.  ``requests.json``
records the exact settings used for reproducible auditioning.
"""

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
VOICEBANK = ROOT / "assets/voicebanks" / VOICEBANK_ID
FIXTURE = ROOT / "tests/fixtures/liee_pitch_expression_warmup.musicxml"
OUTPUT_DIR = ROOT / "tests/output/liee_pitch_expression_auditions"
EXPRESSIONS = (1.0, 0.5, 0.3)


def _fake_synthesize_request(pitch_expression: float) -> dict[str, object]:
    """Return and validate a deterministic fake-LLM synthesis tool call."""
    client = StaticLlmClient(
        response_text=json.dumps(
            {
                "tool_calls": [
                    {
                        "name": "synthesize",
                        "arguments": {
                            "voicebank": VOICEBANK_ID,
                            "part_index": 0,
                            "lyric_selection": {"number": "1"},
                            "language": "en",
                            "pitch_expression": pitch_expression,
                            "solfege_pronunciation_patch": True,
                            "require_solfege_lyrics": True,
                        },
                    }
                ],
                "final_message": "Rendering the C-major warm-up.",
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
        or call.arguments.get("voicebank") != VOICEBANK_ID
        or call.arguments.get("language") != "en"
        or call.arguments.get("pitch_expression") != pitch_expression
    ):
        raise RuntimeError(f"Static LLM returned an invalid request: {call}")
    return dict(call.arguments)


def main() -> None:
    if not VOICEBANK.is_dir():
        raise FileNotFoundError(f"LIEE voicebank not found: {VOICEBANK}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    score = parse_score(FIXTURE, verse_number=1)
    requests: list[dict[str, object]] = []

    for expression in EXPRESSIONS:
        request = _fake_synthesize_request(expression)
        result = synthesize(
            score,
            VOICEBANK,
            part_index=int(request["part_index"]),
            language=str(request["language"]),
            pitch_expression=float(request["pitch_expression"]),
            solfege_pronunciation_patch=bool(request["solfege_pronunciation_patch"]),
            require_solfege_lyrics=bool(request["require_solfege_lyrics"]),
        )
        if result.get("status") == "action_required":
            raise RuntimeError(f"Unexpected action required: {result}")
        filename = f"liee_warmup_pitch_expression_{expression:.1f}.wav"
        audio_path = OUTPUT_DIR / filename
        saved = save_audio(
            result["waveform"], audio_path, sample_rate=result["sample_rate"]
        )
        requests.append(
            {
                "fixture": str(FIXTURE.relative_to(ROOT)),
                "audio": str(audio_path.relative_to(ROOT)),
                "duration_seconds": saved["duration_seconds"],
                "fake_llm_request": request,
            }
        )
        print(f"Rendered pitch_expression={expression:.1f}: {audio_path}")

    (OUTPUT_DIR / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")


if __name__ == "__main__":
    main()
