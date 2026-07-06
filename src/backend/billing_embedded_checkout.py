from __future__ import annotations

"""Stripe Embedded Checkout session creation."""

from typing import Any

from src.backend.billing_checkout import create_embedded_subscription_checkout_session
from src.backend.billing_topup import TOPUP_PACK_KEY, create_embedded_topup_checkout_session
from src.backend.billing_types import BillingHttpError


def create_embedded_checkout_session(
    uid: str,
    email: str,
    checkout_type: str,
    *,
    plan_key: str | None = None,
    pack_key: str | None = None,
    config: Any | None = None,
    stripe_client: Any | None = None,
) -> dict[str, Any]:
    if checkout_type == "topup":
        return create_embedded_topup_checkout_session(
            uid,
            email,
            pack_key or TOPUP_PACK_KEY,
            config=config,
            stripe_client=stripe_client,
        )
    if checkout_type == "subscription":
        if not plan_key:
            raise BillingHttpError(400, "Missing paid plan.")
        return create_embedded_subscription_checkout_session(
            uid,
            email,
            plan_key,
            config=config,
            stripe_client=stripe_client,
        )
    raise BillingHttpError(400, "Invalid checkout type.")

