from __future__ import annotations

"""Firestore helpers for Stripe billing state."""

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_refresh import compute_next_monthly_refresh
from src.backend.billing_types import BillingState, PlanKey
from src.backend.firebase_app import get_firestore_client

ACTIVE_PAID_STATUSES = {"active", "past_due", "unpaid", "incomplete"}


def get_user_data(uid: str) -> dict[str, Any]:
    snapshot = get_firestore_client().collection("users").document(uid).get()
    return snapshot.to_dict() or {}


def get_billing_state(uid: str) -> BillingState:
    data = get_user_data(uid)
    return data.get("billing") or {}


def find_user_id_by_customer_id(stripe_customer_id: str) -> str | None:
    query = (
        get_firestore_client()
        .collection("users")
        .where("billing.stripeCustomerId", "==", stripe_customer_id)
        .limit(1)
        .stream()
    )
    for snapshot in query:
        return snapshot.id
    return None


def has_active_paid_entitlement(billing: BillingState | dict[str, Any] | None) -> bool:
    if not billing:
        return False
    active_plan_key = str(billing.get("activePlanKey") or "free")
    status = billing.get("stripeSubscriptionStatus")
    interval = str(billing.get("billingInterval") or "none")
    return active_plan_key != "free" and interval != "none" and status in ACTIVE_PAID_STATUSES


def persist_checkout_session(uid: str, *, stripe_customer_id: str, checkout_session_id: str) -> None:
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": stripe_customer_id,
                "stripeCheckoutSessionId": checkout_session_id,
            }
        },
        merge=True,
    )


def create_or_update_stripe_event_audit(
    event_id: str,
    *,
    event_type: str,
    payload_summary: dict[str, Any],
    user_id: str | None = None,
    related_stripe_customer_id: str | None = None,
    related_stripe_subscription_id: str | None = None,
    related_stripe_invoice_id: str | None = None,
    related_stripe_checkout_session_id: str | None = None,
    processed: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    get_firestore_client().collection("stripe_events").document(event_id).set(
        {
            "stripeEventId": event_id,
            "type": event_type,
            "processed": processed,
            "processedAt": now if processed else None,
            "relatedStripeCustomerId": related_stripe_customer_id,
            "relatedStripeSubscriptionId": related_stripe_subscription_id,
            "relatedStripeInvoiceId": related_stripe_invoice_id,
            "relatedStripeCheckoutSessionId": related_stripe_checkout_session_id,
            "userId": user_id,
            "payloadSummary": payload_summary,
            "createdAt": now,
        },
        merge=True,
    )


def mark_stripe_event_processed(event_id: str) -> None:
    get_firestore_client().collection("stripe_events").document(event_id).set(
        {
            "processed": True,
            "processedAt": datetime.now(timezone.utc),
        },
        merge=True,
    )


def stripe_event_already_processed(event_id: str) -> bool:
    snapshot = get_firestore_client().collection("stripe_events").document(event_id).get()
    if not snapshot.exists:
        return False
    data = snapshot.to_dict() or {}
    return bool(data.get("processed"))


def free_billing_payload(*, now: datetime, anchor: datetime | None = None) -> dict[str, Any]:
    anchor_value = anchor or now
    return {
        "activePlanKey": "free",
        "stripeSubscriptionStatus": None,
        "family": "free",
        "billingInterval": "none",
        "cancelAtPeriodEnd": False,
        "canceledAt": None,
        "creditRefreshAnchor": anchor_value,
        "lastCreditRefreshAt": now,
        "nextCreditRefreshAt": compute_next_monthly_refresh(anchor_value, now),
        "freeTierActivatedAt": now,
    }


def revert_subscription_to_free(
    uid: str,
    *,
    now: datetime,
    preserve_anchor: datetime | None,
) -> None:
    anchor = preserve_anchor or now
    payload = free_billing_payload(now=now, anchor=anchor)
    payload.update(
        {
            "stripeSubscriptionId": None,
            "stripeCheckoutSessionId": None,
            "currentPeriodStart": None,
            "currentPeriodEnd": None,
        }
    )
    get_firestore_client().collection("users").document(uid).set({"billing": payload}, merge=True)


def upsert_stripe_customer_id(uid: str, stripe_customer_id: str) -> None:
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": stripe_customer_id,
            }
        },
        merge=True,
    )


def sync_paid_subscription_state(
    uid: str,
    *,
    plan_key: PlanKey,
    stripe_subscription_id: str | None,
    stripe_subscription_status: str | None,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    cancel_at_period_end: bool,
    canceled_at: datetime | None,
    is_early_supporter: bool,
) -> None:
    from src.backend.billing_config import get_billing_config
    from src.backend.billing_plans import get_plan

    plan = get_plan(plan_key, get_billing_config())
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "stripeSubscriptionId": stripe_subscription_id,
                "activePlanKey": plan_key,
                "stripeSubscriptionStatus": stripe_subscription_status,
                "family": plan.family,
                "billingInterval": plan.billing_interval,
                "currentPeriodStart": current_period_start,
                "currentPeriodEnd": current_period_end,
                "cancelAtPeriodEnd": cancel_at_period_end,
                "canceledAt": canceled_at,
                "isEarlySupporter": is_early_supporter,
            }
        },
        merge=True,
    )
