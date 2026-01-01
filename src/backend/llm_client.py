from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Protocol


class LlmClient(Protocol):
    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class StaticLlmClient:
    response_text: str

    def generate(self, system_prompt: str, history: List[Dict[str, str]]) -> str:
        return self.response_text
