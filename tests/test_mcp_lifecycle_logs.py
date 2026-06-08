import logging
import time

from src.backend.config import Settings
from src.backend.mcp_client import (
    McpError,
    McpRequestTimeoutError,
    McpRouter,
    McpStartupInProgressError,
)


class DummyProcess:
    def __init__(self) -> None:
        self.started = False
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.started = True
        self.start_count += 1

    def stop(self) -> None:
        self.started = False
        self.stop_count += 1

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


def test_mcp_tool_timeout_restarts_without_retry(caplog):
    class TimeoutProcess(DummyProcess):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def call_tool(self, name, arguments):
            self.call_count += 1
            raise McpRequestTimeoutError(
                "MCP request timed out: tools/call",
                method="tools/call",
                timeout_seconds=60,
            )

    settings = Settings.from_env()
    router = McpRouter(settings)
    timeout_process = TimeoutProcess()
    router._cpu = timeout_process
    router._gpu = DummyProcess()

    caplog.set_level(logging.WARNING)
    try:
        router._call_with_retry("cpu", "parse_score", {})
    except McpRequestTimeoutError:
        pass
    else:
        raise AssertionError("Expected McpRequestTimeoutError")

    assert timeout_process.call_count == 1
    assert timeout_process.stop_count == 1
    assert timeout_process.start_count == 1
    assert any(
        "mcp_tool_timeout tool=parse_score worker=cpu" in record.message
        for record in caplog.records
    )


def test_mcp_router_background_start_does_not_block_calls_after_ready():
    settings = Settings.from_env()
    router = McpRouter(settings)
    router._cpu = DummyProcess()
    router._gpu = DummyProcess()

    router.start_background()

    deadline = time.monotonic() + 1.0
    while not router._startup_ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert router.call_tool("list_voicebanks", {}) == {
        "ok": True,
        "tool": "list_voicebanks",
    }


def test_mcp_router_background_start_surfaces_startup_error():
    class FailingProcess(DummyProcess):
        def start(self) -> None:
            raise RuntimeError("startup failed")

    settings = Settings.from_env()
    router = McpRouter(settings)
    router._cpu = FailingProcess()
    router._gpu = DummyProcess()

    router.start_background()

    deadline = time.monotonic() + 1.0
    while not router._startup_ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)

    try:
        router.call_tool("list_voicebanks", {})
    except McpError as exc:
        assert "MCP startup failed: startup failed" in str(exc)
    else:
        raise AssertionError("Expected McpError")


def test_mcp_router_background_start_timeout_is_typed(monkeypatch):
    class SlowProcess(DummyProcess):
        def start(self) -> None:
            time.sleep(0.1)
            super().start()

    monkeypatch.setenv("BACKEND_READY_TIMEOUT_SECONDS", "0.01")
    settings = Settings.from_env()
    router = McpRouter(settings)
    router._cpu = SlowProcess()
    router._gpu = DummyProcess()

    router.start_background()

    try:
        router.call_tool("list_voicebanks", {})
    except McpStartupInProgressError as exc:
        assert exc.code == "backend_starting"
        assert "Please try again in a moment" in str(exc)
    else:
        raise AssertionError("Expected McpStartupInProgressError")
