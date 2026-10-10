import pytest

from src.backend.config import Settings


def test_default_voicebank_is_liee(monkeypatch):
    monkeypatch.delenv("BACKEND_DEFAULT_VOICEBANK", raising=False)

    settings = Settings.from_env()

    assert settings.default_voicebank == "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"


def test_default_voicebank_env_override_is_preserved(monkeypatch):
    monkeypatch.setenv("BACKEND_DEFAULT_VOICEBANK", "custom-voicebank")

    settings = Settings.from_env()

    assert settings.default_voicebank == "custom-voicebank"


def test_synthesis_max_duration_seconds_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_MAX_DURATION_SECONDS", raising=False)

    settings = Settings.from_env()

    assert settings.synthesis_max_duration_seconds == 300.0


def test_synthesis_max_duration_seconds_env_override_is_preserved(monkeypatch):
    monkeypatch.setenv("SYNTHESIS_MAX_DURATION_SECONDS", "240")

    settings = Settings.from_env()

    assert settings.synthesis_max_duration_seconds == 240.0


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [("dev", "memory"), ("test", "memory"), ("prod", "firestore")],
)
def test_session_store_defaults_to_memory_only_in_development(monkeypatch, app_env, expected):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("SESSION_STORE", raising=False)

    settings = Settings.from_env()

    assert settings.session_store == expected


def test_session_store_env_override_is_preserved(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SESSION_STORE", "Firestore")

    settings = Settings.from_env()

    assert settings.session_store == "firestore"


def test_session_store_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("SESSION_STORE", "redis")

    with pytest.raises(ValueError, match="SESSION_STORE"):
        Settings.from_env()
