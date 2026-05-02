from __future__ import annotations

"""Stripe webhook verification and event handlers."""

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_client
from src.backend.billing_plans import get_plan, get_plan_for_price_id
from src.backend.billing_refresh import compute_next_monthly_refresh
from src.backend.billing_store import (
    create_or_update_stripe_event_audit,
    find_user_id_by_customer_id,
    free_billing_payload,
    mark_stripe_event_processed,
    stripe_event_already_processed,
    sync_paid_subscription_state,
)
from src.backend.billing_types import BillingHttpError, PlanKey
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


def construct_stripe_event(
    payload: bytes,
    signature: str,
    *,
    config: BillingConfig | None = None,
) -> Any:
    import stripe

    billing_config = config or get_billing_config()
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=signature,
        secret=billing_config.stripe_webhook_secret,
    )


def handle_event(
    event: Any,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> None:
    event_id = str(event["id"])
    event_type = str(event["type"])
    payload = _to_plain_dict(event["data"]["object"])
    if stripe_event_already_processed(event_id):
        return
    create_or_update_stripe_event_audit(
        event_id,
        event_type=event_type,
        payload_summary=_payload_summary(payload),
        related_stripe_customer_id=_get_string(payload, "customer"),
        related_stripe_subscription_id=_get_string(payload, "subscription"),
        related_stripe_invoice_id=_get_string(payload, "id") if event_type.startswith("invoice.") else None,
        related_stripe_checkout_session_id=_get_string(payload, "id") if event_type == "checkout.session.completed" else None,
        user_id=_resolve_user_id(payload),
        processed=False,
    )

    if event_type == "checkout.session.completed":
        _handle_checkout_session_completed(payload)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(payload, config=config)
    elif event_type == "invoice.payment_failed":
        _handle_invoice_payment_failed(payload)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(payload, config=config)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(payload)
    else:
        logger.info("Ignoring unsupported Stripe event type=%s", event_type)
    mark_stripe_event_processed(event_id)


def _handle_checkout_session_completed(payload: dict[str, Any]) -> None:
    uid = _resolve_user_id(payload)
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for checkout session.")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": payload.get("customer"),
                "stripeSubscriptionId": payload.get("subscription"),
                "stripeCheckoutSessionId": payload.get("id"),
            }
        },
        merge=True,
    )


def _handle_invoice_paid(payload: dict[str, Any], *, config: BillingConfig | None = None) -> None:
    billing_config = config or get_billing_config()
    uid = _resolve_user_id(payload)
    if not uid:
        customer_id = _get_string(payload, "customer")
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for invoice.")

    invoice_id = str(payload["id"])
    line_items = ((payload.get("lines") or {}).get("data") or [])
    price_id = None
    for line in line_items:
        price = line.get("price") or {}
        price_id = price.get("id")
        if price_id:
            break
    if not price_id:
        logger.warning("invoice.paid missing recurring price id invoice=%s", invoice_id)
        return
    plan = get_plan_for_price_id(str(price_id), billing_config)
    if plan is None:
        logger.warning("invoice.paid unknown price invoice=%s price=%s", invoice_id, price_id)
        return
    now = _to_utc(_timestamp_to_datetime(payload.get("status_transitions", {}).get("paid_at")) or datetime.now(timezone.utc))
    _apply_paid_invoice(
        uid,
        invoice_id=invoice_id,
        invoice_paid_at=now,
        plan_key=plan.key,
        stripe_subscription_id=_get_string(payload, "subscription"),
    )


def _handle_invoice_payment_failed(payload: dict[str, Any]) -> None:
    uid = _resolve_user_id(payload)
    if not uid:
        customer_id = _get_string(payload, "customer")
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for failed invoice.")
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "latestInvoiceId": payload.get("id"),
                "latestInvoicePaymentFailedAt": datetime.now(timezone.utc),
            }
        },
        merge=True,
    )


def _handle_subscription_updated(payload: dict[str, Any], *, config: BillingConfig | None = None) -> None:
    billing_config = config or get_billing_config()
    uid = _resolve_user_id(payload)
    if not uid:
        customer_id = _get_string(payload, "customer")
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for subscription.")
    plan_key = _plan_key_from_subscription(payload, billing_config)
    if plan_key is None:
        return
    sync_paid_subscription_state(
        uid,
        plan_key=plan_key,
        stripe_subscription_id=_get_string(payload, "id"),
        stripe_subscription_status=_get_string(payload, "status"),
        current_period_start=_subscription_period_datetime(payload, "current_period_start"),
        current_period_end=_subscription_period_datetime(payload, "current_period_end"),
        cancel_at_period_end=_has_scheduled_cancel(payload),
        canceled_at=_timestamp_to_datetime(payload.get("canceled_at")),
        is_early_supporter=plan_key.startswith("choir_early"),
        billing_cycle_anchor=_timestamp_to_datetime(payload.get("billing_cycle_anchor")),
    )


def _handle_subscription_deleted(payload: dict[str, Any]) -> None:
    uid = _resolve_user_id(payload)
    if not uid:
        customer_id = _get_string(payload, "customer")
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for deleted subscription.")
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    snapshot = user_ref.get()
    data = snapshot.to_dict() or {}
    billing = data.get("billing") or {}
    anchor = billing.get("creditRefreshAnchor") or datetime.now(timezone.utc)
    free_payload = free_billing_payload(now=datetime.now(timezone.utc), anchor=_to_utc(anchor))
    free_payload.update(
        {
            "stripeCustomerId": billing.get("stripeCustomerId"),
            "stripeSubscriptionId": None,
            "stripeCheckoutSessionId": billing.get("stripeCheckoutSessionId"),
        }
    )
    user_ref.set({"billing": free_payload}, merge=True)


def _apply_paid_invoice(
    uid: str,
    *,
    invoice_id: str,
    invoice_paid_at: datetime,
    plan_key: PlanKey,
    stripe_subscription_id: str | None,
) -> None:
    db = get_firestore_client()
    config = get_billing_config()
    plan = get_plan(plan_key, config)
    user_ref = db.collection("users").document(uid)
    ledger_ref = db.collection("credit_ledger").document(f"grant_invoice_{invoice_id}")

    @firestore.transactional
    def _transaction(transaction):
        ledger_snapshot = ledger_ref.get(transaction=transaction)
        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            raise BillingHttpError(404, "User not found for invoice grant.")
        data = user_snapshot.to_dict() or {}
        billing = data.get("billing") or {}
        credits = data.get("credits") or {}
        was_free = str(billing.get("activePlanKey") or "free") == "free"
        anchor = billing.get("creditRefreshAnchor")
        reserved = int(credits.get("reserved", 0) or 0)

        base_billing_update = {
            "billing.activePlanKey": plan_key,
            "billing.family": plan.family,
            "billing.billingInterval": plan.billing_interval,
            "billing.stripeSubscriptionId": stripe_subscription_id,
            "billing.latestInvoiceId": invoice_id,
            "billing.latestInvoicePaidAt": invoice_paid_at,
            "billing.stripeSubscriptionStatus": billing.get("stripeSubscriptionStatus") or "active",
        }
        transaction.update(user_ref, base_billing_update)
        if ledger_snapshot.exists:
            return

        if reserved > 0:
            due_at = billing.get("nextCreditRefreshAt") or invoice_paid_at
            transaction.update(
                user_ref,
                {
                    "billing.nextCreditRefreshAt": due_at if due_at <= invoice_paid_at else invoice_paid_at,
                },
            )
            return

        grant_time = invoice_paid_at
        effective_anchor = grant_time if was_free or anchor is None else anchor
        next_refresh = compute_next_monthly_refresh(_to_utc(effective_anchor), grant_time)
        transaction.update(
            user_ref,
            {
                "credits.balance": plan.monthly_allowance,
                "credits.monthlyAllowance": plan.monthly_allowance,
                "credits.lastGrantType": "grant_paid_subscription_cycle",
                "credits.lastGrantAt": grant_time,
                "credits.lastGrantInvoiceId": invoice_id,
                "billing.creditRefreshAnchor": effective_anchor,
                "billing.lastCreditRefreshAt": grant_time,
                "billing.nextCreditRefreshAt": next_refresh,
            },
        )
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "type": "grant_paid_subscription_cycle",
                "amount": plan.monthly_allowance,
                "balanceAfter": plan.monthly_allowance,
                "createdAt": grant_time,
                "stripeInvoiceId": invoice_id,
                "planKey": plan_key,
            },
        )

    _transaction(db.transaction())


def _plan_key_from_subscription(payload: dict[str, Any], config: BillingConfig) -> PlanKey | None:
    items = ((payload.get("items") or {}).get("data") or [])
    for item in items:
        price = item.get("price") or {}
        price_id = price.get("id")
        if not price_id:
            continue
        plan = get_plan_for_price_id(str(price_id), config)
        if plan is not None:
            return plan.key
    return None


def _resolve_user_id(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata") or {}
    client_reference_id = payload.get("client_reference_id")
    if isinstance(client_reference_id, str) and client_reference_id.strip():
        return client_reference_id.strip()
    firebase_user_id = metadata.get("firebaseUserId")
    if isinstance(firebase_user_id, str) and firebase_user_id.strip():
        return firebase_user_id.strip()
    customer_id = payload.get("customer")
    if isinstance(customer_id, str) and customer_id.strip():
        return find_user_id_by_customer_id(customer_id.strip())
    return None


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "customer": payload.get("customer"),
        "subscription": payload.get("subscription"),
        "status": payload.get("status"),
        "billing_cycle_anchor": payload.get("billing_cycle_anchor"),
        "cancel_at": payload.get("cancel_at"),
        "cancel_at_period_end": payload.get("cancel_at_period_end"),
        "canceled_at": payload.get("canceled_at"),
        "current_period_end": payload.get("current_period_end"),
    }


def _has_scheduled_cancel(payload: dict[str, Any]) -> bool:
    if bool(payload.get("cancel_at_period_end", False)):
        return True
    status = _get_string(payload, "status")
    return status not in {"canceled", "incomplete_expired"} and payload.get("cancel_at") is not None


def _get_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _timestamp_to_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


def _subscription_period_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    value = _timestamp_to_datetime(payload.get(key))
    if value is not None:
        return value
    items = ((payload.get("items") or {}).get("data") or [])
    for item in items:
        value = _timestamp_to_datetime((item or {}).get(key))
        if value is not None:
            return value
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
