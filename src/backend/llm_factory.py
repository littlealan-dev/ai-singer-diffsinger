from __future__ import annotations

from typing import Optional

from src.backend.config import Settings
from src.backend.llm_client import LlmClient
from src.backend.llm_gemini import GeminiRestClient


def create_llm_client(settings: Settings) -> Optional[LlmClient]:
    provider = settings.llm_provider
    if provider in {"", "none", "disabled"}:
        return None
    if provider == "gemini":
        if not settings.gemini_api_key:
            return None
        return GeminiRestClient(settings)
    raise ValueError(f"Unsupported LLM provider: {provider}")
