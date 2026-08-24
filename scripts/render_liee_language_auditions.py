#!/usr/bin/env python3
"""Render short LIEE language auditions without a live LLM.

The static client provides the same structured ``synthesize`` call that the
backend accepts. The script then asserts the selected language before handing
the request to the real synthesis API, making each rendered WAV reproducible
and auditable.
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


VOICEBANK = ROOT / "assets/voicebanks/Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"
FIXTURES = (
    ("fr", "french_phoneme_coverage", ROOT / "tests/fixtures/liee_french_phoneme_coverage.musicxml"),
    ("it", "italian_phoneme_coverage", ROOT / "tests/fixtures/liee_italian_phoneme_coverage.musicxml"),
    ("it", "italian_cia_lexicon", ROOT / "tests/fixtures/liee_italian_cia_lexicon.musicxml"),
    ("pt", "portuguese_phoneme_coverage", ROOT / "tests/fixtures/liee_portuguese_phoneme_coverage.musicxml"),
    ("es", "spanish_phoneme_coverage", ROOT / "tests/fixtures/liee_spanish_phoneme_coverage.musicxml"),
)
OUTPUT_DIR = ROOT / "tests/output/liee_language_auditions"


def _fake_synthesize_request(language: str) -> dict[str, object]:
    """Return and validate one deterministic synthesis call from the fake LLM."""
    client = StaticLlmClient(
        response_text=json.dumps(
            {
                "tool_calls": [
                    {
                        "name": "synthesize",
                        "arguments": {
                            "part_index": 0,
                            "lyric_selection": {"number": "1"},
                            "language": language,
                        },
                    }
                ],
                "final_message": "Rendering the two-measure audition.",
                "include_score": False,
            }
        )
    )
    payload = parse_llm_response(client.generate("", []))
    if payload is None or len(payload.tool_calls) != 1:
        raise RuntimeError("Static LLM did not return exactly one tool call.")
    call = payload.tool_calls[0]
    if call.name != "synthesize" or call.arguments.get("language") != language:
        raise RuntimeError(f"Static LLM returned an invalid request: {call}")
    return dict(call.arguments)


def main() -> None:
    if not VOICEBANK.is_dir():
        raise FileNotFoundError(f"LIEE voicebank not found: {VOICEBANK}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    requests: list[dict[str, object]] = []
    for language, output_name, fixture in FIXTURES:
        request = _fake_synthesize_request(language)
        score = parse_score(fixture, verse_number=1)
        result = synthesize(
            score,
            VOICEBANK,
            part_index=int(request["part_index"]),
            language=str(request["language"]),
        )
        if result.get("status") == "action_required":
            raise RuntimeError(f"Unexpected action required: {result}")
        audio_path = OUTPUT_DIR / f"liee_{output_name}.wav"
        save_audio(result["waveform"], audio_path, sample_rate=result["sample_rate"])
        requests.append(
            {
                "fixture": str(fixture.relative_to(ROOT)),
                "audio": str(audio_path.relative_to(ROOT)),
                "duration_seconds": result["duration_seconds"],
                "fake_llm_request": request,
            }
        )
        print(f"Rendered {language}: {audio_path}")
    (OUTPUT_DIR / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")


if __name__ == "__main__":
    main()
