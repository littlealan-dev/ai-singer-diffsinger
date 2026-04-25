from __future__ import annotations

"""Plan catalog and Stripe price mapping for billing."""

from dataclasses import dataclass

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


def get_plan_catalog(config: BillingConfig) -> dict[PlanKey, PlanDefinition]:
    return {
        "free": PlanDefinition("free", "free", "none", 8, None, None),
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
