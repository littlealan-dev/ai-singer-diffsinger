import logging
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from src.backend.config import Settings
from src.backend.mcp_client import (
    McpError,
    McpProcess,
    McpRequestTimeoutError,
    McpRouter,
    McpShuttingDownError,
    McpStartupInProgressError,
    McpToolError,
    McpWorkerUnavailableError,
    McpWorkerState,
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

    def is_ready(self) -> bool:
        return self.started

    def call_tool(self, name, arguments):
        return {"ok": True, "tool": name}


def test_mcp_process_preserves_structured_tool_error(monkeypatch):
    process = _make_mcp_process()
    monkeypatch.setattr(
        process,
        "_send_request_with_generation",
        lambda request, timeout_seconds: (
            {
                "error": {
                    "code": "invalid_musicxml",
                    "message": "Invalid MusicXML.",
                    "type": "InvalidMusicXmlError",
                    "retryable": False,
                }
            },
            4,
        ),
    )

    try:
        process.call_tool("parse_score", {})
    except McpToolError as exc:
        assert exc.code == "invalid_musicxml"
        assert exc.error_type == "InvalidMusicXmlError"
        assert exc.retryable is False
        assert exc.generation == 4
    else:
        raise AssertionError("Expected McpToolError")


def test_mcp_process_stop_is_atomic_and_idempotent():
    class FakePipe:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakePipe()
            self.stdout = FakePipe()
            self.stderr = FakePipe()
            self.terminate_count = 0

        def terminate(self) -> None:
            self.terminate_count += 1

        def wait(self, timeout):
            return 0

    child = FakeProcess()
    process = _make_mcp_process()
    with process._state_changed:
        process._proc = child
        process._generation = 1
        process._state = McpWorkerState.READY

    stop_threads = [threading.Thread(target=process.stop) for _ in range(4)]
    for thread in stop_threads:
        thread.start()
    for thread in stop_threads:
        thread.join(timeout=1)

    assert child.terminate_count == 1
    assert child.stdin.close_count == 1
    assert child.stdout.close_count == 1
    assert child.stderr.close_count == 1
    assert process._proc is None
    assert process.lifecycle_state == McpWorkerState.STOPPED


def test_concurrent_recovery_waits_for_old_exit_and_one_ready_replacement(monkeypatch):
    old_exit_release = threading.Event()
    replacement_spawned = threading.Event()
    discovery_started = threading.Event()
    discovery_release = threading.Event()

    class FakePipe:
        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, *, delayed_exit: bool) -> None:
            self.stdin = FakePipe()
            self.stdout = FakePipe()
            self.stderr = FakePipe()
            self.delayed_exit = delayed_exit
            self.terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True
            old_exit_release.set()

        def wait(self, timeout):
            if self.delayed_exit:
                assert old_exit_release.wait(timeout)
            return 0

    process = _make_mcp_process(startup_timeout_seconds=2.0)
    old_child = FakeProcess(delayed_exit=True)
    with process._state_changed:
        process._proc = old_child
        process._generation = 1
        process._state = McpWorkerState.READY

    replacements = []

    def spawn(*args, **kwargs):
        child = FakeProcess(delayed_exit=False)
        replacements.append(child)
        replacement_spawned.set()
        return child

    def discover(request, timeout_seconds, **kwargs):
        discovery_started.set()
        assert discovery_release.wait(timeout=timeout_seconds)
        return {"tools": []}, kwargs["expected_generation"]

    monkeypatch.setattr("src.backend.mcp_client.subprocess.Popen", spawn)
    monkeypatch.setattr(process, "_send_request_with_generation", discover)
    results = []
    errors = []
    threads = [
        threading.Thread(
            target=lambda: _capture_result_or_error(
                results,
                errors,
                lambda: process.recover(1),
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + 1.0
    while not old_child.terminated and time.monotonic() < deadline:
        time.sleep(0.005)
    assert old_child.terminated
    assert not replacement_spawned.is_set()

    old_exit_release.set()
    assert replacement_spawned.wait(timeout=1)
    assert discovery_started.wait(timeout=1)
    assert len(replacements) == 1
    assert process.lifecycle_state == McpWorkerState.STARTING
    assert any(thread.is_alive() for thread in threads)

    discovery_release.set()
    for thread in threads:
        thread.join(timeout=1)

    assert not errors
    assert results == [2, 2]
    assert len(replacements) == 1
    assert process.generation == 2
    assert process.is_ready() is True


def test_request_waits_for_recovery_and_uses_healthy_replacement(monkeypatch):
    discovery_started = threading.Event()
    discovery_release = threading.Event()

    class FakePipe:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakePipe()
            self.stdout = FakePipe()
            self.stderr = FakePipe()
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            self.exited = True

        def kill(self):
            self.exited = True

        def wait(self, timeout):
            self.exited = True
            return 0

    process = _make_mcp_process(startup_timeout_seconds=1.0)
    with process._state_changed:
        process._proc = FakeProcess()
        process._generation = 1
        process._state = McpWorkerState.READY

    replacements = []

    def spawn(*args, **kwargs):
        child = FakeProcess()
        replacements.append(child)
        return child

    def discover(request, timeout_seconds, **kwargs):
        discovery_started.set()
        assert discovery_release.wait(timeout=timeout_seconds)
        return {"tools": []}, kwargs["expected_generation"]

    monkeypatch.setattr("src.backend.mcp_client.subprocess.Popen", spawn)
    monkeypatch.setattr(process, "_send_request_with_generation", discover)
    recovery_errors = []
    recovery = threading.Thread(
        target=lambda: _capture_error(recovery_errors, lambda: process.recover(1))
    )
    recovery.start()
    assert discovery_started.wait(timeout=1)

    admitted = []

    def admit_request():
        worker, generation = process._acquire_ready_request(
            time.monotonic() + 1,
            expected_generation=None,
            allow_starting=False,
        )
        admitted.append((worker, generation))
        process._request_lock.release()

    request_b = threading.Thread(target=admit_request)
    request_b.start()
    time.sleep(0.05)
    assert request_b.is_alive()

    discovery_release.set()
    recovery.join(timeout=1)
    request_b.join(timeout=1)

    assert not recovery_errors
    assert admitted == [(replacements[0], 2)]
    assert len(replacements) == 1
    assert process.generation == 2


def test_old_generation_failure_reuses_newer_ready_worker(monkeypatch):
    process = _make_mcp_process()

    class LiveProcess:
        def poll(self):
            return None

    with process._state_changed:
        process._proc = LiveProcess()
        process._generation = 3
        process._state = McpWorkerState.READY
    monkeypatch.setattr(
        "src.backend.mcp_client.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("stale failure spawned another worker"),
    )

    assert process.recover(2) == 3


def test_failed_teardown_retains_child_and_blocks_replacement(monkeypatch):
    class StuckProcess:
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("stuck-worker", timeout)

    process = _make_mcp_process()
    old_child = StuckProcess()
    with process._state_changed:
        process._proc = old_child
        process._generation = 1
        process._state = McpWorkerState.READY
    monkeypatch.setattr(
        "src.backend.mcp_client.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("replacement started before old child exited"),
    )

    with pytest.raises(McpError, match="did not exit"):
        process.recover(1, deadline=time.monotonic() + 0.1)

    assert process._proc is old_child
    assert process.lifecycle_state == McpWorkerState.FAILED
    assert process.is_ready() is False


def test_recovery_failure_notifies_every_waiting_caller(monkeypatch):
    discovery_started = threading.Event()
    discovery_release = threading.Event()

    class FakePipe:
        def close(self):
            return None

    class FakeProcess:
        stdin = FakePipe()
        stdout = FakePipe()
        stderr = FakePipe()

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout):
            return 0

    process = _make_mcp_process()
    with process._state_changed:
        process._proc = FakeProcess()
        process._generation = 1
        process._state = McpWorkerState.READY
    spawn_count = 0

    def spawn(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return FakeProcess()

    def failed_discovery(*args, **kwargs):
        discovery_started.set()
        assert discovery_release.wait(timeout=1)
        raise McpError("replacement discovery failed")

    monkeypatch.setattr("src.backend.mcp_client.subprocess.Popen", spawn)
    monkeypatch.setattr(process, "_send_request_with_generation", failed_discovery)
    errors = []
    threads = [
        threading.Thread(
            target=lambda: _capture_error(errors, lambda: process.recover(1))
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert discovery_started.wait(timeout=1)
    deadline = time.monotonic() + 1
    while process._recovery_waiters.get(1, 0) != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert process._recovery_waiters.get(1, 0) == 1

    discovery_release.set()
    for thread in threads:
        thread.join(timeout=1)

    assert spawn_count == 1
    assert len(errors) == 2
    assert all("replacement discovery failed" in str(error) for error in errors)


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


def test_worker_unavailable_does_not_restart_or_replay():
    class UnavailableProcess(DummyProcess):
        def call_tool(self, name, arguments):
            raise McpWorkerUnavailableError(
                McpWorkerUnavailableError.user_message,
                generation=2,
            )

    router = McpRouter(Settings.from_env())
    process = UnavailableProcess()
    router._cpu = process
    router._gpu = DummyProcess()

    with pytest.raises(McpWorkerUnavailableError):
        router._call_with_retry("cpu", "parse_score", {})

    assert process.stop_count == 0
    assert process.start_count == 0


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


@pytest.mark.parametrize(
    "error",
    [
        McpError("MCP process closed unexpectedly."),
        McpRequestTimeoutError(
            "MCP request timed out: tools/call",
            method="tools/call",
            timeout_seconds=60,
        ),
        McpToolError({"message": "CUBLAS_STATUS_ALLOC_FAILED"}),
    ],
)
def test_mcp_router_shutdown_prevents_error_recovery_restart(error):
    call_started = threading.Event()
    release_call = threading.Event()

    class InterruptedProcess(DummyProcess):
        def call_tool(self, name, arguments):
            call_started.set()
            release_call.wait(timeout=1)
            raise error

    settings = Settings.from_env()
    router = McpRouter(settings)
    process = InterruptedProcess()
    process.started = True
    router._cpu = DummyProcess()
    router._gpu = process
    router._startup_ready.set()
    errors = []

    call_thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: router._call_with_retry("gpu", "synthesize", {}),
        )
    )
    call_thread.start()
    assert call_started.wait(timeout=1)

    router.stop()
    release_call.set()
    call_thread.join(timeout=1)

    assert not call_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], McpShuttingDownError)
    assert process.stop_count == 1
    assert process.start_count == 0
    assert router.readiness()["status"] == "draining"


def test_mcp_process_cannot_spawn_after_router_starts_draining(monkeypatch):
    router = McpRouter(Settings.from_env())
    router.begin_shutdown()
    monkeypatch.setattr(
        "src.backend.mcp_client.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("Worker spawned after shutdown began"),
    )

    with pytest.raises(McpShuttingDownError):
        router._gpu.start()


def test_mcp_router_shutdown_wakes_calls_waiting_for_startup():
    startup_entered = threading.Event()
    startup_release = threading.Event()

    class SlowProcess(DummyProcess):
        def start(self):
            startup_entered.set()
            startup_release.wait(timeout=1)
            super().start()

    router = McpRouter(Settings.from_env())
    router._cpu = SlowProcess()
    router._gpu = DummyProcess()
    errors = []
    call_thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: router.call_tool("list_voicebanks", {}),
        )
    )
    call_thread.start()
    assert startup_entered.wait(timeout=1)

    router.begin_shutdown()
    call_thread.join(timeout=0.2)

    assert not call_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], McpShuttingDownError)
    assert router._startup_future is not None
    assert not router._startup_future.done()

    startup_release.set()
    router._startup_thread.join(timeout=1)


def test_mcp_router_drain_boundary_times_out_while_spawn_gate_is_held():
    router = McpRouter(Settings.from_env())
    router._start_gate.acquire()
    try:
        with pytest.raises(McpError, match="draining boundary"):
            router.begin_shutdown(deadline=time.monotonic() + 0.02)
        assert router._stopping.is_set()
        assert not router._drain_established.is_set()
    finally:
        router._start_gate.release()

    router.begin_shutdown(deadline=time.monotonic() + 1)
    assert router._drain_established.is_set()


def test_request_admission_finishes_before_drain_boundary_is_established():
    request_passed_check = threading.Event()
    release_request = threading.Event()
    drain_finished = threading.Event()

    class ReadyProcess:
        def poll(self):
            return None

    router = McpRouter(Settings.from_env())
    process = router._cpu
    with process._state_changed:
        process._state = McpWorkerState.READY
        process._generation = 1
        process._proc = ReadyProcess()

    original_raise = process._raise_if_stopping
    request_checks = 0

    def pause_after_second_request_check():
        nonlocal request_checks
        original_raise()
        if threading.current_thread().name != "mcp-admission-test":
            return
        request_checks += 1
        if request_checks == 2:
            request_passed_check.set()
            assert release_request.wait(timeout=1)

    process._raise_if_stopping = pause_after_second_request_check
    admitted = []
    errors = []

    def admit_request():
        try:
            admitted.append(
                process._acquire_ready_request(
                    time.monotonic() + 1,
                    expected_generation=None,
                    allow_starting=False,
                )
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            if admitted:
                process._request_lock.release()

    request_thread = threading.Thread(target=admit_request, name="mcp-admission-test")
    request_thread.start()
    assert request_passed_check.wait(timeout=1)

    drain_errors = []

    def begin_draining():
        try:
            router.begin_shutdown(deadline=time.monotonic() + 1)
        except Exception as exc:
            drain_errors.append(exc)
        finally:
            drain_finished.set()

    drain_thread = threading.Thread(target=begin_draining)
    drain_thread.start()
    assert router._stopping.wait(timeout=1)
    assert not drain_finished.wait(timeout=0.05)
    assert not router._drain_established.is_set()

    release_request.set()
    request_thread.join(timeout=1)
    drain_thread.join(timeout=1)

    assert not errors
    assert not drain_errors
    assert admitted and admitted[0][1] == 1
    assert router._drain_established.is_set()


def test_shutdown_interrupts_real_child_during_discovery(tmp_path):
    settings = replace(
        Settings.from_env(),
        mcp_startup_timeout_seconds=10.0,
        backend_shutdown_worker_seconds=1.0,
    )
    router = McpRouter(settings)
    child_code = (
        "import sys,time; "
        "sys.stdin.readline(); "
        "time.sleep(30)"
    )
    router._cpu = McpProcess(
        name="held-discovery",
        args=[sys.executable, "-u", "-c", child_code],
        cwd=tmp_path,
        timeout_seconds=10.0,
        startup_timeout_seconds=10.0,
        pipe_stderr=False,
        start_gate=router._start_gate,
        stopping=router._stopping,
    )
    gpu = DummyProcess()
    router._gpu = gpu

    startup = router.start_background()
    deadline = time.monotonic() + 1
    while router._cpu.lifecycle_state != McpWorkerState.STARTING:
        if time.monotonic() >= deadline:
            raise AssertionError("child did not enter discovery")
        time.sleep(0.005)

    router.begin_shutdown()
    router.stop(deadline=time.monotonic() + 1)

    with pytest.raises(McpShuttingDownError):
        startup.result(timeout=1)
    assert router._cpu.lifecycle_state == McpWorkerState.STOPPED
    assert router._cpu._proc is None
    assert gpu.start_count == 0


def test_shutdown_claims_child_that_is_still_being_published(monkeypatch):
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    child_terminated = threading.Event()

    class FakePipe:
        def close(self):
            return None

    class FakeProcess:
        stdin = FakePipe()
        stdout = FakePipe()
        stderr = FakePipe()

        def poll(self):
            return 0 if child_terminated.is_set() else None

        def terminate(self):
            child_terminated.set()

        def kill(self):
            child_terminated.set()

        def wait(self, timeout):
            assert child_terminated.wait(timeout)
            return 0

    def spawn(*args, **kwargs):
        spawn_entered.set()
        assert release_spawn.wait(timeout=1)
        return FakeProcess()

    router = McpRouter(Settings.from_env())
    process = _make_mcp_process(startup_timeout_seconds=1.0)
    process._start_gate = router._start_gate
    process._stopping = router._stopping
    router._cpu = process
    gpu = DummyProcess()
    router._gpu = gpu
    monkeypatch.setattr("src.backend.mcp_client.subprocess.Popen", spawn)
    monkeypatch.setattr(
        process,
        "_send_request_with_generation",
        lambda *args, **kwargs: child_terminated.wait(timeout=1),
    )

    startup = router.start_background()
    assert spawn_entered.wait(timeout=1)
    drain_errors = []
    drain_finished = threading.Event()

    def begin_draining():
        try:
            router.begin_shutdown(deadline=time.monotonic() + 1)
        except Exception as exc:
            drain_errors.append(exc)
        finally:
            drain_finished.set()

    drain_thread = threading.Thread(target=begin_draining)
    drain_thread.start()
    assert router._stopping.wait(timeout=1)
    assert not drain_finished.wait(timeout=0.05)
    assert not router._drain_established.is_set()

    release_spawn.set()
    drain_thread.join(timeout=1)
    router.stop(deadline=time.monotonic() + 1)

    assert not drain_errors
    assert not drain_thread.is_alive()
    assert router._drain_established.is_set()
    with pytest.raises(McpShuttingDownError):
        startup.result(timeout=1)
    assert process._proc is None
    assert gpu.start_count == 0


def test_mcp_router_shutdown_cancels_background_start_before_gpu_start():
    cpu_starting = threading.Event()
    release_cpu = threading.Event()

    class SlowStartProcess(DummyProcess):
        def start(self):
            cpu_starting.set()
            release_cpu.wait(timeout=1)
            super().start()

    settings = Settings.from_env()
    router = McpRouter(settings)
    cpu = SlowStartProcess()
    gpu = DummyProcess()
    router._cpu = cpu
    router._gpu = gpu

    router.start_background()
    assert cpu_starting.wait(timeout=1)
    router.begin_shutdown()
    release_cpu.set()
    router._startup_thread.join(timeout=1)
    router.stop()

    assert gpu.start_count == 0
    assert router.readiness()["status"] == "draining"


def _capture_error(errors, callback):
    try:
        callback()
    except Exception as exc:
        errors.append(exc)


def _capture_result_or_error(results, errors, callback):
    try:
        results.append(callback())
    except Exception as exc:
        errors.append(exc)


def _make_mcp_process(*, startup_timeout_seconds=1.0):
    return McpProcess(
        name="test",
        args=["test-worker"],
        cwd=Path("."),
        timeout_seconds=1.0,
        startup_timeout_seconds=startup_timeout_seconds,
        pipe_stderr=False,
        start_gate=threading.Lock(),
        stopping=threading.Event(),
    )
