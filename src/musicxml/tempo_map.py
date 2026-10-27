"""Canonical tempo events shared by score timing and MIDI export."""

from __future__ import annotations

import math
from typing import Iterable

from music21 import stream, tempo


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


def score_tempo_events(score: stream.Score) -> list[tuple[float, float]]:
    """Collect the score-wide tempo marks in beat order, without duplicates."""
    events: list[tuple[float, float]] = []
    for mark in score.recurse().getElementsByClass(tempo.MetronomeMark):
        bpm = mark.getQuarterBPM() if hasattr(mark, "getQuarterBPM") else None
        if bpm is None:
            bpm = mark.number
        if bpm is None or bpm <= 0:
            continue
        try:
            offset = float(mark.getOffsetInHierarchy(score))
        except Exception:
            offset = float(mark.offset)
        events.append((offset, float(bpm)))
    events.sort(key=lambda event: event[0])
    deduped: list[tuple[float, float]] = []
    for event in events:
        if deduped and all(abs(a - b) < 1e-9 for a, b in zip(event, deduped[-1])):
            continue
        deduped.append(event)
    return deduped


def normalize_score_tempo_map(score: stream.Score) -> None:
    """Put the complete score-wide tempo map into every part in memory."""
    raw = score_tempo_events(score)
    if not raw:
        return
    # The canonical map shifts the first mark to beat zero; retain its original
    # late position as well so every part has the same explicit score map.
    events = canonical_tempo_events(raw) + raw
    for part in score.parts:
        measures = list(part.getElementsByClass(stream.Measure))
        if not measures:
            continue
        have = {
            (round(offset, 6), round(bpm, 6))
            for offset, bpm in score_tempo_events(part)
        }
        for offset, bpm in events:
            key = (round(offset, 6), round(bpm, 6))
            if key in have:
                continue
            measure = next((m for m in reversed(measures) if m.offset <= offset), measures[0])
            measure.insert(offset - measure.offset, tempo.MetronomeMark(number=bpm))
            have.add(key)
