from __future__ import annotations

"""LLM client protocol and test stub implementation."""

from dataclasses import dataclass, field
from typing import Dict, List, Protocol
import json

TOOL_RESULT_PREFIX = "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"


class LlmClient(Protocol):
    """Protocol for LLM clients used by the orchestrator."""
    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        """Return a model response given prompt and chat history."""
        raise NotImplementedError


@dataclass
class StaticLlmClient:
    """Static response client for testing and offline flows."""
    response_text: str
    responses: List[str] = field(default_factory=list)
    loop: bool = False
    _index: int = 0

    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        """Return the configured static response."""
        if history:
            last = history[-1].get("content", "")
            if isinstance(last, str) and last.startswith(TOOL_RESULT_PREFIX):
                tool_output = last[len(TOOL_RESULT_PREFIX):].strip()
                return json.dumps(
                    {
                        "tool_calls": [],
                        "final_message": tool_output,
                        "include_score": False,
                    }
                )
            if self.responses and len(history) <= 2:
                self._index = 0
        if self.responses:
            response = self.responses[min(self._index, len(self.responses) - 1)]
            if self.loop:
                self._index = (self._index + 1) % len(self.responses)
            else:
                self._index += 1
            return response
        return self.response_text
