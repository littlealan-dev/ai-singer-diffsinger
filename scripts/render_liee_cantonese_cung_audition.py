#!/usr/bin/env python3
"""Render a short, deterministic LIEE Cantonese ``cung`` articulation audition."""

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
FIXTURE = ROOT / "tests" / "fixtures" / "liee_cantonese_cung_articulation.musicxml"
OUTPUT_DIR = ROOT / "tests" / "output" / "liee_language_auditions"


def _fake_synthesize_request() -> dict[str, object]:
    client = StaticLlmClient(
        response_text=json.dumps(
            {
                "tool_calls": [{"name": "synthesize", "arguments": {
                    "voicebank": VOICEBANK_ID,
                    "part_index": 0,
                    "lyric_selection": {"number": "1"},
                    "language": "zh-yue",
                    "pitch_expression": 0.75,
                }}],
                "final_message": "Rendering the Cantonese cung articulation audition.",
                "include_score": False,
            }
        )
    )
    payload = parse_llm_response(client.generate("", []))
    if payload is None or len(payload.tool_calls) != 1:
        raise RuntimeError("Static LLM did not return exactly one tool call.")
    call = payload.tool_calls[0]
    if call.name != "synthesize" or call.arguments.get("language") != "zh-yue":
        raise RuntimeError(f"Static LLM returned an invalid request: {call}")
    return dict(call.arguments)


def main() -> None:
    request = _fake_synthesize_request()
    score = parse_score(FIXTURE, verse_number=1)
    result = synthesize(
        score,
        VOICEBANK,
        part_index=int(request["part_index"]),
        language=str(request["language"]),
        pitch_expression=float(request["pitch_expression"]),
    )
    if result.get("status") == "action_required":
        raise RuntimeError(f"Unexpected action required: {result}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = OUTPUT_DIR / "liee_zh-yue_cung_articulation.wav"
    saved = save_audio(result["waveform"], audio_path, sample_rate=result["sample_rate"])
    metadata = {
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "audio": str(audio_path.relative_to(ROOT)),
        "duration_seconds": saved["duration_seconds"],
        "words": ["充 (cung1)", "蟲 (cung4)", "重 (cung5)"],
        "expected_phones": ["ch", "u", "ng"],
        "fake_llm_request": request,
    }
    (OUTPUT_DIR / "liee_zh-yue_cung_articulation.request.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    )
    print(audio_path)


if __name__ == "__main__":
    main()
