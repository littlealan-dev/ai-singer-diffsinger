from __future__ import annotations

"""Recurring credit refresh logic for free and paid plans."""

from calendar import monthrange
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_config import get_billing_config
from src.backend.billing_plans import get_plan
from src.backend.billing_types import BillingState, PlanKey
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


def compute_next_monthly_refresh(anchor: datetime, after: datetime) -> datetime:
    anchor_utc = _ensure_utc(anchor)
    after_utc = _ensure_utc(after)
    year = after_utc.year
    month = after_utc.month
    while True:
        day = min(anchor_utc.day, monthrange(year, month)[1])
        candidate = datetime(
            year,
            month,
            day,
            anchor_utc.hour,
            anchor_utc.minute,
            anchor_utc.second,
            anchor_utc.microsecond,
            tzinfo=timezone.utc,
        )
        if candidate > after_utc:
            return candidate
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def run_credit_refresh(*, now: datetime | None = None) -> dict[str, int]:
    db = get_firestore_client()
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    processed = 0
    skipped_reserved = 0
    query = db.collection("users").where("billing.nextCreditRefreshAt", "<=", current_time)
    for snapshot in query.stream():
        outcome = apply_due_refresh(snapshot.id, now=current_time)
        if outcome == "applied":
            processed += 1
        elif outcome == "reserved":
            skipped_reserved += 1
    return {
        "processed": processed,
        "skipped_reserved": skipped_reserved,
    }


def apply_due_refresh(uid: str, *, now: datetime | None = None) -> str:
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    config = get_billing_config()

    @firestore.transactional
    def _apply(transaction):
        snapshot = user_ref.get(transaction=transaction)
        if not snapshot.exists:
            return "missing"
        data = snapshot.to_dict() or {}
        billing = data.get("billing") or {}
        credits = data.get("credits") or {}
        billing = {**billing, "_credits": credits}
        due_at = billing.get("nextCreditRefreshAt")
        if due_at is None or _ensure_utc(due_at) > current_time:
            return "not_due"
        reserved = int(credits.get("reserved", 0) or 0)
        if reserved > 0:
            return "reserved"

        active_plan_key = str(billing.get("activePlanKey") or "free")
        plan_key = _effective_refresh_plan_key(active_plan_key, billing)
        plan = get_plan(plan_key, config)
        allowance = plan.monthly_allowance
        anchor = _ensure_utc(billing.get("creditRefreshAnchor") or current_time)
        next_refresh = compute_next_monthly_refresh(anchor, current_time)
        grant_type = _grant_type_for_refresh(active_plan_key, billing)
        ledger_id = _refresh_ledger_id(uid, due_at, grant_type)
        ledger_ref = db.collection("credit_ledger").document(ledger_id)
        ledger_snapshot = ledger_ref.get(transaction=transaction)
        if ledger_snapshot.exists:
            transaction.update(
                user_ref,
                {
                    "billing.lastCreditRefreshAt": current_time,
                    "billing.nextCreditRefreshAt": next_refresh,
                },
            )
            return "already_applied"

        transaction.update(
            user_ref,
            {
                "credits.balance": allowance,
                "credits.monthlyAllowance": allowance,
                "credits.lastGrantType": grant_type,
                "credits.lastGrantAt": current_time,
                "credits.lastGrantInvoiceId": billing.get("latestInvoiceId")
                if grant_type == "grant_paid_subscription_cycle"
                else credits.get("lastGrantInvoiceId"),
                "billing.lastCreditRefreshAt": current_time,
                "billing.nextCreditRefreshAt": next_refresh,
            },
        )
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "type": grant_type,
                "amount": allowance,
                "balanceAfter": allowance,
                "createdAt": current_time,
                "refreshDueAt": due_at,
            },
        )
        return "applied"

    return _apply(db.transaction())


def _effective_refresh_plan_key(active_plan_key: str, billing: BillingState | dict[str, Any]) -> PlanKey:
    interval = billing.get("billingInterval")
    latest_paid_at = billing.get("latestInvoicePaidAt")
    last_refresh_at = billing.get("lastCreditRefreshAt")
    latest_invoice_id = billing.get("latestInvoiceId")
    credits = billing.get("_credits") or {}
    last_grant_invoice_id = credits.get("lastGrantInvoiceId")
    if interval == "year" and active_plan_key != "free":
        return active_plan_key  # type: ignore[return-value]
    if interval == "month" and active_plan_key != "free":
        if latest_invoice_id and latest_invoice_id != last_grant_invoice_id:
            return active_plan_key  # type: ignore[return-value]
        if latest_paid_at and (last_refresh_at is None or _ensure_utc(latest_paid_at) > _ensure_utc(last_refresh_at)):
            return active_plan_key  # type: ignore[return-value]
    return "free"


def _grant_type_for_refresh(active_plan_key: str, billing: BillingState | dict[str, Any]) -> str:
    interval = billing.get("billingInterval")
    latest_paid_at = billing.get("latestInvoicePaidAt")
    last_refresh_at = billing.get("lastCreditRefreshAt")
    latest_invoice_id = billing.get("latestInvoiceId")
    credits = billing.get("_credits") or {}
    last_grant_invoice_id = credits.get("lastGrantInvoiceId")
    if interval == "year" and active_plan_key != "free":
        return "grant_paid_annual_monthly_refresh"
    if interval == "month" and active_plan_key != "free":
        if latest_invoice_id and latest_invoice_id != last_grant_invoice_id:
            return "grant_paid_subscription_cycle"
        if latest_paid_at and (last_refresh_at is None or _ensure_utc(latest_paid_at) > _ensure_utc(last_refresh_at)):
            return "grant_paid_subscription_cycle"
    return "grant_free_monthly"


def _refresh_ledger_id(uid: str, due_at: datetime | None, grant_type: str) -> str:
    if due_at is None:
        due_key = "none"
    else:
        due_key = _ensure_utc(due_at).strftime("%Y%m%dT%H%M%S")
    return f"grant_refresh_{uid}_{due_key}_{grant_type}"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
