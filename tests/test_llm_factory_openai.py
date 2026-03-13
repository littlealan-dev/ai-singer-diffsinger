from __future__ import annotations

import pytest

from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
from src.backend.llm_openai import OpenAIRestClient


def test_openai_llm_provider_constructs_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings.from_env()
    client = create_llm_client(settings)
    assert isinstance(client, OpenAIRestClient)


def test_openai_prompt_cache_retention_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "bad")
    with pytest.raises(ValueError, match="OPENAI_PROMPT_CACHE_RETENTION"):
        Settings.from_env()


def test_openai_reasoning_effort_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "bad")
    with pytest.raises(ValueError, match="OPENAI_REASONING_EFFORT"):
        Settings.from_env()
