from __future__ import annotations

import json
import urllib.request

import pytest

from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_openai import OpenAIRestClient
from src.backend.llm_prompt import PromptBundle


def _settings() -> Settings:
    return Settings.from_env()


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


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
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.5")
    monkeypatch.setenv("BACKEND_BUILD_ID", "build-123")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Response(
            {
                "output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}',
                "usage": {"input_tokens_details": {"cached_tokens": 42}},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate(
        PromptBundle(
            static_prompt_text="STATIC",
            dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context.",
        ),
        [{"role": "user", "content": "hello"}],
    )
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["prompt_cache_key"] == "sightsinger:llm:openai:gpt-5.5:build-123"
    assert payload["prompt_cache_retention"] == "24h"
    assert payload["input"][0] == {"role": "developer", "content": "STATIC"}
    assert payload["input"][1]["content"].startswith("Dynamic Context:\n")
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "sightsinger_llm_response"
    assert payload["text"]["format"]["strict"] is False
    schema = payload["text"]["format"]["schema"]
    assert schema["required"] == ["tool_calls", "final_message", "include_score"]
    assert schema["properties"]["tool_calls"]["items"]["required"] == ["name", "arguments"]


def test_openai_generate_omits_prompt_cache_fields_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_ENABLED", "0")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Response(
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
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate("system", [{"role": "user", "content": "hello"}])
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert "prompt_cache_key" not in payload
    assert "prompt_cache_retention" not in payload


def test_openai_generate_uses_role_specific_models_and_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_DEFAULT_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_PREPROCESS_MODEL", "gpt-5.5-pro")
    monkeypatch.setenv("OPENAI_PREPROCESS_REASONING_EFFORT", "medium")
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _Response(
            {"output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}'}
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate("system", [{"role": "user", "content": "hello"}], role=LlmRole.PREPROCESS)
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["model"] == "gpt-5.5-pro"
    assert payload["reasoning"] == {"effort": "medium"}
