from __future__ import annotations

"""Thin entrypoints for the Stripe billing functions package."""

from datetime import datetime, timezone
import time

from firebase_functions import scheduler_fn

from src.backend.billing_checkout import create_checkout_session
from src.backend.billing_config import get_billing_refresh_config
from src.backend.billing_portal import create_portal_session
from src.backend.billing_refresh import run_credit_refresh
from src.backend.billing_refresh_metrics import emit_credit_refresh_metrics
from src.backend.billing_webhooks import handle_event
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

_refresh_config = get_billing_refresh_config()


@scheduler_fn.on_schedule(
    schedule=_refresh_config.schedule,
    timeout_sec=_refresh_config.timeout_seconds,
)
def refreshCredits(event: scheduler_fn.ScheduledEvent) -> None:
    started_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc)
    result = run_credit_refresh(
        now=started_at,
        max_users=_refresh_config.max_due_users,
    )
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    logger.info(
        "billing_credit_refresh_run result=%s duration_ms=%s",
        result,
        duration_ms,
    )
    if _refresh_config.metrics_enabled:
        emit_credit_refresh_metrics(result, duration_ms=duration_ms)


__all__ = [
    "create_checkout_session",
    "create_portal_session",
    "handle_event",
    "run_credit_refresh",
    "refreshCredits",
]
