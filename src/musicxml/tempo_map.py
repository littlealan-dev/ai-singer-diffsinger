"""Canonical tempo events shared by score timing and MIDI export."""

from __future__ import annotations

import math
from typing import Iterable


def canonical_tempo_events(events: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Apply the first valid tempo from beat zero, or default to 120 BPM."""
    normalized: list[tuple[float, float]] = []
    for offset, bpm in events:
        try:
            offset, bpm = float(offset), float(bpm)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(offset) or not math.isfinite(bpm) or bpm <= 0:
            continue
        normalized.append((max(0.0, offset), bpm))

    normalized.sort(key=lambda event: event[0])
    if not normalized:
        return [(0.0, 120.0)]
    normalized[0] = (0.0, normalized[0][1])
    return normalized
