from __future__ import annotations

"""Plan catalog and Stripe price mapping for billing."""

from dataclasses import dataclass
import os

from src.backend.billing_config import BillingConfig
from src.backend.billing_types import BillingInterval, PlanFamily, PlanKey


@dataclass(frozen=True)
class PlanDefinition:
    key: PlanKey
    family: PlanFamily
    billing_interval: BillingInterval
    monthly_allowance: int
    stripe_price_id: str | None
    stripe_product_id: str | None
    is_early_supporter: bool = False

    @property
    def is_paid(self) -> bool:
        return self.key != "free"


DEFAULT_FREE_TIER_MONTHLY_ALLOWANCE = 8


def get_free_tier_monthly_allowance() -> int:
    """Return the deployment-specific allowance for the free plan.

    The WebMCP challenge backend sets this to 100 in its own environment,
    without changing the core product's 8-credit default.
    """
    raw_value = os.getenv("FREE_TIER_MONTHLY_ALLOWANCE", str(DEFAULT_FREE_TIER_MONTHLY_ALLOWANCE))
    try:
        allowance = int(raw_value)
    except ValueError as exc:
        raise ValueError("FREE_TIER_MONTHLY_ALLOWANCE must be a positive integer.") from exc
    if allowance < 1:
        raise ValueError("FREE_TIER_MONTHLY_ALLOWANCE must be a positive integer.")
    return allowance


def get_free_plan() -> PlanDefinition:
    return PlanDefinition(
        "free",
        "free",
        "none",
        get_free_tier_monthly_allowance(),
        None,
        None,
    )


def get_plan_catalog(config: BillingConfig) -> dict[PlanKey, PlanDefinition]:
    return {
        "free": get_free_plan(),
        "solo_monthly": PlanDefinition(
            "solo_monthly",
            "solo",
            "month",
            30,
            config.stripe_price_solo_monthly,
            config.stripe_product_solo,
        ),
        "solo_annual": PlanDefinition(
            "solo_annual",
            "solo",
            "year",
            30,
            config.stripe_price_solo_annual,
            config.stripe_product_solo,
        ),
        "choir_early_monthly": PlanDefinition(
            "choir_early_monthly",
            "choir",
            "month",
            120,
            config.stripe_price_choir_early_monthly,
            config.stripe_product_choir,
            True,
        ),
        "choir_early_annual": PlanDefinition(
            "choir_early_annual",
            "choir",
            "year",
            120,
            config.stripe_price_choir_early_annual,
            config.stripe_product_choir,
            True,
        ),
        "choir_monthly": PlanDefinition(
            "choir_monthly",
            "choir",
            "month",
            120,
            config.stripe_price_choir_monthly,
            config.stripe_product_choir,
        ),
        "choir_annual": PlanDefinition(
            "choir_annual",
            "choir",
            "year",
            120,
            config.stripe_price_choir_annual,
            config.stripe_product_choir,
        ),
    }


def get_plan(plan_key: PlanKey, config: BillingConfig) -> PlanDefinition:
    return get_plan_catalog(config)[plan_key]


def is_selectable_paid_plan(plan_key: PlanKey, config: BillingConfig) -> bool:
    plan = get_plan(plan_key, config)
    if not plan.is_paid:
        return False
    if plan.is_early_supporter and not config.choir_early_supporter_enabled:
        return False
    return True


def get_plan_for_price_id(price_id: str, config: BillingConfig) -> PlanDefinition | None:
    for plan in get_plan_catalog(config).values():
        if plan.stripe_price_id == price_id:
            return plan
    return None


def get_monthly_allowance(plan_key: PlanKey, config: BillingConfig) -> int:
    return get_plan(plan_key, config).monthly_allowance
