from __future__ import annotations

"""Environment-backed Stripe billing configuration."""

from dataclasses import dataclass
from functools import lru_cache
import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class BillingConfig:
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_product_solo: str
    stripe_product_choir: str
    stripe_price_solo_monthly: str
    stripe_price_solo_annual: str
    stripe_price_choir_early_monthly: str
    stripe_price_choir_early_annual: str
    stripe_price_choir_monthly: str
    stripe_price_choir_annual: str
    choir_early_supporter_enabled: bool
    checkout_success_url: str
    checkout_cancel_url: str
    portal_return_url: str
    portal_configuration_id: str
    stripe_api_version: str


@dataclass(frozen=True)
class BillingRefreshConfig:
    schedule: str
    max_due_users: int
    timeout_seconds: int
    metrics_enabled: bool


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not value:
        raise ValueError(f"Missing required billing env var: {name}")
    return value


@lru_cache(maxsize=1)
def get_billing_config() -> BillingConfig:
    return BillingConfig(
        stripe_secret_key=_required_env("STRIPE_SECRET_KEY"),
        stripe_webhook_secret=_required_env("STRIPE_WEBHOOK_SECRET"),
        stripe_product_solo=_required_env("STRIPE_PRODUCT_SOLO"),
        stripe_product_choir=_required_env("STRIPE_PRODUCT_CHOIR"),
        stripe_price_solo_monthly=_required_env("STRIPE_PRICE_SOLO_MONTHLY"),
        stripe_price_solo_annual=_required_env("STRIPE_PRICE_SOLO_ANNUAL"),
        stripe_price_choir_early_monthly=_required_env("STRIPE_PRICE_CHOIR_EARLY_MONTHLY"),
        stripe_price_choir_early_annual=_required_env("STRIPE_PRICE_CHOIR_EARLY_ANNUAL"),
        stripe_price_choir_monthly=_required_env("STRIPE_PRICE_CHOIR_MONTHLY"),
        stripe_price_choir_annual=_required_env("STRIPE_PRICE_CHOIR_ANNUAL"),
        choir_early_supporter_enabled=_env_bool("CHOIR_EARLY_SUPPORTER_ENABLED", False),
        checkout_success_url=_required_env("STRIPE_CHECKOUT_SUCCESS_URL"),
        checkout_cancel_url=_required_env("STRIPE_CHECKOUT_CANCEL_URL"),
        portal_return_url=_required_env("STRIPE_PORTAL_RETURN_URL"),
        portal_configuration_id=_required_env("STRIPE_PORTAL_CONFIGURATION_ID"),
        stripe_api_version=os.getenv("STRIPE_API_VERSION", "2026-02-25.clover").strip(),
    )


@lru_cache(maxsize=1)
def get_billing_refresh_config() -> BillingRefreshConfig:
    max_due_users = _env_int("BILLING_REFRESH_MAX_DUE_USERS", 300)
    if max_due_users < 1 or max_due_users > 1000:
        raise ValueError("BILLING_REFRESH_MAX_DUE_USERS must be between 1 and 1000.")
    timeout_seconds = _env_int("BILLING_REFRESH_TIMEOUT_SECONDS", 300)
    if timeout_seconds < 30 or timeout_seconds > 540:
        raise ValueError("BILLING_REFRESH_TIMEOUT_SECONDS must be between 30 and 540.")
    return BillingRefreshConfig(
        schedule=os.getenv("BILLING_REFRESH_SCHEDULE", "every 2 hours").strip() or "every 2 hours",
        max_due_users=max_due_users,
        timeout_seconds=timeout_seconds,
        metrics_enabled=_env_bool("BILLING_REFRESH_METRICS_ENABLED", True),
    )


@lru_cache(maxsize=1)
def get_stripe_client():
    config = get_billing_config()
    import stripe

    stripe.api_key = config.stripe_secret_key
    if hasattr(stripe, "api_version"):
        stripe.api_version = config.stripe_api_version
    if hasattr(stripe, "StripeClient"):
        try:
            return stripe.StripeClient(config.stripe_secret_key)
        except TypeError:
            return stripe.StripeClient(config.stripe_secret_key, stripe_version=config.stripe_api_version)
    return stripe


def get_stripe_v1_client():
    client = get_stripe_client()
    v1 = getattr(client, "v1", None)
    return v1 or client
