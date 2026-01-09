import logging

from src.backend.config import Settings
from src.backend.mcp_client import McpRouter


class DummyProcess:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def call_tool(self, name, arguments):
        return {"ok": True, "tool": name}


def test_mcp_tool_call_logs(caplog):
    settings = Settings.from_env()
    router = McpRouter(settings)
    router._cpu = DummyProcess()
    router._gpu = DummyProcess()

    caplog.set_level(logging.INFO)
    router._call_with_retry("cpu", "list_voicebanks", {})

    assert any(
        "mcp_tool_call tool=list_voicebanks worker=cpu" in record.message
        for record in caplog.records
    )
