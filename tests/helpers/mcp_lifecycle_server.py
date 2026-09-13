from __future__ import annotations

"""Run a lifecycle server whose CPU worker blocks during discovery."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys

from fastapi import FastAPI
import uvicorn

from src.backend.config import Settings
from src.backend.lifecycle import ShutdownCoordinator
from src.backend.mcp_client import McpProcess, McpRouter
from src.backend.server import LifecycleServer


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
    settings = replace(
        Settings.from_env(),
        mcp_startup_blocking=True,
        mcp_startup_timeout_seconds=10.0,
        backend_shutdown_total_seconds=2.0,
        backend_shutdown_request_seconds=0.25,
        backend_shutdown_worker_seconds=1.0,
    )
    router = McpRouter(settings)
    router._cpu = McpProcess(
        name="held-discovery",
        args=[
            sys.executable,
            "-u",
            str(Path(__file__).with_name("mcp_lifecycle_worker.py")),
            str(child_marker),
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
        try:
            await asyncio.wrap_future(router.start_background())
            yield
        finally:
            coordinator.http_drain_finished()
            try:
                await coordinator.finish()
            finally:
                coordinator.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.shutdown_coordinator = coordinator
    server = LifecycleServer(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on", log_level="critical"),
        lifecycle_app=app,
    )
    server.run()


if __name__ == "__main__":
    main()
