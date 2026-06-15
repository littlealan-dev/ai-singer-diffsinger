from __future__ import annotations

import io
import urllib.request
import json
import urllib.error

import pytest

from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_prompt import PromptBundle
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


def test_gemini_generate_uses_cached_content_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CacheManager:
        def ensure_prompt_cache(self, **kwargs):  # type: ignore[no-untyped-def]
            return "cachedContents/prompt-cache-123"

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
    assert payload["cachedContent"] == "cachedContents/prompt-cache-123"
    assert "system_instruction" not in payload
    assert payload["contents"][0]["parts"][0]["text"].startswith("Dynamic Context:\n")


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
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


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
