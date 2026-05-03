from __future__ import annotations

"""Authenticated Checkout return reconciliation.

Stripe webhooks remain the primary billing update path. This module provides a
same-user fallback for the hosted Checkout success redirect so local development
and delayed webhooks do not leave the UI permanently waiting for Firestore sync.
"""

from datetime import datetime, timezone
from typing import Any

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_plans import get_plan_for_price_id
from src.backend.billing_store import get_billing_state, sync_paid_subscription_state
from src.backend.billing_types import BillingHttpError
from src.backend.billing_webhooks import _apply_paid_invoice
from src.backend.firebase_app import get_firestore_client

SYNCABLE_SUBSCRIPTION_STATUSES = {"active", "trialing", "past_due"}


def sync_checkout_session(
    uid: str,
    checkout_session_id: str,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, str | bool | None]:
    session_id = checkout_session_id.strip()
    if not session_id:
        raise BillingHttpError(400, "Missing Checkout session id.")

    billing_config = config or get_billing_config()
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    session = _to_plain_dict(
        client.checkout.sessions.retrieve(
            session_id,
            params={
                "expand": [
                    "subscription",
                    "subscription.items.data.price",
                    "subscription.latest_invoice",
                ]
            },
        )
    )
    billing = get_billing_state(uid)
    _assert_session_belongs_to_user(uid, session, billing)
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "latestCheckoutSessionStatus": session.get("status"),
                "latestCheckoutPaymentStatus": session.get("payment_status"),
            }
        },
        merge=True,
    )

    if session.get("mode") != "subscription":
        raise BillingHttpError(409, "Checkout session is not a subscription checkout.")
    if session.get("status") != "complete" or session.get("payment_status") != "paid":
        return {"synced": False, "status": str(session.get("status") or "open"), "activePlanKey": None}

    subscription = _subscription_payload(client, session)
    subscription_status = _get_string(subscription, "status")
    if subscription_status not in SYNCABLE_SUBSCRIPTION_STATUSES:
        raise BillingHttpError(409, "Checkout subscription is not active.")
    plan_key = _plan_key_from_subscription(subscription, billing_config)
    if plan_key is None:
        raise BillingHttpError(409, "Checkout subscription does not match a configured plan.")

    sync_paid_subscription_state(
        uid,
        plan_key=plan_key,
        stripe_subscription_id=_get_string(subscription, "id"),
        stripe_subscription_status=subscription_status,
        current_period_start=_subscription_period_datetime(subscription, "current_period_start"),
        current_period_end=_subscription_period_datetime(subscription, "current_period_end"),
        cancel_at_period_end=_has_scheduled_cancel(subscription),
        canceled_at=_timestamp_to_datetime(subscription.get("canceled_at")),
        is_early_supporter=plan_key.startswith("choir_early"),
        billing_cycle_anchor=_timestamp_to_datetime(subscription.get("billing_cycle_anchor")),
    )

    invoice = _invoice_payload(subscription)
    invoice_id = _get_string(invoice, "id")
    if invoice_id:
        paid_at = _timestamp_to_datetime((invoice.get("status_transitions") or {}).get("paid_at")) or _timestamp_to_datetime(
            invoice.get("created")
        ) or datetime.now(timezone.utc)
        _apply_paid_invoice(
            uid,
            invoice_id=invoice_id,
            invoice_paid_at=paid_at,
            plan_key=plan_key,
            stripe_subscription_id=_get_string(subscription, "id"),
            invoice_status=_get_string(invoice, "status") or "paid",
            payment_intent_status=_payment_intent_status(invoice),
        )

    return {
        "synced": True,
        "status": str(session.get("status") or "complete"),
        "activePlanKey": plan_key,
    }


def _assert_session_belongs_to_user(uid: str, session: dict[str, Any], billing: dict[str, Any]) -> None:
    metadata = session.get("metadata") or {}
    session_user_id = session.get("client_reference_id") or metadata.get("firebaseUserId")
    stored_session_id = billing.get("stripeCheckoutSessionId")
    stored_customer_id = billing.get("stripeCustomerId")
    session_customer_id = session.get("customer")
    if session_user_id != uid:
        raise BillingHttpError(403, "Checkout session does not belong to the current user.")
    if stored_session_id and stored_session_id != session.get("id"):
        raise BillingHttpError(403, "Checkout session does not belong to the current user.")
    if stored_customer_id and session_customer_id and stored_customer_id != session_customer_id:
        raise BillingHttpError(403, "Checkout session does not belong to the current user.")
    if stored_session_id == session.get("id") or (
        stored_customer_id and session_customer_id and stored_customer_id == session_customer_id
    ):
        return
    raise BillingHttpError(403, "Checkout session does not belong to the current user.")


def _subscription_payload(client: Any, session: dict[str, Any]) -> dict[str, Any]:
    subscription = session.get("subscription")
    if isinstance(subscription, dict):
        return subscription
    if isinstance(subscription, str) and subscription:
        return _to_plain_dict(
            client.subscriptions.retrieve(
                subscription,
                params={"expand": ["items.data.price", "latest_invoice"]},
            )
        )
    raise BillingHttpError(409, "Checkout session has no subscription.")


def _invoice_payload(subscription: dict[str, Any]) -> dict[str, Any]:
    invoice = subscription.get("latest_invoice")
    if isinstance(invoice, dict):
        return invoice
    if isinstance(invoice, str) and invoice:
        return {"id": invoice}
    return {}


def _payment_intent_status(invoice: dict[str, Any]) -> str | None:
    payment_intent = invoice.get("payment_intent")
    if isinstance(payment_intent, dict):
        return _get_string(payment_intent, "status")
    return None


def _plan_key_from_subscription(subscription: dict[str, Any], config: BillingConfig) -> str | None:
    items = ((subscription.get("items") or {}).get("data") or [])
    for item in items:
        price = item.get("price") or {}
        price_id = price.get("id")
        if not price_id:
            continue
        plan = get_plan_for_price_id(str(price_id), config)
        if plan is not None:
            return plan.key
    return None


def _timestamp_to_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


def _subscription_period_datetime(subscription: dict[str, Any], key: str) -> datetime | None:
    value = _timestamp_to_datetime(subscription.get(key))
    if value is not None:
        return value
    items = ((subscription.get("items") or {}).get("data") or [])
    for item in items:
        value = _timestamp_to_datetime((item or {}).get(key))
        if value is not None:
            return value
    return None


def _get_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _has_scheduled_cancel(subscription: dict[str, Any]) -> bool:
    if bool(subscription.get("cancel_at_period_end", False)):
        return True
    status = _get_string(subscription, "status")
    return status not in {"canceled", "incomplete_expired"} and subscription.get("cancel_at") is not None


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    to_dict_recursive = getattr(value, "to_dict_recursive", None)
    if callable(to_dict_recursive):
        result = to_dict_recursive()
        if isinstance(result, dict):
            return result
    private_recursive = getattr(value, "_to_dict_recursive", None)
    if callable(private_recursive):
        result = private_recursive()
        if isinstance(result, dict):
            return result
    return dict(value)
