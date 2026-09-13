from __future__ import annotations

"""Inline retry helpers for transient credit-related infrastructure failures."""

from typing import Any, Callable
import asyncio
import time

from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)


def _remaining_seconds(
    deadline_getter: Callable[[], float | None] | None,
) -> float | None:
    if deadline_getter is None:
        return None
    deadline = deadline_getter()
    if deadline is None:
        return None
    return deadline - time.monotonic()


async def retry_credit_op(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Retry a status-returning operation when it reports infra_error."""
    max_attempts = kwargs.pop("max_attempts", 3)
    base_delay = kwargs.pop("base_delay", 0.5)
    deadline_getter = kwargs.pop("deadline_getter", None)
    attempts = max(1, int(max_attempts))
    delay_seconds = max(0.0, float(base_delay))
    operation_name = getattr(fn, "__name__", fn.__class__.__name__)
    last_result: Any = None
    for attempt in range(1, attempts + 1):
        remaining = _remaining_seconds(deadline_getter)
        if remaining is not None and remaining <= 0:
            raise asyncio.TimeoutError
        # Do not time out an in-flight synchronous persistence call. The caller's
        # shutdown wait remains bounded and can report this task as incomplete,
        # while the operation itself stays tracked until its actual completion.
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
        remaining = _remaining_seconds(deadline_getter)
        if remaining is not None:
            if remaining <= 0:
                raise asyncio.TimeoutError
            if sleep_seconds >= remaining:
                await asyncio.sleep(remaining)
                raise asyncio.TimeoutError
        await asyncio.sleep(sleep_seconds)
    return last_result
