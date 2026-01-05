from __future__ import annotations

from typing import Optional

from src.backend.config import Settings
from src.backend.llm_client import LlmClient
from src.backend.llm_gemini import GeminiRestClient
from src.backend.secret_manager import read_secret


def create_llm_client(settings: Settings) -> Optional[LlmClient]:
    provider = settings.llm_provider
    if provider in {"", "none", "disabled"}:
        return None
    if provider == "gemini":
        api_key = settings.gemini_api_key
        if settings.app_env.lower() not in {"dev", "development", "local", "test"}:
            api_key = read_secret(
                settings,
                settings.gemini_api_key_secret,
                settings.gemini_api_key_secret_version,
            )
        if not api_key:
            return None
        return GeminiRestClient(settings, api_key=api_key)
    raise ValueError(f"Unsupported LLM provider: {provider}")
