from __future__ import annotations

"""Factory helpers for constructing LLM clients."""

from typing import Optional

from src.backend.config import Settings
import json
import os

from src.backend.llm_client import LlmClient, StaticLlmClient
from src.backend.llm_gemini import GeminiRestClient
from src.backend.secret_manager import read_secret


def create_llm_client(settings: Settings) -> Optional[LlmClient]:
    """Create an LLM client based on settings, or None if disabled."""
    provider = settings.llm_provider
    if provider in {"", "none", "disabled"}:
        return None
    if provider == "static":
        # Static mode uses fixed response payloads (helpful for tests).
        response_text = os.getenv("LLM_STATIC_RESPONSE")
        loop = os.getenv("LLM_STATIC_LOOP", "").strip().lower() in {"1", "true", "yes"}
        responses: list[str] = []
        if response_text:
            try:
                parsed = json.loads(response_text)
                if isinstance(parsed, list):
                    for entry in parsed:
                        if isinstance(entry, str):
                            responses.append(entry)
                        else:
                            responses.append(json.dumps(entry))
                elif isinstance(parsed, dict):
                    response_text = json.dumps(parsed)
            except json.JSONDecodeError:
                pass
        if not response_text and not responses:
            response_text = json.dumps(
                {
                    "tool_calls": [{"name": "synthesize", "arguments": {}}],
                    "final_message": "Starting synthesis now.",
                    "include_score": False,
                }
            )
        return StaticLlmClient(
            response_text=response_text or (responses[-1] if responses else ""),
            responses=responses,
            loop=loop,
        )
    if provider == "gemini":
        api_key = settings.gemini_api_key
        if settings.app_env.lower() not in {"dev", "development", "local", "test"}:
            # Read the API key from Secret Manager in production-like envs.
            api_key = read_secret(
                settings,
                settings.gemini_api_key_secret,
                settings.gemini_api_key_secret_version,
            )
        if not api_key:
            return None
        return GeminiRestClient(settings, api_key=api_key)
    raise ValueError(f"Unsupported LLM provider: {provider}")
