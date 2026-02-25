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


def test_gemini_generate_includes_thinking_config_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_ENABLED", "1")
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "256")
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
    assert thinking_config == {"includeThoughts": True, "thinkingBudget": 256}


def test_gemini_generate_omits_thinking_config_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_ENABLED", "0")
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "256")
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
