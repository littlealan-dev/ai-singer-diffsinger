"""Shared HTTP retry policy for LLM generation requests."""

from __future__ import annotations

import http.client
import json
import math
import random
import re
import socket
import ssl
import time
import urllib.error
import urllib.request


_RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503}
_RETRY_DELAY = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)s$")


def _is_transient_transport_error(exc: BaseException) -> bool:
    """Distinguish connection failures from permanent URL/TLS errors."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    return isinstance(
        reason,
        (ConnectionError, http.client.IncompleteRead, ssl.SSLError, TimeoutError, socket.gaierror),
    )


def _backoff_delay(attempt: int, settings: object) -> float:
    base = settings.llm_retry_base_delay_seconds * 2 ** (attempt - 1)
    return min(base + random.uniform(0, base * 0.25), settings.llm_retry_max_delay_seconds)


def _server_retry_delay(exc: urllib.error.HTTPError, details: str) -> float | None:
    header = exc.headers.get("Retry-After") if exc.headers is not None else None
    if header is not None:
        try:
            value = float(header)
            if math.isfinite(value) and value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        body = json.loads(details)
        items = body.get("error", {}).get("details", [])
        for item in items:
            if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("google.rpc.RetryInfo"):
                continue
            duration = item.get("retryDelay")
            if isinstance(duration, str) and _RETRY_DELAY.fullmatch(duration):
                return float(duration[:-1])
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def _http_retry_delay(
    exc: urllib.error.HTTPError, details: str, attempt: int, settings: object
) -> float | None:
    if exc.code not in _RETRYABLE_HTTP_STATUS:
        return None
    try:
        body = json.loads(details)
        if exc.code == 429 and body.get("error", {}).get("code") == "insufficient_quota":
            return None
    except (TypeError, ValueError, AttributeError):
        pass
    server_delay = _server_retry_delay(exc, details)
    if server_delay is not None and server_delay > settings.llm_retry_max_delay_seconds:
        return None
    if exc.code == 429 and server_delay is None:
        return None
    return max(_backoff_delay(attempt, settings), server_delay or 0)


def post_with_retry(request, *, timeout, provider, role, model, settings, logger) -> bytes:
    """Send an LLM HTTP request, retrying transient failures with bounded backoff."""
    max_attempts = settings.llm_retry_max_attempts
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except TimeoutError as exc:
            # A raw timeout can occur after the request was sent and must not be replayed.
            raise RuntimeError(f"{provider} request timed out.") from exc
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            failure = RuntimeError(f"{provider} HTTP error {exc.code}: {details}")
            delay = _http_retry_delay(exc, details, attempt, settings)
            status = exc.code
            cause = exc
        except (urllib.error.URLError, ConnectionError, http.client.IncompleteRead, ssl.SSLError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            failure = RuntimeError(f"{provider} connection error: {reason}")
            delay = _backoff_delay(attempt, settings) if _is_transient_transport_error(exc) else None
            status = None
            cause = exc
        if delay is None or attempt == max_attempts:
            raise failure from cause
        logger.warning(
            "llm_http_retry provider=%s role=%s model=%s attempt=%s max_attempts=%s "
            "delay_seconds=%.2f error_type=%s status=%s",
            provider, role.value, model, attempt, max_attempts, delay, type(cause).__name__, status,
        )
        time.sleep(delay)
    raise AssertionError("LLM retry loop completed without a response")
