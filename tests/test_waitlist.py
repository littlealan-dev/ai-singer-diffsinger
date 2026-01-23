import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.backend.main import create_app


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
        from src.backend.waitlist import WaitlistResult

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
