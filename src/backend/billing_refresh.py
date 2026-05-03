from __future__ import annotations

"""Recurring credit refresh logic for free and paid plans."""

from calendar import monthrange
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from google.cloud import firestore

from src.backend.billing_config import get_billing_config
from src.backend.billing_plans import get_plan
from src.backend.billing_types import BillingState, PlanKey
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

_TERMINAL_SUBSCRIPTION_STATUSES = {"canceled", "incomplete_expired"}


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


def run_credit_refresh(
    *,
    now: datetime | None = None,
    max_users: int | None = None,
    run_id: str | None = None,
) -> dict[str, int | bool | str]:
    db = get_firestore_client()
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    refresh_run_id = run_id or f"refresh_{current_time.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
    limit = max_users or 300
    processed = 0
    skipped_reserved = 0
    failed = 0
    scanned = 0
    query = (
        db.collection("users")
        .where("billing.nextCreditRefreshAt", "<=", current_time)
        .order_by("billing.nextCreditRefreshAt")
        .limit(limit)
    )
    for snapshot in query.stream():
        scanned += 1
        try:
            outcome = apply_due_refresh(snapshot.id, now=current_time, run_id=refresh_run_id)
            if outcome in {"applied", "already_applied"}:
                processed += 1
            elif outcome == "reserved":
                skipped_reserved += 1
        except Exception as exc:
            failed += 1
            _record_refresh_failure(snapshot.id, now=current_time, run_id=refresh_run_id, error=exc)
            logger.exception("billing_credit_refresh_user_failed uid=%s run_id=%s", snapshot.id, refresh_run_id)
    return {
        "processed": processed,
        "skipped_reserved": skipped_reserved,
        "failed": failed,
        "scanned": scanned,
        "limit": limit,
        "has_more_due_users": scanned >= limit,
        "run_id": refresh_run_id,
    }


def apply_due_refresh(uid: str, *, now: datetime | None = None, run_id: str | None = None) -> str:
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    refresh_run_id = run_id or f"refresh_{current_time.strftime('%Y%m%dT%H%M%S')}"
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
            transaction.update(
                user_ref,
                _refresh_scheduler_audit_update(
                    current_time,
                    status="reserved",
                    run_id=refresh_run_id,
                ),
            )
            return "reserved"

        decision = _refresh_decision(str(billing.get("activePlanKey") or "free"), billing)
        if decision.status_only:
            transaction.update(
                user_ref,
                _refresh_scheduler_audit_update(
                    current_time,
                    status=decision.status,
                    run_id=refresh_run_id,
                ),
            )
            return decision.status
        plan_key = decision.plan_key
        plan = get_plan(plan_key, config)
        allowance = plan.monthly_allowance
        anchor = _ensure_utc(billing.get("creditRefreshAnchor") or current_time)
        next_refresh = compute_next_monthly_refresh(anchor, current_time)
        grant_type = decision.grant_type
        ledger_id = _refresh_ledger_id(uid, due_at, grant_type)
        ledger_ref = db.collection("credit_ledger").document(ledger_id)
        ledger_snapshot = ledger_ref.get(transaction=transaction)
        if ledger_snapshot.exists:
            transaction.update(
                user_ref,
                {
                    "billing.lastCreditRefreshAt": current_time,
                    "billing.nextCreditRefreshAt": next_refresh,
                    **_refresh_scheduler_audit_update(
                        current_time,
                        status="already_applied",
                        run_id=refresh_run_id,
                    ),
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
                **_refresh_scheduler_audit_update(
                    current_time,
                    status="applied",
                    run_id=refresh_run_id,
                ),
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


def _record_refresh_failure(uid: str, *, now: datetime, run_id: str, error: Exception) -> None:
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    try:
        user_ref.update(
            _refresh_scheduler_audit_update(
                now,
                status="failed",
                run_id=run_id,
                error_message=_sanitize_error_message(error),
            )
        )
    except Exception:
        logger.exception("billing_credit_refresh_failure_audit_failed uid=%s run_id=%s", uid, run_id)


def _refresh_scheduler_audit_update(
    attempted_at: datetime,
    *,
    status: str,
    run_id: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    return {
        "billing.refreshScheduler.lastAttemptAt": attempted_at,
        "billing.refreshScheduler.lastStatus": status,
        "billing.refreshScheduler.lastErrorMessage": error_message,
        "billing.refreshScheduler.lastRunId": run_id,
    }


def _sanitize_error_message(error: Exception) -> str:
    message = str(error) or error.__class__.__name__
    message = " ".join(message.split())
    if len(message) > 500:
        return message[:497] + "..."
    return message


class _RefreshDecision:
    def __init__(
        self,
        *,
        status: str,
        plan_key: PlanKey = "free",
        grant_type: str = "grant_free_monthly",
        status_only: bool = False,
    ) -> None:
        self.status = status
        self.plan_key = plan_key
        self.grant_type = grant_type
        self.status_only = status_only


def _refresh_decision(active_plan_key: str, billing: BillingState | dict[str, Any]) -> _RefreshDecision:
    if active_plan_key == "free":
        return _RefreshDecision(status="applied", plan_key="free", grant_type="grant_free_monthly")

    interval = billing.get("billingInterval")
    subscription_status = str(billing.get("stripeSubscriptionStatus") or "")
    if subscription_status in _TERMINAL_SUBSCRIPTION_STATUSES:
        return _RefreshDecision(status="billing_state_inconsistent", status_only=True)

    if interval == "year":
        return _RefreshDecision(
            status="applied",
            plan_key=active_plan_key,  # type: ignore[arg-type]
            grant_type="grant_paid_annual_monthly_refresh",
        )

    if interval != "month":
        return _RefreshDecision(status="billing_state_inconsistent", status_only=True)

    latest_paid_at = billing.get("latestInvoicePaidAt")
    last_refresh_at = billing.get("lastCreditRefreshAt")
    latest_invoice_id = billing.get("latestInvoiceId")
    credits = billing.get("_credits") or {}
    last_grant_invoice_id = credits.get("lastGrantInvoiceId")
    if latest_invoice_id and latest_paid_at and latest_invoice_id != last_grant_invoice_id:
        return _RefreshDecision(
            status="applied",
            plan_key=active_plan_key,  # type: ignore[arg-type]
            grant_type="grant_paid_subscription_cycle",
        )
    if latest_paid_at and (last_refresh_at is None or _ensure_utc(latest_paid_at) > _ensure_utc(last_refresh_at)):
        return _RefreshDecision(
            status="applied",
            plan_key=active_plan_key,  # type: ignore[arg-type]
            grant_type="grant_paid_subscription_cycle",
        )
    return _RefreshDecision(status="waiting_for_invoice", status_only=True)


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
