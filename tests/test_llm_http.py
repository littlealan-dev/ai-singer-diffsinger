from __future__ import annotations

import http.client
import io
import json
import random
import socket
import ssl
import urllib.error
from types import SimpleNamespace

import pytest

from src.backend.llm_http import _backoff_delay, _http_retry_delay, _is_transient_transport_error


SETTINGS = SimpleNamespace(llm_retry_base_delay_seconds=1.0, llm_retry_max_delay_seconds=10.0)


@pytest.mark.parametrize("error, expected", [
    (http.client.RemoteDisconnected("closed"), True),
    (ConnectionResetError("reset"), True),
    (ConnectionAbortedError("aborted"), True),
    (BrokenPipeError("pipe"), True),
    (ConnectionRefusedError("refused"), True),
    (http.client.IncompleteRead(b""), True),
    (ssl.SSLEOFError(8, "EOF"), True),
    (ssl.SSLCertVerificationError(1, "verify failed"), False),
    (urllib.error.URLError(TimeoutError("connect timeout")), True),
    (urllib.error.URLError(socket.gaierror(8, "DNS failed")), True),
    (urllib.error.URLError("permanent"), False),
    (urllib.error.URLError(OSError("other")), False),
    (urllib.error.URLError(ssl.SSLCertVerificationError(1, "verify failed")), False),
])
def test_transient_transport_classification(error, expected):  # type: ignore[no-untyped-def]
    assert _is_transient_transport_error(error) is expected


def _error(status: int, body: object = None, retry_after: str | None = None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    details = json.dumps(body) if body is not None else "not-json"
    return urllib.error.HTTPError("https://example.invalid", status, "error", headers, io.BytesIO(details.encode())), details


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_server_failures_retry_with_backoff(status, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)
    exc, details = _error(status)
    assert _http_retry_delay(exc, details, 1, SETTINGS) == 1.0


def test_server_delay_sets_floor_and_cap(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)
    exc, details = _error(503, retry_after="3")
    assert _http_retry_delay(exc, details, 1, SETTINGS) == 3.0
    exc, details = _error(503, retry_after="11")
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 504])
def test_other_http_statuses_are_not_retried(status):  # type: ignore[no-untyped-def]
    exc, details = _error(status, retry_after="1")
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None


def test_429_requires_server_delay_and_sufficient_quota(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)
    exc, details = _error(429)
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None
    exc, details = _error(429, {"error": {"code": "rate_limit_exceeded"}}, retry_after="2")
    assert _http_retry_delay(exc, details, 1, SETTINGS) == 2.0
    exc, details = _error(429, {"error": {"code": "insufficient_quota"}}, retry_after="2")
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None
    exc, details = _error(429, retry_after="11")
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None


@pytest.mark.parametrize("duration, expected", [("7s", 7.0), ("0.5s", 1.0), ("60s", None)])
def test_gemini_retry_info_duration(duration, expected, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": duration}]}}
    exc, details = _error(429, body)
    assert _http_retry_delay(exc, details, 1, SETTINGS) == expected


def test_header_takes_precedence_and_http_date_is_ignored(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: 0)
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}]}}
    exc, details = _error(429, body, retry_after="2")
    assert _http_retry_delay(exc, details, 1, SETTINGS) == 2.0
    exc, details = _error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    assert _http_retry_delay(exc, details, 1, SETTINGS) is None


def test_backoff_jitter_and_zero_base(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(random, "uniform", lambda low, high: high)
    assert _backoff_delay(1, SETTINGS) == 1.25
    assert _backoff_delay(2, SETTINGS) == 2.5
    assert _backoff_delay(5, SETTINGS) == 10.0
    zero = SimpleNamespace(llm_retry_base_delay_seconds=0.0, llm_retry_max_delay_seconds=10.0)
    assert _backoff_delay(1, zero) == 0.0
