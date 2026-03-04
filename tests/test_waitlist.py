import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.backend.config import Settings
from src.backend.main import create_app
from src.backend.waitlist import WaitlistResult, subscribe_to_waitlist


def _prepare_app(monkeypatch, overrides=None):
    data_dir = Path("tests/output/backend_data") / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BACKEND_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "true")
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    async def _noop_app_check(_request):
        return None

    monkeypatch.setattr("src.backend.main._require_app_check", _noop_app_check)
    if overrides:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
    return create_app()


def _settings(monkeypatch, overrides=None):
    monkeypatch.setenv("BACKEND_DATA_DIR", f"tests/output/backend_data/{uuid.uuid4().hex}")
    monkeypatch.setenv("BREVO_WAITLIST_LIST_ID", "3")
    monkeypatch.setenv("BREVO_DOI_TEMPLATE_ID", "1")
    monkeypatch.setenv("BREVO_DOI_REDIRECT_URL", "https://example.com/confirmed")
    if overrides:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
    return Settings.from_env()


class _FakeAsyncClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _no_sleep(*args, **kwargs):
    return None


def test_waitlist_requires_app_check(monkeypatch):
    app = _prepare_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/waitlist/subscribe",
            json={
                "email": "a@b.com",
                "gdpr_consent": True,
                "consent_text": "text",
                "source": "landing",
            },
        )
        assert response.status_code == 401


def test_waitlist_rejects_missing_consent(monkeypatch):
    app = _prepare_app(monkeypatch)
    monkeypatch.setattr("src.backend.main.verify_app_check_token", lambda token: True)
    with TestClient(app) as client:
        response = client.post(
            "/waitlist/subscribe",
            headers={"X-Firebase-AppCheck": "token"},
            json={
                "email": "a@b.com",
                "gdpr_consent": False,
                "consent_text": "text",
                "source": "landing",
            },
        )
        assert response.status_code == 400


def test_waitlist_success(monkeypatch):
    app = _prepare_app(monkeypatch)
    monkeypatch.setattr("src.backend.main.verify_app_check_token", lambda token: True)

    async def _fake_subscribe(*_, **__):
        return WaitlistResult(
            success=True,
            message="ok",
            requires_confirmation=True,
        )

    monkeypatch.setattr("src.backend.main.subscribe_to_waitlist", _fake_subscribe)
    with TestClient(app) as client:
        response = client.post(
            "/waitlist/subscribe",
            headers={"X-Firebase-AppCheck": "token"},
            json={
                "email": "a@b.com",
                "gdpr_consent": True,
                "consent_text": "text",
                "source": "landing",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["requires_confirmation"] is True


def test_waitlist_dependency_failure_maps_to_503(monkeypatch):
    app = _prepare_app(monkeypatch)
    monkeypatch.setattr("src.backend.main.verify_app_check_token", lambda token: True)

    async def _fake_subscribe(*_, **__):
        return WaitlistResult(
            success=False,
            message="Waitlist service temporarily unavailable. Please try again later.",
            requires_confirmation=False,
            status_code=503,
        )

    monkeypatch.setattr("src.backend.main.subscribe_to_waitlist", _fake_subscribe)
    with TestClient(app) as client:
        response = client.post(
            "/waitlist/subscribe",
            headers={"X-Firebase-AppCheck": "token"},
            json={
                "email": "a@b.com",
                "gdpr_consent": True,
                "consent_text": "text",
                "source": "landing",
            },
        )
        assert response.status_code == 503
        assert "temporarily unavailable" in response.json()["detail"]


def test_subscribe_to_waitlist_retries_timeout_then_succeeds(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr("src.backend.waitlist._load_brevo_api_key", lambda settings: "test-key")
    monkeypatch.setattr("src.backend.waitlist.asyncio.sleep", _no_sleep)
    timeout_request = httpx.Request("POST", "https://example.com")
    outcomes = [
        httpx.ReadTimeout("timed out", request=timeout_request),
        httpx.Response(204, request=timeout_request),
    ]
    monkeypatch.setattr(
        "src.backend.waitlist.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(outcomes),
    )

    result = asyncio.run(
        subscribe_to_waitlist(
            settings,
            email="a@b.com",
            first_name="Alan",
            feedback=None,
            gdpr_consent=True,
            consent_text="text",
            source="landing",
        )
    )

    assert result.success is True
    assert result.status_code == 200


def test_subscribe_to_waitlist_returns_503_on_transport_retry_exhaustion(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr("src.backend.waitlist._load_brevo_api_key", lambda settings: "test-key")
    monkeypatch.setattr("src.backend.waitlist.asyncio.sleep", _no_sleep)
    request = httpx.Request("POST", "https://example.com")
    outcomes = [
        httpx.ConnectError("boom", request=request),
        httpx.ConnectError("boom again", request=request),
    ]
    monkeypatch.setattr(
        "src.backend.waitlist.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(outcomes),
    )

    result = asyncio.run(
        subscribe_to_waitlist(
            settings,
            email="a@b.com",
            first_name=None,
            feedback=None,
            gdpr_consent=True,
            consent_text="text",
            source="landing",
        )
    )

    assert result.success is False
    assert result.status_code == 503


def test_subscribe_to_waitlist_returns_503_after_retryable_status_exhaustion(monkeypatch):
    settings = _settings(monkeypatch)
    monkeypatch.setattr("src.backend.waitlist._load_brevo_api_key", lambda settings: "test-key")
    monkeypatch.setattr("src.backend.waitlist.asyncio.sleep", _no_sleep)
    request = httpx.Request("POST", "https://example.com")
    outcomes = [
        httpx.Response(503, request=request, text="upstream busy"),
        httpx.Response(503, request=request, text="still busy"),
    ]
    monkeypatch.setattr(
        "src.backend.waitlist.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(outcomes),
    )

    result = asyncio.run(
        subscribe_to_waitlist(
            settings,
            email="a@b.com",
            first_name=None,
            feedback=None,
            gdpr_consent=True,
            consent_text="text",
            source="landing",
        )
    )

    assert result.success is False
    assert result.status_code == 503
