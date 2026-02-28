from __future__ import annotations

import urllib.request
import json

import pytest

from src.backend.config import Settings
from src.backend.llm_gemini import GeminiRestClient


def _settings() -> Settings:
    return Settings.from_env()


def test_gemini_generate_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GeminiRestClient(_settings(), api_key="dummy")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TimeoutError("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    with pytest.raises(RuntimeError, match="Gemini request timed out"):
        client.generate("system", [{"role": "user", "content": "hello"}])


def test_gemini_generate_includes_thinking_config_when_level_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "high")
    monkeypatch.setenv("GEMINI_INCLUDE_THOUGHT_SUMMARY", "1")
    client = GeminiRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "internal reasoning"},
                                    {"text": '{"tool_calls":[],"final_message":"ok","include_score":false}'},
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["timeout"] = timeout
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    text = client.generate("system", [{"role": "user", "content": "hello"}])
    assert '"final_message":"ok"' in text
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    generation_config = payload.get("generationConfig")
    assert isinstance(generation_config, dict)
    thinking_config = generation_config.get("thinkingConfig")
    assert thinking_config == {"thinkingLevel": "high", "includeThoughts": True}
    parsed_text = json.loads(text)
    assert parsed_text["thought_summary"] == "internal reasoning"


def test_gemini_generate_omits_thinking_config_when_level_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "")
    monkeypatch.setenv("GEMINI_INCLUDE_THOUGHT_SUMMARY", "0")
    client = GeminiRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": '{"tool_calls":[],"final_message":"ok","include_score":false}'},
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    text = client.generate("system", [{"role": "user", "content": "hello"}])
    assert '"final_message":"ok"' in text
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert "generationConfig" not in payload


def test_gemini_generate_includes_thoughts_without_explicit_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "")
    monkeypatch.setenv("GEMINI_INCLUDE_THOUGHT_SUMMARY", "1")
    client = GeminiRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "summary only"},
                                    {"text": '{"tool_calls":[],"final_message":"ok","include_score":false}'},
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    text = client.generate("system", [{"role": "user", "content": "hello"}])
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    generation_config = payload.get("generationConfig")
    assert isinstance(generation_config, dict)
    assert generation_config.get("thinkingConfig") == {"includeThoughts": True}
    parsed_text = json.loads(text)
    assert parsed_text["thought_summary"] == "summary only"
