from __future__ import annotations

import json
import http.client
import io
import random
import time
import urllib.error
import urllib.request

import pytest

from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_openai import OpenAIRestClient
from src.backend.llm_prompt import PromptBundle


def _script_openai(monkeypatch: pytest.MonkeyPatch, steps: list[object]):
    monkeypatch.setenv("LLM_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY_SECONDS", "1")
    monkeypatch.setenv("LLM_RETRY_MAX_DELAY_SECONDS", "10")
    calls: list[object] = []
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)

    def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        calls.append(request)
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return OpenAIRestClient(Settings.from_env(), api_key="dummy"), calls, sleeps


def _openai_429(code: str, retry_after: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid", 429, "rate limited", {"Retry-After": retry_after},
        io.BytesIO(json.dumps({"error": {"code": code}}).encode()),
    )


def test_retry_openai_remote_disconnect(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, _ = _script_openai(monkeypatch, [
        http.client.RemoteDisconnected("Remote end closed"),
        _Response({"output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}'}),
    ])
    assert '"final_message":"ok"' in client.generate("system", [{"role": "user", "content": "hello"}])
    assert len(calls) == 2


def test_retry_openai_429_with_retry_after(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_openai(monkeypatch, [
        _openai_429("rate_limit_exceeded", "2"),
        _Response({"output_text": '{"tool_calls":[],"final_message":"ok","include_score":false}'}),
    ])
    assert '"final_message":"ok"' in client.generate("system", [{"role": "user", "content": "hello"}])
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_openai_insufficient_quota_is_not_retried(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_openai(monkeypatch, [_openai_429("insufficient_quota", "1")])
    with pytest.raises(RuntimeError, match="OpenAI HTTP error 429"):
        client.generate("system", [{"role": "user", "content": "hello"}])
    assert len(calls) == 1
    assert sleeps == []


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
    assert payload["input"][-1]["content"].startswith("Dynamic Context:\n")
    assert payload["input"][-1]["content"].endswith("Current user request:\nhello")
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


def test_openai_generate_uses_a_given_response_schema_as_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenAIRestClient(_settings(), api_key="dummy")
    captured: dict[str, object] = {}

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        captured["body"] = request.data
        return _Response({"output_text": '{"quote_presentation":{}}'})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    schema = {
        "type": "OBJECT",
        "required": ["quote_presentation"],
        "properties": {
            "quote_presentation": {
                "type": "OBJECT",
                "propertyOrdering": ["quote_id"],
                "properties": {"quote_id": {"type": "STRING"}},
            }
        },
    }

    client.generate("STATIC", [{"role": "user", "content": "hello"}], response_schema=schema)

    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["text"]["format"]["name"] == "sightsinger_fixed_reply"
    assert payload["text"]["format"]["schema"] == {
        "type": "object",
        "required": ["quote_presentation"],
        "properties": {
            "quote_presentation": {
                "type": "object",
                "properties": {"quote_id": {"type": "string"}},
            }
        },
    }
