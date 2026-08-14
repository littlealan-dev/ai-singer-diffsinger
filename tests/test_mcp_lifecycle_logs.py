import logging
import time

from src.backend.config import Settings
from src.backend.mcp_client import (
    McpError,
    McpProcess,
    McpRequestTimeoutError,
    McpRouter,
    McpStartupInProgressError,
    McpToolError,
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


def test_mcp_process_preserves_structured_tool_error(monkeypatch):
    process = McpProcess.__new__(McpProcess)
    process._timeout_seconds = 30
    monkeypatch.setattr(
        process,
        "_send_request",
        lambda request, timeout_seconds: {
            "error": {
                "code": "invalid_musicxml",
                "message": "Invalid MusicXML.",
                "type": "InvalidMusicXmlError",
                "retryable": False,
            }
        },
    )

    try:
        process.call_tool("parse_score", {})
    except McpToolError as exc:
        assert exc.code == "invalid_musicxml"
        assert exc.error_type == "InvalidMusicXmlError"
        assert exc.retryable is False
    else:
        raise AssertionError("Expected McpToolError")


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


def test_mcp_tool_error_does_not_restart_or_retry(caplog):
    class ToolErrorProcess(DummyProcess):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def call_tool(self, name, arguments):
            self.call_count += 1
            raise McpToolError(
                {
                    "code": "invalid_musicxml",
                    "message": "Invalid MusicXML.",
                    "type": "InvalidMusicXmlError",
                    "retryable": False,
                }
            )

    settings = Settings.from_env()
    router = McpRouter(settings)
    process = ToolErrorProcess()
    router._cpu = process
    router._gpu = DummyProcess()

    caplog.set_level(logging.WARNING)
    try:
        router._call_with_retry("cpu", "parse_score", {})
    except McpToolError as exc:
        assert exc.code == "invalid_musicxml"
    else:
        raise AssertionError("Expected McpToolError")

    assert process.call_count == 1
    assert process.stop_count == 0
    assert process.start_count == 0
    assert any("retry_skipped=true" in record.message for record in caplog.records)


def test_gpu_synthesize_tool_error_restarts_and_retries_once(caplog):
    class GpuErrorThenSuccessProcess(DummyProcess):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def call_tool(self, name, arguments):
            self.call_count += 1
            if self.call_count == 1:
                raise McpToolError(
                    {
                        "message": (
                            "ONNXRuntimeError: BFCArena::AllocateRawInternal "
                            "failed to allocate 287244032 bytes on CUDAExecutionProvider."
                        ),
                        "type": "ONNXRuntimeError",
                    }
                )
            return {"ok": True, "tool": name}

    settings = Settings.from_env()
    router = McpRouter(settings)
    process = GpuErrorThenSuccessProcess()
    router._cpu = DummyProcess()
    router._gpu = process

    caplog.set_level(logging.INFO)
    result = router._call_with_retry("gpu", "synthesize", {})

    assert result == {"ok": True, "tool": "synthesize"}
    assert process.call_count == 2
    assert process.stop_count == 1
    assert process.start_count == 1
    assert any(
        "mcp_gpu_worker_health_error tool=synthesize worker=gpu" in record.message
        for record in caplog.records
    )


def test_gpu_synthesize_tool_error_marks_retry_metadata_on_second_failure():
    class AlwaysGpuErrorProcess(DummyProcess):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def call_tool(self, name, arguments):
            self.call_count += 1
            raise McpToolError(
                {
                    "message": "CUBLAS_STATUS_ALLOC_FAILED during CUDAExecutionProvider inference.",
                    "type": "ONNXRuntimeError",
                    "retryable": True,
                    "workerRestartRequired": True,
                }
            )

    settings = Settings.from_env()
    router = McpRouter(settings)
    process = AlwaysGpuErrorProcess()
    router._cpu = DummyProcess()
    router._gpu = process

    try:
        router._call_with_retry("gpu", "synthesize", {})
    except McpToolError as exc:
        assert exc.payload["retryAttempted"] is True
        assert exc.payload["workerRestarted"] is True
    else:
        raise AssertionError("Expected McpToolError")

    assert process.call_count == 2
    assert process.stop_count == 1
    assert process.start_count == 1


def test_gpu_non_synthesize_tool_error_does_not_restart():
    class GpuSaveAudioErrorProcess(DummyProcess):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        def call_tool(self, name, arguments):
            self.call_count += 1
            raise McpToolError(
                {
                    "message": "CUDNN_STATUS_NOT_INITIALIZED",
                    "type": "ONNXRuntimeError",
                    "retryable": True,
                    "workerRestartRequired": True,
                }
            )

    settings = Settings.from_env()
    router = McpRouter(settings)
    process = GpuSaveAudioErrorProcess()
    router._cpu = DummyProcess()
    router._gpu = process

    try:
        router._call_with_retry("gpu", "save_audio", {})
    except McpToolError:
        pass
    else:
        raise AssertionError("Expected McpToolError")

    assert process.call_count == 1
    assert process.stop_count == 0
    assert process.start_count == 0


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
