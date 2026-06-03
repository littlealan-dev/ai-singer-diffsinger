from __future__ import annotations

"""OpenAI Responses API client implementation for LLM interactions."""

from typing import Any, Dict, List
import json
import logging
import urllib.error
import urllib.request

from src.backend.config import Settings
from src.backend.llm_client import LlmRole
from src.backend.llm_prompt import PromptBundle
from src.mcp.logging_utils import summarize_payload


_OPENAI_LLM_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        },
        "final_message": {"type": "string"},
        "include_score": {"type": "boolean"},
        "thought_summary": {"type": "string"},
    },
    "required": ["tool_calls", "final_message", "include_score"],
    "additionalProperties": False,
}


class OpenAIRestClient:
    """Lightweight REST client for the OpenAI Responses API."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key or settings.openai_api_key
        self._base_url = settings.openai_base_url.rstrip("/")
        self._prompt_cache_enabled = settings.openai_prompt_cache_enabled
        self._prompt_cache_key_prefix = settings.openai_prompt_cache_key_prefix
        self._prompt_cache_retention = settings.openai_prompt_cache_retention
        self._logger = logging.getLogger(__name__)

    def generate(
        self,
        prompt_bundle: PromptBundle | str,
        history: List[Dict[str, str]],
        *,
        role: LlmRole = LlmRole.DEFAULT,
    ) -> str:
        """Generate a response from OpenAI using the Responses API."""
        if isinstance(prompt_bundle, str):
            prompt_bundle = PromptBundle(
                static_prompt_text=prompt_bundle,
                dynamic_prompt_text="Dynamic Context:\nnone\nEnd Dynamic Context.",
            )
        role_config = (
            self._settings.openai_preprocess
            if role == LlmRole.PREPROCESS
            else self._settings.openai_default
        )
        model = role_config.model
        payload: Dict[str, Any] = {
            "model": model,
            "input": self._history_to_input(
                history,
                static_prompt=prompt_bundle.static_prompt_text,
                dynamic_prompt=prompt_bundle.dynamic_prompt_text,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sightsinger_llm_response",
                    "schema": _OPENAI_LLM_RESPONSE_SCHEMA,
                    "strict": False,
                }
            },
        }
        prompt_cache_key = self._prompt_cache_key(model)
        if self._prompt_cache_enabled and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
            if self._prompt_cache_retention:
                payload["prompt_cache_retention"] = self._prompt_cache_retention
        if role_config.reasoning_effort:
            payload["reasoning"] = {"effort": role_config.reasoning_effort}
        self._logger.debug(
            "openai_request_payload role=%s model=%s reasoning_effort=%s payload=%s",
            role.value,
            model,
            role_config.reasoning_effort,
            payload,
        )
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/responses",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=role_config.timeout_seconds) as response:
                body = response.read()
        except TimeoutError as exc:
            raise RuntimeError("OpenAI request timed out.") from exc
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenAI HTTP error {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI connection error: {exc.reason}") from exc

        parsed = json.loads(body)
        self._log_usage(parsed, model=model, prompt_cache_key=prompt_cache_key)
        response_text = self._extract_text(parsed)
        if response_text:
            return response_text
        raise RuntimeError("OpenAI returned no text content.")

    def _history_to_input(
        self,
        history: List[Dict[str, str]],
        *,
        static_prompt: str,
        dynamic_prompt: str,
    ) -> List[Dict[str, str]]:
        """Convert prompt bundle and chat history into Responses API input items."""
        items: List[Dict[str, str]] = [
            {"role": "developer", "content": static_prompt},
            {"role": "user", "content": dynamic_prompt},
        ]
        for entry in history[-10:]:
            role = entry.get("role", "user")
            input_role = "assistant" if role == "assistant" else "user"
            items.append({"role": input_role, "content": entry.get("content", "")})
        return items

    def _prompt_cache_key(self, model: str) -> str:
        """Build a stable deployment-scoped prompt cache key."""
        prefix = self._prompt_cache_key_prefix or "sightsinger"
        build_id = self._settings.backend_build_id or "unknown-build"
        return f"{prefix}:llm:openai:{model}:{build_id}"

    def _extract_text(self, parsed: Dict[str, Any]) -> str:
        """Extract text output from a Responses API payload."""
        output_text = parsed.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = parsed.get("output", [])
        if isinstance(output, list):
            text_parts: List[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in {"output_text", "text"}:
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            text_parts.append(text.strip())
            if text_parts:
                return "\n".join(text_parts)

        raise RuntimeError("OpenAI returned no text content.")

    def _log_usage(self, parsed: Dict[str, Any], *, model: str, prompt_cache_key: str) -> None:
        """Log token usage, including cached prompt-token details when available."""
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        total_tokens = usage.get("total_tokens")
        cached_tokens = None
        prompt_details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
        if isinstance(prompt_details, dict):
            raw_cached_tokens = prompt_details.get("cached_tokens")
            if isinstance(raw_cached_tokens, int):
                cached_tokens = raw_cached_tokens
        self._logger.info(
            "openai_usage model=%s prompt_cache_key=%s retention=%s prompt_tokens=%s cached_tokens=%s completion_tokens=%s total_tokens=%s output=%s",
            model,
            prompt_cache_key,
            self._prompt_cache_retention or "default",
            prompt_tokens,
            cached_tokens,
            completion_tokens,
            total_tokens,
            summarize_payload(self._extract_loggable_output(parsed)),
        )

    def _extract_loggable_output(self, parsed: Dict[str, Any]) -> str:
        """Return a small text preview for usage logging without changing behavior."""
        try:
            return self._extract_text(parsed)
        except RuntimeError:
            return ""
