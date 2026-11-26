from __future__ import annotations

"""Self-serve plan switching through a Stripe Customer Portal deep link.

The portal shows the prorated amount, takes payment and applies the change, or books it for the
renewal date with a subscription schedule. This module opens that confirm page for one target
plan, and keeps a booked change on the billing doc so the app can show and undo it.
"""

from datetime import datetime, timezone
from typing import Any

import stripe
from google.cloud import firestore

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_plans import get_plan, get_plan_for_price_id, is_on_sale_plan
from src.backend.billing_portal import portal_return_url
from src.backend.billing_store import get_billing_state
from src.backend.billing_subscription_sync import (
    _list_customer_subscriptions,
    _select_relevant_subscription,
    _to_plain_dict,
)
from src.backend.billing_types import BillingHttpError
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

# A plan can change only while the subscription is paid up: Stripe prorates as if unpaid
# invoices will be paid, so a past_due user would get credit for time they never paid for.
CHANGEABLE_STATUSES = {"active", "trialing"}
OPEN_SCHEDULE_STATUSES = {"not_started", "active"}


def create_plan_change_session(
    uid: str,
    plan_key: str,
    *,
    return_path: str | None = None,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> str:
    billing_config = config or get_billing_config()
    billing = get_billing_state(uid)
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        raise BillingHttpError(409, "No subscription to change.")
    try:
        target = get_plan(plan_key, billing_config)  # type: ignore[arg-type]
    except KeyError as exc:
        raise BillingHttpError(400, "That plan isn't on sale.") from exc
    if not is_on_sale_plan(target.key, billing_config):
        raise BillingHttpError(400, "That plan isn't on sale.")

    client = _v1(stripe_client)
    subscription = _select_relevant_subscription(
        _list_customer_subscriptions(client, stripe_customer_id),
        billing.get("stripeSubscriptionId"),
    )
    if subscription is None:
        raise BillingHttpError(409, "No subscription to change.")
    if subscription.get("status") not in CHANGEABLE_STATUSES:
        raise BillingHttpError(409, "Fix your payment in Manage Billing first.")
    # A subscription set to cancel needs no check: the portal's confirm page renews it.
    if subscription.get("schedule"):
        raise BillingHttpError(409, "You already have a plan change booked. Undo it first.")
    items = ((subscription.get("items") or {}).get("data") or [])
    if len(items) != 1:
        raise BillingHttpError(409, "This subscription can't be changed here.")
    item = items[0]
    current_price_id = (item.get("price") or {}).get("id")
    current = get_plan_for_price_id(str(current_price_id), billing_config) if current_price_id else None
    if current is not None and (current.family, current.billing_interval) == (
        target.family,
        target.billing_interval,
    ):
        raise BillingHttpError(400, "That's your current plan.")

    try:
        session = client.billing_portal.sessions.create(
            params={
                "customer": stripe_customer_id,
                "configuration": billing_config.portal_configuration_id,
                # The portal's "back" link: the user gave up, so no change=done.
                "return_url": portal_return_url(billing_config, return_path),
                "flow_data": {
                    "type": "subscription_update_confirm",
                    "subscription_update_confirm": {
                        "subscription": subscription["id"],
                        "items": [{"id": item["id"], "price": target.stripe_price_id, "quantity": 1}],
                    },
                    "after_completion": {
                        "type": "redirect",
                        "redirect": {"return_url": portal_return_url(billing_config, return_path, change="done")},
                    },
                },
            },
        )
    except stripe.InvalidRequestError as exc:
        # Stripe refuses a price its portal configuration doesn't list (spec §4.1, by hand).
        logger.error("plan_change_portal_refused plan=%s error=%s", target.key, exc)
        raise BillingHttpError(409, "That plan change isn't available right now. Please contact support.") from exc
    return str(session.url)


def cancel_scheduled_change(
    uid: str,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, bool]:
    """Undo a booked plan change: release the schedule, so the subscription keeps its plan."""
    billing = get_billing_state(uid)
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        raise BillingHttpError(409, "No subscription to change.")
    client = _v1(stripe_client)
    subscription = _select_relevant_subscription(
        _list_customer_subscriptions(client, stripe_customer_id),
        billing.get("stripeSubscriptionId"),
    )
    schedule_id = _stripe_id((subscription or {}).get("schedule"))
    if schedule_id:
        client.subscription_schedules.release(schedule_id)
    clear_scheduled_change(uid)
    return {"released": bool(schedule_id)}


def scheduled_change_from_schedule(
    schedule: dict[str, Any],
    config: BillingConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The plan a schedule moves to next, and when: None when nothing different is booked."""
    if schedule.get("status") not in OPEN_SCHEDULE_STATUSES:
        return None
    current_time = now or datetime.now(timezone.utc)
    phases = sorted(
        (phase for phase in schedule.get("phases") or [] if _phase_start(phase) is not None),
        key=lambda phase: _phase_start(phase),  # type: ignore[arg-type, return-value]
    )
    current_price_id = None
    for phase in phases:
        if _phase_start(phase) <= current_time:  # type: ignore[operator]
            current_price_id = _phase_price_id(phase)
    if current_price_id is None and phases:
        # A schedule that hasn't started yet: its first phase is the plan the user has now.
        current_price_id = _phase_price_id(phases[0])
    for phase in phases:
        starts_at = _phase_start(phase)
        if starts_at <= current_time:  # type: ignore[operator]
            continue
        price_id = _phase_price_id(phase)
        if not price_id or price_id == current_price_id:
            continue
        plan = get_plan_for_price_id(price_id, config)
        if plan is None:
            return None
        return {"planKey": plan.key, "at": starts_at, "scheduleId": schedule.get("id")}
    return None


def store_scheduled_change(
    uid: str,
    schedule: dict[str, Any],
    *,
    config: BillingConfig | None = None,
    now: datetime | None = None,
) -> None:
    """Keep the booked change from a schedule event on the billing doc.

    Schedule events can arrive out of order, so an ended schedule clears the stored change only
    when it is the one stored.
    """
    change = scheduled_change_from_schedule(schedule, config or get_billing_config(), now=now)
    schedule_id = schedule.get("id")
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)

    @firestore.transactional
    def _transaction(transaction):
        snapshot = user_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        stored = ((snapshot.to_dict() or {}).get("billing") or {}).get("scheduledChange")
        if change is None and isinstance(stored, dict) and stored.get("scheduleId") != schedule_id:
            return
        transaction.update(user_ref, {"billing.scheduledChange": change})

    _transaction(db.transaction())


def clear_scheduled_change(uid: str) -> None:
    user_ref = get_firestore_client().collection("users").document(uid)
    if user_ref.get().exists:
        user_ref.update({"billing.scheduledChange": None})


def sync_scheduled_change(
    uid: str,
    subscription: dict[str, Any],
    client: Any,
    config: BillingConfig,
) -> None:
    """Make the stored booked change match the subscription's schedule, for portal returns."""
    schedule_id = _stripe_id(subscription.get("schedule"))
    if not schedule_id:
        clear_scheduled_change(uid)
        return
    schedule = _to_plain_dict(client.subscription_schedules.retrieve(schedule_id))
    store_scheduled_change(uid, schedule, config=config)


def _phase_start(phase: dict[str, Any]) -> datetime | None:
    value = phase.get("start_date")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


def _phase_price_id(phase: dict[str, Any]) -> str | None:
    items = phase.get("items") or []
    if not items:
        return None
    return _stripe_id((items[0] or {}).get("price"))


def _stripe_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        identifier = value.get("id")
        return identifier if isinstance(identifier, str) and identifier else None
    return None


def _v1(stripe_client: Any | None) -> Any:
    raw_client = stripe_client or get_stripe_v1_client()
    return getattr(raw_client, "v1", None) or raw_client
