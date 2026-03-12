"""Shared timing error types for synthesis and preprocessing flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class InfeasibleAnchorError(ValueError):
    """Raised when an anchor window cannot allocate >=1 frame per phoneme."""

    stage: str
    group_index: int
    anchor_total: int
    phoneme_count: int
    note_index: Optional[int] = None
    detail: str = "insufficient_anchor_budget"

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "error_type": "InfeasibleAnchorError",
            "stage": self.stage,
            "group_index": int(self.group_index),
            "anchor_total": int(self.anchor_total),
            "phoneme_count": int(self.phoneme_count),
            "detail": self.detail,
        }
        if self.note_index is not None:
            payload["note_index"] = int(self.note_index)
        return payload

    def __str__(self) -> str:
        return (
            f"{self.detail}: stage={self.stage} group={self.group_index} "
            f"anchor_total={self.anchor_total} phonemes={self.phoneme_count}"
        )

