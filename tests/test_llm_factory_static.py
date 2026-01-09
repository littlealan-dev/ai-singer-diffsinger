import json

from src.backend.config import Settings
from src.backend.llm_factory import create_llm_client
from src.backend.llm_prompt import parse_llm_response


def test_static_llm_provider_generates_synthesize(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "static")
    monkeypatch.delenv("LLM_STATIC_RESPONSE", raising=False)
    settings = Settings.from_env()
    client = create_llm_client(settings)
    assert client is not None
    response = client.generate("", [])
    payload = parse_llm_response(response)
    assert payload is not None
    assert payload.tool_calls
    assert payload.tool_calls[0].name == "synthesize"


def test_static_llm_provider_uses_env_response(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "static")
    monkeypatch.setenv(
        "LLM_STATIC_RESPONSE",
        json.dumps(
            {
                "tool_calls": [{"name": "synthesize", "arguments": {"voicebank": "Raine_Rena_2.01"}}],
                "final_message": "Using the stub.",
                "include_score": False,
            }
        ),
    )
    settings = Settings.from_env()
    client = create_llm_client(settings)
    response = client.generate("", [])
    payload = parse_llm_response(response)
    assert payload is not None
    assert payload.final_message == "Using the stub."
