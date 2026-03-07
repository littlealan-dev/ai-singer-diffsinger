from __future__ import annotations

"""Inline retry helpers for transient credit-related infrastructure failures."""

from typing import Any, Callable
import asyncio

from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


async def retry_credit_op(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Retry a status-returning operation when it reports infra_error."""
    max_attempts = kwargs.pop("max_attempts", 3)
    base_delay = kwargs.pop("base_delay", 0.5)
    attempts = max(1, int(max_attempts))
    delay_seconds = max(0.0, float(base_delay))
    operation_name = getattr(fn, "__name__", fn.__class__.__name__)
    last_result: Any = None
    for attempt in range(1, attempts + 1):
        result = await asyncio.to_thread(fn, *args, **kwargs)
        last_result = result
        status = getattr(result, "status", None)
        if status != "infra_error":
            if attempt > 1:
                logger.info(
                    "credit_retry_succeeded_after_retry operation=%s attempt=%s max_attempts=%s status=%s",
                    operation_name,
                    attempt,
                    attempts,
                    status,
                )
            return result
        logger.warning(
            "credit_retry_attempt operation=%s attempt=%s max_attempts=%s status=%s",
            operation_name,
            attempt,
            attempts,
            status,
        )
        if attempt >= attempts:
            logger.error(
                "credit_retry_exhausted operation=%s attempts=%s status=%s",
                operation_name,
                attempts,
                status,
            )
            return result
        sleep_seconds = delay_seconds * (2 ** (attempt - 1))
        await asyncio.sleep(sleep_seconds)
    return last_result
