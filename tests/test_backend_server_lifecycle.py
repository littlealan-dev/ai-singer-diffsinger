import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from fastapi import FastAPI
import uvicorn

from src.backend.server import LifecycleServer
from src.backend.lifecycle import ShutdownCoordinator
from src.backend.orchestrator import Orchestrator


@dataclass
class _Settings:
    backend_shutdown_request_seconds: float = 0.15
    backend_shutdown_total_seconds: float = 1.0
    backend_shutdown_worker_seconds: float = 0.3


class _Coordinator:
    def __init__(self, total_seconds: float = 1.0) -> None:
        self._total_seconds = total_seconds
        self._deadline = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    def request_shutdown(self, started_at=None, *, serving):
        if self._deadline is None:
            self._deadline = (started_at or time.monotonic()) + self._total_seconds
            self.started.set()
        return self._deadline

    async def wait_until_draining(self):
        await self.started.wait()
        return self._deadline

    def request_remaining_seconds(self):
        return min(0.15, self.remaining_seconds())

    def http_drain_finished(self):
        return None

    def remaining_seconds(self):
        if self._deadline is None:
            return self._total_seconds
        return max(0.0, self._deadline - time.monotonic())

    async def finish(self):
        self.request_shutdown(serving=False)
        self.finished.set()

    def close(self):
        return None


class _FailingCoordinator(_Coordinator):
    async def wait_until_draining(self):
        await self.started.wait()
        raise RuntimeError("drain boundary unavailable")


class _Router:
    def __init__(self) -> None:
        self.draining = threading.Event()
        self.stopped = threading.Event()

    def begin_shutdown(self, *, deadline=None):
        self.draining.set()

    def stop(self, *, deadline=None):
        self.stopped.set()


class _FailingDrainRouter(_Router):
    def begin_shutdown(self, *, deadline=None):
        self.draining.set()
        raise RuntimeError("drain boundary unavailable")


class _Orchestrator:
    def begin_shutdown(self, deadline):
        pass

    async def shutdown_tasks(self, deadline):
        return True


class _IncompleteOrchestrator(_Orchestrator):
    async def shutdown_tasks(self, deadline):
        return False


def test_signal_hook_schedules_draining_on_server_loop(monkeypatch):
    async def scenario():
        test_app, coordinator = _make_app()
        server = LifecycleServer(
            uvicorn.Config(test_app, lifespan="off"),
            lifecycle_app=test_app,
        )
        server._loop = asyncio.get_running_loop()
        monkeypatch.setattr(uvicorn.Server, "handle_exit", lambda self, sig, frame: None)

        server.handle_exit(signal.SIGTERM, None)
        await asyncio.wait_for(coordinator.started.wait(), timeout=1)

    asyncio.run(scenario())


def test_pending_request_enters_draining_before_uvicorn_waits_and_exits_bounded():
    async def scenario():
        request_started = asyncio.Event()
        request_cancelled = asyncio.Event()
        test_app, coordinator = _make_app()

        @test_app.get("/hold")
        async def hold_request():
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            test_app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="critical",
        )
        server = LifecycleServer(config, lifecycle_app=test_app)
        serve_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await _wait_until(lambda: server.started)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /hold HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(request_started.wait(), timeout=1)

            shutdown_started = time.monotonic()
            server._start_draining()
            server.should_exit = True
            await asyncio.wait_for(coordinator.started.wait(), timeout=1)
            assert not request_cancelled.is_set()

            await asyncio.wait_for(serve_task, timeout=1)
            assert request_cancelled.is_set()
            assert coordinator.finished.is_set()
            assert time.monotonic() - shutdown_started < 0.8
            writer.close()
            await writer.wait_closed()
            await reader.read()
        finally:
            listener.close()
            if not serve_task.done():
                server.force_exit = True
                server.should_exit = True
                serve_task.cancel()
                await asyncio.gather(serve_task, return_exceptions=True)

    asyncio.run(scenario())


def test_server_fallback_settles_tasks_after_drain_observer_failure(monkeypatch):
    async def scenario():
        test_app = FastAPI()
        coordinator = _FailingCoordinator()
        test_app.state.shutdown_coordinator = coordinator
        test_app.state.settings = _Settings()
        server = LifecycleServer(
            uvicorn.Config(test_app, lifespan="off"),
            lifecycle_app=test_app,
        )

        async def no_op_serve(_server, sockets):
            return None

        monkeypatch.setattr(uvicorn.Server, "_serve", no_op_serve)
        await server._serve()

        assert coordinator.finished.is_set()

    asyncio.run(scenario())


def test_server_fallback_settles_tasks_after_total_deadline(monkeypatch):
    async def scenario():
        test_app = FastAPI()
        coordinator = _Coordinator(total_seconds=0)
        test_app.state.shutdown_coordinator = coordinator
        test_app.state.settings = _Settings()
        server = LifecycleServer(
            uvicorn.Config(test_app, lifespan="off"),
            lifecycle_app=test_app,
        )

        async def no_op_serve(_server, sockets):
            return None

        monkeypatch.setattr(uvicorn.Server, "_serve", no_op_serve)
        await server._serve()

        assert coordinator.finished.is_set()

    asyncio.run(scenario())


def test_server_closes_connections_when_drain_observer_fails(monkeypatch):
    async def scenario():
        test_app = FastAPI()
        coordinator = _FailingCoordinator()
        test_app.state.shutdown_coordinator = coordinator
        test_app.state.settings = _Settings()
        server = LifecycleServer(
            uvicorn.Config(test_app, lifespan="off"),
            lifecycle_app=test_app,
        )
        base_shutdown_called = asyncio.Event()

        async def record_shutdown(_server, sockets):
            base_shutdown_called.set()

        monkeypatch.setattr(uvicorn.Server, "shutdown", record_shutdown)
        try:
            await server.shutdown()
        except RuntimeError as exc:
            assert "drain boundary unavailable" in str(exc)
        else:
            raise AssertionError("Expected the drain boundary failure")

        assert base_shutdown_called.is_set()
        assert server._shutdown_completed is True

    asyncio.run(scenario())


def test_shutdown_controller_runs_while_default_executor_is_saturated():
    async def scenario():
        release_executor = threading.Event()
        loop = asyncio.get_running_loop()
        default_executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(default_executor)
        occupied = loop.run_in_executor(None, release_executor.wait)
        router = _Router()
        coordinator = ShutdownCoordinator(
            router=router,
            orchestrator=_Orchestrator(),
            export_tasks={},
            settings=_Settings(),
        )
        coordinator.initialize()
        try:
            coordinator.request_shutdown(serving=False)
            await asyncio.wait_for(coordinator.wait_until_draining(), timeout=0.2)
            assert router.draining.is_set()
            assert router.stopped.wait(timeout=0.2)
            await asyncio.wait_for(coordinator.finish(), timeout=0.2)
        finally:
            coordinator.close()
            release_executor.set()
            await occupied
            default_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_orchestrator_reports_task_still_pending_at_shutdown_deadline():
    async def scenario():
        task_started = asyncio.Event()
        release_task = asyncio.Event()

        async def cancellation_resistant_task():
            task_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_task.wait()

        task = asyncio.create_task(cancellation_resistant_task())
        await task_started.wait()
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator._synthesis_tasks = {"job": task}
        orchestrator._preprocess_tasks = {}
        orchestrator._billing_finalization_tasks = {}
        orchestrator._shutdown_finalization_failures = set()
        orchestrator._shutdown_deadline = None
        orchestrator._shutdown_requested = asyncio.Event()
        orchestrator._logger = logging.getLogger("test.orchestrator.shutdown")
        try:
            complete = await orchestrator.shutdown_tasks(time.monotonic() + 0.02)
            assert complete is False
            assert not task.done()
        finally:
            release_task.set()
            await task

    asyncio.run(scenario())


def test_coordinator_reports_incomplete_when_orchestrator_tasks_remain(caplog):
    async def scenario():
        coordinator = ShutdownCoordinator(
            router=_Router(),
            orchestrator=_IncompleteOrchestrator(),
            export_tasks={},
            settings=_Settings(),
        )
        coordinator.initialize()
        try:
            await coordinator.finish()
        finally:
            coordinator.close()

    caplog.set_level(logging.INFO)
    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("backend_shutdown_incomplete") for message in messages)
    assert not any(message.startswith("backend_shutdown_complete") for message in messages)


def test_coordinator_preserves_drain_failure_and_attempts_worker_cleanup(caplog):
    async def scenario():
        router = _FailingDrainRouter()
        coordinator = ShutdownCoordinator(
            router=router,
            orchestrator=_Orchestrator(),
            export_tasks={},
            settings=_Settings(),
        )
        coordinator.initialize()
        try:
            coordinator.request_shutdown(serving=False)
            try:
                await coordinator.wait_until_draining()
            except RuntimeError as exc:
                assert "drain boundary unavailable" in str(exc)
            else:
                raise AssertionError("Expected the drain boundary failure")
            await coordinator.finish()
            assert router.stopped.is_set()
        finally:
            coordinator.close()

    caplog.set_level(logging.INFO)
    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert any(message == "backend_shutdown_draining_failed" for message in messages)
    assert any(message.startswith("backend_shutdown_incomplete") for message in messages)
    assert not any(message.startswith("backend_shutdown_complete") for message in messages)


def test_sigterm_during_blocking_discovery_reaps_child_and_skips_gpu(tmp_path):
    child_marker = tmp_path / "child.pid"
    gpu_marker = tmp_path / "gpu.started"
    helper = Path(__file__).parent / "helpers" / "mcp_lifecycle_server.py"
    project_root = Path(__file__).parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(project_root), env.get("PYTHONPATH", "")) if part
    )
    process = subprocess.Popen(
        [sys.executable, "-u", str(helper), str(child_marker), str(gpu_marker)],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(child_marker, process, timeout=8)
        child_pid = int(child_marker.read_text(encoding="utf-8"))
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode in {0, -signal.SIGTERM}, (stdout, stderr)
        assert not gpu_marker.exists()
        _wait_for_process_exit(child_pid, timeout=1)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)


def test_sigterm_returns_503_to_request_waiting_during_deferred_startup(tmp_path):
    child_marker = tmp_path / "child.pid"
    gpu_marker = tmp_path / "gpu.started"
    request_waiting_marker = tmp_path / "request.waiting"
    response_ready_marker = tmp_path / "response.ready"
    child_terminated_marker = tmp_path / "child.terminated"
    startup_settled_marker = tmp_path / "startup.settled"
    helper = Path(__file__).parent / "helpers" / "mcp_lifecycle_deferred_server.py"
    project_root = Path(__file__).parents[1]
    port = _reserve_local_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(project_root), env.get("PYTHONPATH", "")) if part
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(helper),
            str(child_marker),
            str(gpu_marker),
            str(request_waiting_marker),
            str(response_ready_marker),
            str(child_terminated_marker),
            str(startup_settled_marker),
            str(port),
        ],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    request_result = {}
    request_errors = []

    def issue_request() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request("GET", "/mcp")
            response = connection.getresponse()
            request_result["status"] = response.status
            request_result["payload"] = json.loads(response.read())
            request_result["received_at"] = time.monotonic()
        except Exception as exc:
            request_errors.append(exc)
        finally:
            connection.close()

    request_thread = threading.Thread(target=issue_request)
    try:
        _wait_for_file(child_marker, process, timeout=8)
        child_pid = int(child_marker.read_text(encoding="utf-8"))
        _wait_for_server(port, process, timeout=3)
        request_thread.start()
        _wait_for_file(request_waiting_marker, process, timeout=2)

        shutdown_started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        request_thread.join(timeout=3)
        assert not request_thread.is_alive()
        assert not request_errors
        assert request_result["status"] == 503
        assert request_result["payload"] == {
            "detail": {
                "code": "backend_shutting_down",
                "message": "SightSinger is restarting. Please try again in a moment.",
            }
        }

        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode in {0, -signal.SIGTERM}, (stdout, stderr)
        assert time.monotonic() - shutdown_started < 3
        assert response_ready_marker.exists()
        assert child_terminated_marker.exists()
        assert startup_settled_marker.exists()
        response_ready_at = float(response_ready_marker.read_text(encoding="utf-8"))
        child_terminated_at = float(
            child_terminated_marker.read_text(encoding="utf-8")
        )
        startup_settled_at = float(
            startup_settled_marker.read_text(encoding="utf-8")
        )
        assert (
            response_ready_at
            <= request_result["received_at"]
            <= child_terminated_at
            <= startup_settled_at
        )
        assert not gpu_marker.exists()
        _wait_for_process_exit(child_pid, timeout=1)
    finally:
        if request_thread.is_alive():
            request_thread.join(timeout=1)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)


def _make_app():
    test_app = FastAPI()
    coordinator = _Coordinator()
    test_app.state.shutdown_coordinator = coordinator
    test_app.state.settings = _Settings()
    return test_app, coordinator


async def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.01)


def _wait_for_file(path: Path, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"server exited before startup marker: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError("server did not start the controlled MCP child")
        time.sleep(0.01)


def _wait_for_process_exit(pid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"MCP child {pid} remained alive after server shutdown")
        time.sleep(0.01)


def _reserve_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(
    port: int,
    process: subprocess.Popen[str],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"server exited before listening: {stdout}\n{stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError("deferred lifecycle server did not start listening")
            time.sleep(0.01)
