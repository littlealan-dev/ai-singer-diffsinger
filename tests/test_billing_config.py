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


def test_billing_refresh_config_reads_service_account_only_during_firebase_discovery(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("BILLING_REFRESH_SERVICE_ACCOUNT", raising=False)
    monkeypatch.setenv("ADMIN_PORT", "8081")
    monkeypatch.setenv("GCLOUD_PROJECT", "sightsinger-app")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.sightsinger-app").write_text(
        "UNRELATED_SETTING=ignored\n"
        "BILLING_REFRESH_SERVICE_ACCOUNT=refresh@sightsinger-app.iam.gserviceaccount.com\n",
        encoding="utf-8",
    )

    config = get_billing_refresh_config()

    assert config.service_account == "refresh@sightsinger-app.iam.gserviceaccount.com"


def test_billing_refresh_config_does_not_read_dotenv_outside_firebase_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("BILLING_REFRESH_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("ADMIN_PORT", raising=False)
    monkeypatch.setenv("GCLOUD_PROJECT", "sightsinger-app")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.sightsinger-app").write_text(
        "BILLING_REFRESH_SERVICE_ACCOUNT=refresh@sightsinger-app.iam.gserviceaccount.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="BILLING_REFRESH_SERVICE_ACCOUNT"):
        get_billing_refresh_config()
