from __future__ import annotations

"""Gemini REST client implementation for LLM interactions."""

from typing import Any, Dict, List
import json
import logging
import urllib.error
import urllib.request

from src.backend.config import Settings
from src.mcp.logging_utils import summarize_payload


class GeminiRestClient:
    """Lightweight REST client for Google Gemini."""
    def __init__(self, settings: Settings, *, api_key: str | None = None) -> None:
        """Configure client endpoints, model, and timeouts."""
        self._api_key = api_key or settings.gemini_api_key
        self._base_url = settings.gemini_base_url
        self._model = settings.gemini_model
        self._timeout = settings.gemini_timeout_seconds
        self._thinking_enabled = bool(settings.gemini_thinking_enabled)
        self._thinking_budget = int(settings.gemini_thinking_budget)
        self._logger = logging.getLogger(__name__)

    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        """Generate a response from Gemini using the REST API."""
        url = f"{self._base_url}/models/{self._model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": self._history_to_contents(history),
        }
        if self._thinking_enabled:
            thinking_config: Dict[str, Any] = {"includeThoughts": True}
            if self._thinking_budget > 0:
                thinking_config["thinkingBudget"] = self._thinking_budget
            payload["generationConfig"] = {"thinkingConfig": thinking_config}
        # Encode payload as JSON for the HTTP request body.
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except TimeoutError as exc:
            raise RuntimeError("Gemini request timed out.") from exc
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini HTTP error {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini connection error: {exc.reason}") from exc

        parsed = json.loads(body)
        candidates = parsed.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise RuntimeError("Gemini returned empty content parts.")
        response_text_parts: List[str] = []
        thinking_parts: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if bool(part.get("thought")):
                thinking_parts.append(stripped)
            else:
                response_text_parts.append(stripped)
        if self._thinking_enabled and thinking_parts:
            self._logger.debug(
                "gemini_thinking model=%s parts=%s thought=%s",
                self._model,
                len(thinking_parts),
                summarize_payload("\n".join(thinking_parts)),
            )
        if response_text_parts:
            return "\n".join(response_text_parts)
        # Fallback for models that return text-only parts without thought tags.
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = part["text"].strip()
                if text:
                    return text
        raise RuntimeError("Gemini returned no text content.")

    def _history_to_contents(self, history: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Convert chat history into Gemini content payloads."""
        contents: List[Dict[str, Any]] = []
        for entry in history[-10:]:
            role = entry.get("role", "user")
            content_role = "model" if role == "assistant" else "user"
            contents.append({"role": content_role, "parts": [{"text": entry.get("content", "")}]})
        return contents
