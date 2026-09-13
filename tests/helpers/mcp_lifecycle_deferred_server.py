from __future__ import annotations

"""Run a deferred-startup server with an HTTP request waiting on MCP discovery."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time

from fastapi import FastAPI, HTTPException
import uvicorn

from src.backend.config import Settings
from src.backend.lifecycle import ShutdownCoordinator
from src.backend.mcp_client import McpProcess, McpRouter, McpShuttingDownError
from src.backend.server import LifecycleServer


class _ObservedCondition(threading.Condition):
    def __init__(self, lock: threading.Lock, waiting_marker: Path) -> None:
        super().__init__(lock)
        self._waiting_marker = waiting_marker

    def wait(self, timeout: float | None = None) -> bool:
        self._waiting_marker.write_text(str(time.monotonic()), encoding="utf-8")
        return super().wait(timeout)


class _GpuWorker:
    def __init__(self, marker: Path) -> None:
        self._marker = marker

    def start(self) -> None:
        self._marker.write_text("started", encoding="utf-8")

    def stop(self) -> None:
        return None

    def is_ready(self) -> bool:
        return False


class _Orchestrator:
    def begin_shutdown(self, deadline):
        pass

    async def shutdown_tasks(self, deadline: float) -> bool:
        return True


def main() -> None:
    child_marker = Path(sys.argv[1])
    gpu_marker = Path(sys.argv[2])
    request_waiting_marker = Path(sys.argv[3])
    response_ready_marker = Path(sys.argv[4])
    child_terminated_marker = Path(sys.argv[5])
    startup_settled_marker = Path(sys.argv[6])
    port = int(sys.argv[7])
    settings = replace(
        Settings.from_env(),
        mcp_startup_blocking=False,
        mcp_startup_timeout_seconds=10.0,
        backend_ready_timeout_seconds=10.0,
        backend_shutdown_total_seconds=2.0,
        backend_shutdown_request_seconds=0.5,
        backend_shutdown_worker_seconds=1.0,
    )
    router = McpRouter(settings)
    router._startup_changed = _ObservedCondition(
        router._startup_lock,
        request_waiting_marker,
    )
    router._cpu = McpProcess(
        name="held-deferred-discovery",
        args=[
            sys.executable,
            "-u",
            str(Path(__file__).with_name("mcp_lifecycle_worker.py")),
            str(child_marker),
            str(child_terminated_marker),
        ],
        cwd=settings.project_root,
        timeout_seconds=10.0,
        startup_timeout_seconds=10.0,
        pipe_stderr=False,
        start_gate=router._start_gate,
        stopping=router._stopping,
    )
    router._gpu = _GpuWorker(gpu_marker)
    coordinator = ShutdownCoordinator(
        router=router,
        orchestrator=_Orchestrator(),
        export_tasks={},
        settings=settings,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        coordinator.initialize()
        startup_future = router.start_background()
        try:
            yield
        finally:
            coordinator.http_drain_finished()
            try:
                await coordinator.finish()
                if not startup_future.done():
                    raise RuntimeError("MCP startup future remained pending after shutdown")
                startup_error = startup_future.exception()
                if not isinstance(startup_error, McpShuttingDownError):
                    raise RuntimeError(
                        "MCP startup did not settle with the expected shutdown error"
                    )
                startup_thread = router._startup_thread
                if startup_thread is not None and startup_thread.is_alive():
                    raise RuntimeError("MCP startup thread remained alive after shutdown")
                startup_settled_marker.write_text(
                    str(time.monotonic()),
                    encoding="utf-8",
                )
            finally:
                coordinator.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.shutdown_coordinator = coordinator

    @app.get("/mcp")
    async def wait_for_mcp() -> dict[str, bool]:
        try:
            await asyncio.to_thread(router.call_tool, "parse_score", {})
        except McpShuttingDownError as exc:
            response_ready_marker.write_text(str(time.monotonic()), encoding="utf-8")
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.user_message},
            ) from exc
        return {"ok": True}

    server = LifecycleServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="critical",
        ),
        lifecycle_app=app,
    )
    server.run()


if __name__ == "__main__":
    main()
