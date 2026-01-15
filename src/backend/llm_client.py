from __future__ import annotations

"""LLM client protocol and test stub implementation."""

from dataclasses import dataclass
from typing import Dict, List, Protocol


class LlmClient(Protocol):
    """Protocol for LLM clients used by the orchestrator."""
    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        """Return a model response given prompt and chat history."""
        raise NotImplementedError


@dataclass(frozen=True)
class StaticLlmClient:
    """Static response client for testing and offline flows."""
    response_text: str

    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        """Return the configured static response."""
        return self.response_text
