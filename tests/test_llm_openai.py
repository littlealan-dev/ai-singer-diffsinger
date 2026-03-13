from __future__ import annotations

import json
import urllib.request

import pytest

from src.backend.config import Settings
from src.backend.llm_openai import OpenAIRestClient
from src.backend.llm_prompt import PromptBundle


def _settings() -> Settings:
    return Settings.from_env()


def test_openai_generate_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAIRestClient(_settings(), api_key="dummy")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TimeoutError("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    with pytest.raises(RuntimeError, match="OpenAI request timed out"):
        client.generate("system", [{"role": "user", "content": "hello"}])


def test_openai_generate_includes_prompt_cache_fields_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "24h")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("BACKEND_BUILD_ID", "build-123")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}',
                    "usage": {"prompt_tokens_details": {"cached_tokens": 42}},
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate(
        PromptBundle(
            static_prompt_text="STATIC",
            dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context.",
        ),
        [{"role": "user", "content": "hello"}],
    )
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["prompt_cache_key"] == "sightsinger:llm:openai:gpt-5.4:build-123"
    assert payload["prompt_cache_retention"] == "24h"
    assert payload["input"][0] == {"role": "developer", "content": "STATIC"}
    assert payload["input"][1]["content"].startswith("Dynamic Context:\n")


def test_openai_generate_omits_prompt_cache_fields_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_ENABLED", "0")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"tool_calls":[],"final_message":"ok","include_score":false}',
                                }
                            ]
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate("system", [{"role": "user", "content": "hello"}])
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert "prompt_cache_key" not in payload
    assert "prompt_cache_retention" not in payload


def test_openai_generate_includes_reasoning_effort_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {"output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}'}
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate("system", [{"role": "user", "content": "hello"}])
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["reasoning"] == {"effort": "low"}

