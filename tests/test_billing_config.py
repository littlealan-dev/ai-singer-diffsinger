import pytest

from src.backend.billing_config import get_billing_refresh_config


@pytest.fixture(autouse=True)
def clear_billing_refresh_config_cache():
    get_billing_refresh_config.cache_clear()
    yield
    get_billing_refresh_config.cache_clear()


def test_billing_refresh_config_requires_explicit_service_account(monkeypatch):
    monkeypatch.setenv(
        "BILLING_REFRESH_SERVICE_ACCOUNT",
        "custom-refresh@sightsinger-app.iam.gserviceaccount.com",
    )

    config = get_billing_refresh_config()

    assert config.service_account == "custom-refresh@sightsinger-app.iam.gserviceaccount.com"


def test_billing_refresh_config_errors_when_service_account_missing(monkeypatch):
    monkeypatch.delenv("BILLING_REFRESH_SERVICE_ACCOUNT", raising=False)

    with pytest.raises(ValueError, match="BILLING_REFRESH_SERVICE_ACCOUNT"):
        get_billing_refresh_config()


def test_billing_refresh_config_errors_when_service_account_blank(monkeypatch):
    monkeypatch.setenv("BILLING_REFRESH_SERVICE_ACCOUNT", "'  '")

    with pytest.raises(ValueError, match="BILLING_REFRESH_SERVICE_ACCOUNT"):
        get_billing_refresh_config()
