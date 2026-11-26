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


FREE_PLAN = PlanDefinition("free", "free", "none", 8, None, None)


def get_free_plan() -> PlanDefinition:
    return FREE_PLAN


def get_plan_catalog(config: BillingConfig) -> dict[PlanKey, PlanDefinition]:
    return {
        "free": FREE_PLAN,
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
        "max_early_monthly": PlanDefinition(
            "max_early_monthly",
            "max",
            "month",
            400,
            config.stripe_price_max_early_monthly or None,
            config.stripe_product_max or None,
            True,
        ),
        "max_early_annual": PlanDefinition(
            "max_early_annual",
            "max",
            "year",
            400,
            config.stripe_price_max_early_annual or None,
            config.stripe_product_max or None,
            True,
        ),
    }


def get_plan(plan_key: PlanKey, config: BillingConfig) -> PlanDefinition:
    return get_plan_catalog(config)[plan_key]


def is_selectable_paid_plan(plan_key: PlanKey, config: BillingConfig) -> bool:
    plan = get_plan(plan_key, config)
    if not plan.is_paid or not plan.stripe_price_id:
        return False
    if plan.is_early_supporter:
        # Each family's founding offer has its own sale switch.
        if plan.family == "max":
            return config.max_early_supporter_enabled
        return config.choir_early_supporter_enabled
    return True


def is_on_sale_plan(plan_key: PlanKey, config: BillingConfig) -> bool:
    """Whether a plan card shows this plan, so a subscriber may switch to it.

    Checkout also takes the regular Pro prices while the founding Pro price is on sale; a plan
    switch goes only to the price the card shows.
    """
    if not is_selectable_paid_plan(plan_key, config):
        return False
    plan = get_plan(plan_key, config)
    if plan.family == "choir" and not plan.is_early_supporter:
        return not config.choir_early_supporter_enabled
    return True


def get_plan_for_price_id(price_id: str, config: BillingConfig) -> PlanDefinition | None:
    for plan in get_plan_catalog(config).values():
        if plan.stripe_price_id == price_id:
            return plan
    return None


def get_monthly_allowance(plan_key: PlanKey, config: BillingConfig) -> int:
    return get_plan(plan_key, config).monthly_allowance
