from src.backend.config import Settings


def test_default_voicebank_is_liee(monkeypatch):
    monkeypatch.delenv("BACKEND_DEFAULT_VOICEBANK", raising=False)

    settings = Settings.from_env()

    assert settings.default_voicebank == "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"


def test_default_voicebank_env_override_is_preserved(monkeypatch):
    monkeypatch.setenv("BACKEND_DEFAULT_VOICEBANK", "custom-voicebank")

    settings = Settings.from_env()

    assert settings.default_voicebank == "custom-voicebank"
