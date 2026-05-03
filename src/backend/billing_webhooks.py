from __future__ import annotations

"""Stripe webhook verification and event handlers."""

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_client, get_stripe_v1_client
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
        related_stripe_customer_id=_customer_id_from_payload(payload),
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
    elif event_type in {"charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed"}:
        _handle_dispute_event(event_type, payload)
    elif event_type in {"refund.created", "refund.updated", "refund.failed", "charge.refunded"}:
        _handle_refund_event(event_type, payload, stripe_client=stripe_client)
    else:
        logger.info("Ignoring unsupported Stripe event type=%s", event_type)
    mark_stripe_event_processed(event_id)


def _handle_checkout_session_completed(payload: dict[str, Any]) -> None:
    uid = _resolve_user_id(payload)
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for checkout session.")
    billing_payload = {
        "stripeCustomerId": payload.get("customer"),
        "stripeCheckoutSessionId": payload.get("id"),
        "latestCheckoutSessionStatus": payload.get("status"),
        "latestCheckoutPaymentStatus": payload.get("payment_status"),
    }
    if payload.get("payment_status") == "paid":
        billing_payload["stripeSubscriptionId"] = payload.get("subscription")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {
            "billing": billing_payload
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
        invoice_status=_get_string(payload, "status") or "paid",
        payment_intent_status=_payment_intent_status(payload),
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
                "latestInvoiceStatus": payload.get("status"),
                "latestInvoicePaymentFailedAt": datetime.now(timezone.utc),
                "latestPaymentIntentStatus": _payment_intent_status(payload),
                "latestPaymentFailureCode": _payment_failure_code(payload),
                "latestPaymentFailureMessage": _payment_failure_message(payload),
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


def _handle_dispute_event(event_type: str, payload: dict[str, Any]) -> None:
    uid = _resolve_user_id(payload)
    if not uid:
        customer_id = _customer_id_from_payload(payload)
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        logger.warning("Unable to resolve Firebase user for Stripe dispute id=%s", payload.get("id"))
        return

    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "latestDisputeId": payload.get("id"),
                "latestDisputeStatus": payload.get("status"),
                "latestDisputeReason": payload.get("reason"),
                "latestDisputeCreatedAt": _timestamp_to_datetime(payload.get("created")) or datetime.now(timezone.utc),
                "latestDisputeAmount": payload.get("amount"),
                "latestDisputeCurrency": payload.get("currency"),
                "latestDisputeEventType": event_type,
                "latestDisputeChargeId": _stripe_id_from_expandable(payload.get("charge")),
                "latestDisputePaymentIntentId": _stripe_id_from_expandable(payload.get("payment_intent")),
            }
        },
        merge=True,
    )


def _handle_refund_event(event_type: str, payload: dict[str, Any], *, stripe_client: Any | None = None) -> None:
    refund_payload = _refund_payload_from_event(event_type, payload)
    uid = _resolve_user_id(payload) or _resolve_user_id(refund_payload)
    if not uid:
        customer_id = _customer_id_from_payload(payload) or _customer_id_from_payload(refund_payload)
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        customer_id = _lookup_refund_customer_id(refund_payload, stripe_client=stripe_client)
        uid = find_user_id_by_customer_id(customer_id) if customer_id else None
    if not uid:
        logger.warning("Unable to resolve Firebase user for Stripe refund id=%s", refund_payload.get("id"))
        return

    charge_id = _stripe_id_from_expandable(refund_payload.get("charge"))
    if event_type == "charge.refunded" and not charge_id:
        charge_id = _stripe_id_from_expandable(payload.get("id"))
    get_firestore_client().collection("users").document(uid).set(
        {
            "billing": {
                "latestRefundId": refund_payload.get("id"),
                "latestRefundStatus": refund_payload.get("status"),
                "latestRefundReason": refund_payload.get("reason"),
                "latestRefundCreatedAt": _timestamp_to_datetime(refund_payload.get("created"))
                or datetime.now(timezone.utc),
                "latestRefundAmount": refund_payload.get("amount"),
                "latestRefundCurrency": refund_payload.get("currency"),
                "latestRefundEventType": event_type,
                "latestRefundChargeId": charge_id,
                "latestRefundPaymentIntentId": _stripe_id_from_expandable(refund_payload.get("payment_intent")),
                "latestRefundFailureReason": refund_payload.get("failure_reason"),
            }
        },
        merge=True,
    )


def _apply_paid_invoice(
    uid: str,
    *,
    invoice_id: str,
    invoice_paid_at: datetime,
    plan_key: PlanKey,
    stripe_subscription_id: str | None,
    invoice_status: str | None = None,
    payment_intent_status: str | None = None,
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
            "billing.latestInvoiceStatus": invoice_status or "paid",
            "billing.latestInvoicePaidAt": invoice_paid_at,
            "billing.latestPaymentIntentStatus": payment_intent_status,
            "billing.latestPaymentFailureCode": None,
            "billing.latestPaymentFailureMessage": None,
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
    for expandable_key in ("payment_intent", "charge"):
        expandable = payload.get(expandable_key)
        if not isinstance(expandable, dict):
            continue
        nested_uid = _resolve_user_id(expandable)
        if nested_uid:
            return nested_uid
    return None


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "customer": _customer_id_from_payload(payload),
        "subscription": payload.get("subscription"),
        "charge": _stripe_id_from_expandable(payload.get("charge")),
        "payment_intent": _stripe_id_from_expandable(payload.get("payment_intent")),
        "reason": payload.get("reason"),
        "amount": payload.get("amount"),
        "currency": payload.get("currency"),
        "failure_reason": payload.get("failure_reason"),
        "status": payload.get("status"),
        "billing_cycle_anchor": payload.get("billing_cycle_anchor"),
        "cancel_at": payload.get("cancel_at"),
        "cancel_at_period_end": payload.get("cancel_at_period_end"),
        "canceled_at": payload.get("canceled_at"),
        "current_period_end": payload.get("current_period_end"),
        "payment_status": payload.get("payment_status"),
        "payment_intent_status": _payment_intent_status(payload),
    }


def _has_scheduled_cancel(payload: dict[str, Any]) -> bool:
    if bool(payload.get("cancel_at_period_end", False)):
        return True
    status = _get_string(payload, "status")
    return status not in {"canceled", "incomplete_expired"} and payload.get("cancel_at") is not None


def _get_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _stripe_id_from_expandable(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _get_string(value, "id")
    return None


def _refund_payload_from_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type != "charge.refunded":
        return payload
    refunds = ((payload.get("refunds") or {}).get("data") or [])
    if refunds:
        refund = refunds[0]
        if isinstance(refund, dict):
            return {**refund, "charge": refund.get("charge") or payload.get("id")}
    return {
        "id": None,
        "amount": payload.get("amount_refunded"),
        "currency": payload.get("currency"),
        "charge": payload.get("id"),
        "payment_intent": payload.get("payment_intent"),
        "status": "succeeded" if payload.get("refunded") else None,
    }


def _customer_id_from_payload(payload: dict[str, Any]) -> str | None:
    customer_id = _get_string(payload, "customer")
    if customer_id:
        return customer_id
    for expandable_key in ("payment_intent", "charge"):
        expandable = payload.get(expandable_key)
        if isinstance(expandable, dict):
            customer_id = _customer_id_from_payload(expandable)
            if customer_id:
                return customer_id
    return None


def _lookup_refund_customer_id(refund_payload: dict[str, Any], *, stripe_client: Any | None) -> str | None:
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    for resource_names, object_id in (
        (("charges", "Charge"), _stripe_id_from_expandable(refund_payload.get("charge"))),
        (("payment_intents", "PaymentIntent"), _stripe_id_from_expandable(refund_payload.get("payment_intent"))),
    ):
        if not object_id:
            continue
        resource = None
        for resource_name in resource_names:
            resource = getattr(client, resource_name, None)
            if resource is not None:
                break
        retrieve = getattr(resource, "retrieve", None)
        if not callable(retrieve):
            continue
        try:
            retrieved = _to_plain_dict(retrieve(object_id))
        except TypeError:
            try:
                retrieved = _to_plain_dict(retrieve(object_id, params={}))
            except Exception:
                continue
        except Exception:
            continue
        customer_id = _customer_id_from_payload(retrieved)
        if customer_id:
            return customer_id
    return None


def _payment_intent_status(payload: dict[str, Any]) -> str | None:
    payment_intent = payload.get("payment_intent")
    if isinstance(payment_intent, dict):
        return _get_string(payment_intent, "status")
    return None


def _payment_failure_code(payload: dict[str, Any]) -> str | None:
    payment_intent = payload.get("payment_intent")
    if not isinstance(payment_intent, dict):
        return None
    last_error = payment_intent.get("last_payment_error")
    if isinstance(last_error, dict):
        return _get_string(last_error, "code") or _get_string(last_error, "decline_code")
    return None


def _payment_failure_message(payload: dict[str, Any]) -> str | None:
    payment_intent = payload.get("payment_intent")
    if not isinstance(payment_intent, dict):
        return None
    last_error = payment_intent.get("last_payment_error")
    if isinstance(last_error, dict):
        message = _get_string(last_error, "message")
        if message:
            return message[:500]
    return None


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
