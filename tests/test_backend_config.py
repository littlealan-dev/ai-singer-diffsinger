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
