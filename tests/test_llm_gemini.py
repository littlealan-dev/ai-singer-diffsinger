from __future__ import annotations

import io
import http.client
import json
import logging
import random
import socket
import ssl
import time
import urllib.error
import urllib.request

import pytest

from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_prompt import PromptBundle
from src.backend.llm_gemini import GeminiRestClient


_RETRY_REPLY = '{"tool_calls":[],"final_message":"ok","include_score":false}'


class _RetryCacheManager:
    def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
        return None


class _RetryResponse:
    def __init__(self, body: bytes | BaseException | None = None) -> None:
        self.body = body if body is not None else json.dumps(
            {"candidates": [{"content": {"parts": [{"text": _RETRY_REPLY}]}}]}
        ).encode()

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return False

    def read(self) -> bytes:
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


def _http_error(code: int, body: dict, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid", code, "failure", headers or {}, io.BytesIO(json.dumps(body).encode())
    )


def _script_gemini(monkeypatch: pytest.MonkeyPatch, steps: list[object]):
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
    client = GeminiRestClient(Settings.from_env(), api_key="dummy-secret-key", cache_manager=_RetryCacheManager())
    return client, calls, sleeps


def _gemini_text(client: GeminiRestClient, message: str = "hello") -> str:
    return client.generate("system", [{"role": "user", "content": message}])


def test_retry_remote_disconnect_twice_then_succeeds(monkeypatch, caplog):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_gemini(monkeypatch, [
        http.client.RemoteDisconnected("Remote end closed"),
        http.client.RemoteDisconnected("Remote end closed"),
        _RetryResponse(),
    ])
    caplog.set_level(logging.WARNING, logger="src.backend.llm_gemini")
    assert _gemini_text(client) == _RETRY_REPLY
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage().startswith("llm_http_retry")]) == 2


def test_retry_remote_disconnect_exhausts(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_gemini(monkeypatch, [
        http.client.RemoteDisconnected("Remote end closed") for _ in range(3)
    ])
    with pytest.raises(RuntimeError, match="Gemini connection error: Remote end closed"):
        _gemini_text(client)
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retry_gemini_503(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, _ = _script_gemini(monkeypatch, [_http_error(503, {"error": "busy"}), _RetryResponse()])
    assert _gemini_text(client) == _RETRY_REPLY
    assert len(calls) == 2


def test_gemini_spending_cap_429_fails_immediately(monkeypatch):  # type: ignore[no-untyped-def]
    body = {"error": {"code": 429, "message": "Your project has exceeded its monthly spending cap.", "status": "RESOURCE_EXHAUSTED"}}
    client, calls, sleeps = _script_gemini(monkeypatch, [_http_error(429, body)])
    with pytest.raises(RuntimeError, match="Gemini HTTP error 429"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


def test_retry_gemini_429_retry_info(monkeypatch):  # type: ignore[no-untyped-def]
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}]}}
    client, calls, sleeps = _script_gemini(monkeypatch, [_http_error(429, body), _RetryResponse()])
    assert _gemini_text(client) == _RETRY_REPLY
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_gemini_429_excessive_retry_delay_fails_immediately(monkeypatch):  # type: ignore[no-untyped-def]
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "60s"}]}}
    client, calls, sleeps = _script_gemini(monkeypatch, [_http_error(429, body)])
    with pytest.raises(RuntimeError, match="Gemini HTTP error 429"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


def test_gemini_raw_timeout_is_not_retried(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_gemini(monkeypatch, [TimeoutError("read timed out")])
    with pytest.raises(RuntimeError, match="Gemini request timed out"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("error", [
    urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
    urllib.error.URLError(TimeoutError("timed out")),
])
def test_retry_gemini_connect_failure(monkeypatch, error):  # type: ignore[no-untyped-def]
    client, calls, _ = _script_gemini(monkeypatch, [error, _RetryResponse()])
    assert _gemini_text(client) == _RETRY_REPLY
    assert len(calls) == 2


def test_retry_gemini_incomplete_read(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, _ = _script_gemini(monkeypatch, [_RetryResponse(http.client.IncompleteRead(b"")), _RetryResponse()])
    assert _gemini_text(client) == _RETRY_REPLY
    assert len(calls) == 2


@pytest.mark.parametrize("status", [504, 400])
def test_gemini_permanent_http_error_fails_immediately(monkeypatch, status):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_gemini(monkeypatch, [_http_error(status, {"error": "permanent"})])
    with pytest.raises(RuntimeError, match=f"Gemini HTTP error {status}"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


def test_gemini_certificate_failure_is_not_retried(monkeypatch):  # type: ignore[no-untyped-def]
    error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    client, calls, sleeps = _script_gemini(monkeypatch, [error])
    with pytest.raises(RuntimeError, match="Gemini connection error"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


def test_gemini_one_attempt_maps_disconnect(monkeypatch):  # type: ignore[no-untyped-def]
    client, calls, sleeps = _script_gemini(monkeypatch, [http.client.RemoteDisconnected("Remote end closed")])
    monkeypatch.setenv("LLM_RETRY_MAX_ATTEMPTS", "1")
    client._settings = Settings.from_env()
    with pytest.raises(RuntimeError, match="Gemini connection error: Remote end closed"):
        _gemini_text(client)
    assert len(calls) == 1
    assert sleeps == []


def test_gemini_logs_do_not_include_prompt_or_key(monkeypatch, caplog):  # type: ignore[no-untyped-def]
    sentinel = "private-sentinel-message"
    client, _, _ = _script_gemini(monkeypatch, [_RetryResponse()])
    caplog.set_level(logging.WARNING)
    assert _gemini_text(client, sentinel) == _RETRY_REPLY
    assert sentinel not in caplog.text
    assert "dummy-secret-key" not in caplog.text


def _settings() -> Settings:
    return Settings.from_env()


def test_gemini_generate_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GeminiRestClient(_settings(), api_key="dummy")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TimeoutError("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    with pytest.raises(RuntimeError, match="Gemini request timed out"):
        client.generate("system", [{"role": "user", "content": "hello"}])


def test_gemini_generate_uses_cached_content_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CacheManager:
        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            return "cachedContents/prompt-cache-123"

    client = GeminiRestClient(
        _settings(), api_key="dummy", cache_manager=_CacheManager()
    )
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

    client.generate(
        PromptBundle(static_prompt_text="STATIC", dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context."),
        [{"role": "user", "content": "hello"}],
    )
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["cachedContent"] == "cachedContents/prompt-cache-123"
    assert "system_instruction" not in payload
    current_request = payload["contents"][-1]["parts"][0]["text"]
    assert current_request.startswith("Dynamic Context:\n")
    assert current_request.endswith("Current user request:\nhello")


def test_gemini_generate_logs_structured_usage_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "high")

    class _CacheManager:
        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            return "cachedContents/prompt-cache-123"

    client = GeminiRestClient(_settings(), api_key="dummy", cache_manager=_CacheManager())

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "responseId": "response-123",
                    "modelVersion": "gemini-test-version",
                    "usageMetadata": {
                        "promptTokenCount": 1000,
                        "cachedContentTokenCount": 800,
                        "candidatesTokenCount": 120,
                        "thoughtsTokenCount": 75,
                        "toolUsePromptTokenCount": 5,
                        "totalTokenCount": 1200,
                        "promptTokensDetails": [
                            {"modality": "TEXT", "tokenCount": 1000}
                        ],
                        "cacheTokensDetails": [
                            {"modality": "TEXT", "tokenCount": 800}
                        ],
                        "candidatesTokensDetails": [
                            {"modality": "TEXT", "tokenCount": 120}
                        ],
                        "toolUsePromptTokensDetails": [
                            {"modality": "TEXT", "tokenCount": 5}
                        ],
                        "serviceTier": "PAYG",
                        "trafficType": "ON_DEMAND",
                    },
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": '{"tool_calls":[],"final_message":"ok","include_score":false}'
                                    },
                                ]
                            }
                        }
                    ],
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: _Resp()
    )
    caplog.set_level(logging.INFO, logger="src.backend.llm_gemini")

    client.generate("system", [{"role": "user", "content": "hello"}])

    usage_record = next(
        record for record in caplog.records if record.msg == "gemini_usage"
    )
    assert usage_record.event == "gemini_usage"
    assert usage_record.model == _settings().gemini_default.model
    assert usage_record.role == "default"
    assert usage_record.response_id == "response-123"
    assert usage_record.model_version == "gemini-test-version"
    assert usage_record.usage_metadata_present is True
    assert usage_record.cache_requested is True
    assert usage_record.cache_effective is True
    assert usage_record.thinking_config_sent is True
    assert usage_record.thinking_level_requested == "high"
    assert usage_record.prompt_token_count == 1000
    assert usage_record.cached_content_token_count == 800
    assert usage_record.uncached_prompt_token_count == 200
    assert usage_record.candidates_token_count == 120
    assert usage_record.thoughts_token_count == 75
    assert usage_record.tool_use_prompt_token_count == 5
    assert usage_record.total_token_count == 1200
    assert usage_record.service_tier == "PAYG"
    assert usage_record.traffic_type == "ON_DEMAND"


def test_gemini_generate_logs_usage_before_candidate_validation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = GeminiRestClient(_settings(), api_key="dummy")

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "usageMetadata": {
                        "promptTokenCount": 250,
                        "totalTokenCount": 250,
                    },
                    "candidates": [],
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: _Resp()
    )
    caplog.set_level(logging.INFO, logger="src.backend.llm_gemini")

    with pytest.raises(RuntimeError, match="Gemini returned no candidates"):
        client.generate("system", [{"role": "user", "content": "hello"}])

    usage_record = next(
        record for record in caplog.records if record.msg == "gemini_usage"
    )
    assert usage_record.prompt_token_count == 250
    assert usage_record.cached_content_token_count is None
    assert usage_record.uncached_prompt_token_count == 250
    assert usage_record.total_token_count == 250


def test_gemini_places_authoritative_context_after_stale_history() -> None:
    client = GeminiRestClient(_settings(), api_key="dummy")

    contents = client._history_to_contents(
        [
            {"role": "user", "content": "Use fixed do."},
            {"role": "assistant", "content": "The setting is fixed do."},
            {"role": "user", "content": "Sing soprano in solfege."},
        ],
        dynamic_prompt="Dynamic Context:\nsystem: movable_do\nEnd Dynamic Context.",
    )

    assert contents[1]["parts"][0]["text"] == "The setting is fixed do."
    assert contents[-1]["parts"][0]["text"].startswith(
        "Dynamic Context:\nsystem: movable_do"
    )
    assert contents[-1]["parts"][0]["text"].endswith(
        "Current user request:\nSing soprano in solfege."
    )


def test_gemini_generate_uses_preprocess_role_model_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-base")
    monkeypatch.setenv("GEMINI_PREPROCESS_MODEL", "gemini-strong")
    monkeypatch.setenv("GEMINI_PREPROCESS_THINKING_LEVEL", "high")
    monkeypatch.setenv("GEMINI_PREPROCESS_TIMEOUT_SECONDS", "222")

    class _CacheManager:
        calls: list[dict[str, object]] = []

        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(dict(kwargs))
            return None

    cache_manager = _CacheManager()
    client = GeminiRestClient(_settings(), api_key="dummy", cache_manager=cache_manager)
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
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = request.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client.generate(
        PromptBundle(static_prompt_text="STATIC", dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context."),
        [{"role": "user", "content": "hello"}],
        role=LlmRole.PREPROCESS,
    )

    assert captured["url"].endswith("/models/gemini-strong:generateContent")
    assert captured["timeout"] == 222
    assert cache_manager.calls[0]["model"] == "gemini-strong"
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingLevel": "high"},
    }


def test_gemini_generate_retries_uncached_when_cached_content_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CacheManager:
        invalidated: list[dict[str, object]] = []

        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            return "cachedContents/deleted-cache"

        def invalidate_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            self.invalidated.append(dict(kwargs))

    cache_manager = _CacheManager()
    client = GeminiRestClient(_settings(), api_key="dummy", cache_manager=cache_manager)
    captured_payloads: list[dict[str, object]] = []

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
        payload = json.loads((request.data or b"{}").decode("utf-8"))
        captured_payloads.append(payload)
        if len(captured_payloads) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(
                    b'{"error":{"message":"CachedContent not found (or permission denied)"}}'
                ),
            )
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    text = client.generate(
        PromptBundle(static_prompt_text="STATIC", dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context."),
        [{"role": "user", "content": "hello"}],
    )

    assert '"final_message":"ok"' in text
    assert captured_payloads[0]["cachedContent"] == "cachedContents/deleted-cache"
    assert "system_instruction" not in captured_payloads[0]
    assert "cachedContent" not in captured_payloads[1]
    assert captured_payloads[1]["system_instruction"] == {"parts": [{"text": "STATIC"}]}
    assert cache_manager.invalidated
    assert cache_manager.invalidated[0]["cache_name"] == "cachedContents/deleted-cache"


def test_gemini_generate_falls_back_to_system_instruction_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CacheManager:
        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    client = GeminiRestClient(_settings(), api_key="dummy", cache_manager=_CacheManager())
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

    client.generate(
        PromptBundle(static_prompt_text="STATIC", dynamic_prompt_text="Dynamic Context:\nctx\nEnd Dynamic Context."),
        [{"role": "user", "content": "hello"}],
    )
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    assert payload["system_instruction"] == {"parts": [{"text": "STATIC"}]}
    assert "cachedContent" not in payload


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
    parsed_text = json.loads(text)
    assert parsed_text["final_message"] == "ok"
    payload = json.loads((captured["body"] or b"{}").decode("utf-8"))
    generation_config = payload.get("generationConfig")
    assert isinstance(generation_config, dict)
    thinking_config = generation_config.get("thinkingConfig")
    assert thinking_config == {"thinkingLevel": "high", "includeThoughts": True}
    assert generation_config["responseMimeType"] == "application/json"
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
    assert payload["generationConfig"] == {"responseMimeType": "application/json"}


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
    assert generation_config["responseMimeType"] == "application/json"
    assert generation_config.get("thinkingConfig") == {"includeThoughts": True}
    parsed_text = json.loads(text)
    assert parsed_text["thought_summary"] == "summary only"


def test_gemini_generate_sends_a_response_schema_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "")
    monkeypatch.setenv("GEMINI_INCLUDE_THOUGHT_SUMMARY", "0")
    client = GeminiRestClient(_settings(), api_key="dummy")
    bodies: list[dict] = []

    class _Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return False

        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"quote_presentation":{}}'}]}}]}
            ).encode("utf-8")

    def _fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    schema = {"type": "OBJECT", "properties": {"quote_presentation": {"type": "OBJECT"}}}

    client.generate("system", [{"role": "user", "content": "hello"}], response_schema=schema)
    client.generate("system", [{"role": "user", "content": "hello"}])

    with_schema, without = bodies
    assert with_schema["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseSchema": schema,
    }
    assert without["generationConfig"] == {"responseMimeType": "application/json"}
