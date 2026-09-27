from __future__ import annotations

"""Shared synthesis pricing arithmetic and estimate serialization."""

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Optional


VOCAL_CREDIT_DURATION_SECONDS = 30
INSTRUMENTAL_CREDIT_DURATION_SECONDS = 120
CREDIT_DURATION_PRECISION_SECONDS = 0.001
SYNTHESIS_PRICING_VERSION = 1


def _normalized_positive_duration(duration_seconds: float) -> float:
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Synthesis duration must be finite and positive.")
    return (
        round(duration / CREDIT_DURATION_PRECISION_SECONDS)
        * CREDIT_DURATION_PRECISION_SECONDS
    )


def credits_for_duration(duration_seconds: float, unit_seconds: int) -> int:
    if unit_seconds <= 0:
        raise ValueError("Pricing unit must be positive.")
    return math.ceil(_normalized_positive_duration(duration_seconds) / unit_seconds)


@dataclass(frozen=True)
class SynthesisCreditBreakdown:
    duration_seconds: float
    vocal_part_credits: int
    instrumental_credits: int
    total_credits: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_synthesis_credit_breakdown(
    *, duration_seconds: float, include_instrumentals: bool
) -> SynthesisCreditBreakdown:
    duration = _normalized_positive_duration(duration_seconds)
    vocal = credits_for_duration(duration, VOCAL_CREDIT_DURATION_SECONDS)
    instrumental = (
        credits_for_duration(duration, INSTRUMENTAL_CREDIT_DURATION_SECONDS)
        if include_instrumentals
        else 0
    )
    return SynthesisCreditBreakdown(
        duration_seconds=duration,
        vocal_part_credits=vocal,
        instrumental_credits=instrumental,
        total_credits=vocal + instrumental,
    )


@dataclass(frozen=True)
class SynthesisCreditEstimate:
    pricing_version: int
    vocal_part_id: Optional[str]
    expand_repeats: bool
    vocal_duration_seconds: float
    vocal_part_credits: int
    instrumental_credits: int
    total_estimated_credits: int
    has_instrumental_parts: bool
    instrumental_charge_required: bool
    instrumental_charge_scope: Optional[str]
    instrumental_pricing_expand_repeats: Optional[bool]
    instrumental_pricing_duration_seconds: Optional[float]
    vocal_pricing_unit_seconds: int = VOCAL_CREDIT_DURATION_SECONDS
    instrumental_pricing_unit_seconds: int = INSTRUMENTAL_CREDIT_DURATION_SECONDS

    @property
    def billing_components(self) -> tuple[str, ...]:
        if self.instrumental_charge_required:
            return ("vocal", "instrumental")
        return ("vocal",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pricing_version": self.pricing_version,
            "vocal_part_id": self.vocal_part_id,
            "expand_repeats": self.expand_repeats,
            "vocal_duration_seconds": self.vocal_duration_seconds,
            "vocal_part": {
                "pricing_unit_seconds": self.vocal_pricing_unit_seconds,
                "estimated_credits": self.vocal_part_credits,
            },
            "instrumentals": {
                "has_instrumental_parts": self.has_instrumental_parts,
                "charge_required": self.instrumental_charge_required,
                "charge_scope": self.instrumental_charge_scope,
                "pricing_expand_repeats": self.instrumental_pricing_expand_repeats,
                "pricing_duration_seconds": self.instrumental_pricing_duration_seconds,
                "pricing_unit_seconds": self.instrumental_pricing_unit_seconds,
                "estimated_credits": self.instrumental_credits,
                "charged_once_for_all_tracks": True,
            },
            "billing_components": list(self.billing_components),
            "total_estimated_credits": self.total_estimated_credits,
        }


def score_has_instrumental_parts(score_summary: Mapping[str, Any] | None) -> bool:
    if not isinstance(score_summary, Mapping):
        return False
    resolution = score_summary.get("instrument_program_resolution")
    if not isinstance(resolution, Mapping):
        return False
    route_ids = resolution.get("instrumental_score_instrument_ids")
    return isinstance(route_ids, list) and any(
        isinstance(item, str) and item.strip() for item in route_ids
    )


def selected_score_duration(
    score_summary: Mapping[str, Any], *, expand_repeats: bool
) -> float:
    key = "expanded_duration_seconds" if expand_repeats else "duration_seconds"
    raw = score_summary.get(key)
    if expand_repeats and not isinstance(raw, (int, float)):
        raw = score_summary.get("duration_seconds")
    if not isinstance(raw, (int, float)):
        raise ValueError("Score duration is unavailable.")
    return _normalized_positive_duration(float(raw))


def estimate_synthesis_credits(
    *,
    vocal_duration_seconds: float,
    vocal_part_id: Optional[str],
    expand_repeats: bool,
    has_instrumental_parts: bool,
    instrumental_charge_required: bool,
    instrumental_charge_scope: Optional[str],
) -> SynthesisCreditEstimate:
    if instrumental_charge_required and not has_instrumental_parts:
        raise ValueError("Instrumental charge cannot be required without instrumentals.")
    breakdown = calculate_synthesis_credit_breakdown(
        duration_seconds=vocal_duration_seconds,
        include_instrumentals=instrumental_charge_required,
    )
    return SynthesisCreditEstimate(
        pricing_version=SYNTHESIS_PRICING_VERSION,
        vocal_part_id=vocal_part_id,
        expand_repeats=bool(expand_repeats),
        vocal_duration_seconds=breakdown.duration_seconds,
        vocal_part_credits=breakdown.vocal_part_credits,
        instrumental_credits=breakdown.instrumental_credits,
        total_estimated_credits=breakdown.total_credits,
        has_instrumental_parts=bool(has_instrumental_parts),
        instrumental_charge_required=bool(instrumental_charge_required),
        instrumental_charge_scope=(
            instrumental_charge_scope if has_instrumental_parts else None
        ),
        instrumental_pricing_expand_repeats=(
            bool(expand_repeats) if instrumental_charge_required else None
        ),
        instrumental_pricing_duration_seconds=(
            breakdown.duration_seconds if instrumental_charge_required else None
        ),
    )
