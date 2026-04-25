from __future__ import annotations

"""Stripe Billing Portal session creation flow."""

from typing import Any

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_store import get_billing_state
from src.backend.billing_types import BillingHttpError


def create_portal_session(
    uid: str,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> str:
    billing = get_billing_state(uid)
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        raise BillingHttpError(409, "Stripe customer is not set up for this account.")
    billing_config = config or get_billing_config()
    client = stripe_client or get_stripe_v1_client()
    session = client.billing_portal.sessions.create(
        params={
            "customer": stripe_customer_id,
            "return_url": billing_config.portal_return_url,
            "configuration": billing_config.portal_configuration_id,
        },
    )
    return str(session.url)
