from __future__ import annotations

"""Authenticated Stripe subscription reconciliation for Billing Portal returns."""

from datetime import datetime, timezone
from typing import Any

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_plans import get_plan_for_price_id
from src.backend.billing_store import get_billing_state, revert_subscription_to_free, sync_paid_subscription_state
from src.backend.billing_types import BillingHttpError

SYNCABLE_PAID_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete"}
FREE_STATUSES = {"canceled", "incomplete_expired"}


def sync_current_subscription(
    uid: str,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, str | bool | None]:
    billing_config = config or get_billing_config()
    billing = get_billing_state(uid)
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        raise BillingHttpError(409, "Stripe customer is not set up for this account.")

    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    subscriptions = _list_customer_subscriptions(client, stripe_customer_id)
    subscription = _select_relevant_subscription(subscriptions, billing.get("stripeSubscriptionId"))
    if subscription is None:
        _revert_to_free(uid, billing)
        return {"synced": True, "status": "free", "activePlanKey": "free"}

    status = _get_string(subscription, "status")
    if status in FREE_STATUSES:
        _revert_to_free(uid, billing)
        return {"synced": True, "status": status, "activePlanKey": "free"}
    if status not in SYNCABLE_PAID_STATUSES:
        raise BillingHttpError(409, "Stripe subscription status is not supported.")

    plan_key = _plan_key_from_subscription(subscription, billing_config)
    if plan_key is None:
        raise BillingHttpError(409, "Stripe subscription does not match a configured plan.")

    sync_paid_subscription_state(
        uid,
        plan_key=plan_key,
        stripe_subscription_id=_get_string(subscription, "id"),
        stripe_subscription_status=status,
        current_period_start=_subscription_period_datetime(subscription, "current_period_start"),
        current_period_end=_subscription_period_datetime(subscription, "current_period_end"),
        cancel_at_period_end=_has_scheduled_cancel(subscription),
        canceled_at=_timestamp_to_datetime(subscription.get("canceled_at")),
        is_early_supporter=plan_key.startswith("choir_early"),
        billing_cycle_anchor=_timestamp_to_datetime(subscription.get("billing_cycle_anchor")),
    )
    return {"synced": True, "status": status, "activePlanKey": plan_key}


def _list_customer_subscriptions(client: Any, stripe_customer_id: str) -> list[dict[str, Any]]:
    result = client.subscriptions.list(
        params={
            "customer": stripe_customer_id,
            "status": "all",
            "limit": 10,
            "expand": ["data.items.data.price"],
        }
    )
    payload = _to_plain_dict(result)
    data = payload.get("data") or []
    return [_to_plain_dict(item) for item in data]


def _select_relevant_subscription(
    subscriptions: list[dict[str, Any]],
    current_subscription_id: Any,
) -> dict[str, Any] | None:
    if not subscriptions:
        return None
    current_id = current_subscription_id if isinstance(current_subscription_id, str) else None
    paid = [sub for sub in subscriptions if sub.get("status") in SYNCABLE_PAID_STATUSES]
    if current_id:
        for subscription in subscriptions:
            if subscription.get("id") == current_id and subscription.get("status") in SYNCABLE_PAID_STATUSES:
                return subscription
    if paid:
        return max(paid, key=_subscription_sort_key)
    if current_id:
        for subscription in subscriptions:
            if subscription.get("id") == current_id:
                return subscription
    return max(subscriptions, key=_subscription_sort_key)


def _subscription_sort_key(subscription: dict[str, Any]) -> tuple[float, str]:
    timestamp = subscription.get("created") or subscription.get("current_period_start") or 0
    if not isinstance(timestamp, (int, float)):
        timestamp = 0
    return float(timestamp), str(subscription.get("id") or "")


def _revert_to_free(uid: str, billing: dict[str, Any]) -> None:
    anchor = _timestamp_to_datetime(billing.get("creditRefreshAnchor")) or datetime.now(timezone.utc)
    revert_subscription_to_free(uid, now=datetime.now(timezone.utc), preserve_anchor=anchor)


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
    return status not in FREE_STATUSES and subscription.get("cancel_at") is not None


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
