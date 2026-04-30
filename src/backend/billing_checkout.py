from __future__ import annotations

"""Stripe Checkout session creation flow."""

from typing import Any

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_plans import get_plan, is_selectable_paid_plan
from src.backend.billing_store import (
    get_billing_state,
    has_active_paid_entitlement,
    persist_checkout_session,
    upsert_stripe_customer_id,
)
from src.backend.billing_types import BillingHttpError, PlanKey


def create_checkout_session(
    uid: str,
    email: str,
    plan_key: PlanKey,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> str:
    billing_config = config or get_billing_config()
    if not is_selectable_paid_plan(plan_key, billing_config):
        raise BillingHttpError(400, "Invalid paid plan.")

    billing = get_billing_state(uid)
    if has_active_paid_entitlement(billing):
        raise BillingHttpError(409, "Active paid subscription already exists.")

    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        customer = client.customers.create(
            params={
                "email": email or None,
                "metadata": {
                    "firebaseUserId": uid,
                    "environment": "app",
                },
            },
        )
        stripe_customer_id = customer.id
        upsert_stripe_customer_id(uid, stripe_customer_id)

    plan = get_plan(plan_key, billing_config)
    session = client.checkout.sessions.create(
        params={
            "mode": "subscription",
            "customer": stripe_customer_id,
            "line_items": [
                {
                    "price": plan.stripe_price_id,
                    "quantity": 1,
                }
            ],
            "success_url": billing_config.checkout_success_url,
            "cancel_url": billing_config.checkout_cancel_url,
            "client_reference_id": uid,
            "metadata": {
                "firebaseUserId": uid,
                "planKey": plan_key,
            },
        },
    )
    persist_checkout_session(
        uid,
        stripe_customer_id=stripe_customer_id,
        checkout_session_id=session.id,
    )
    return str(session.url)
