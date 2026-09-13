from __future__ import annotations

"""Coordinate bounded backend shutdown across server and application layers."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import threading
import time
from typing import Any, MutableMapping

from src.backend.config import Settings
from src.backend.mcp_client import McpRouter
from src.backend.orchestrator import Orchestrator


class ShutdownCoordinator:
    """Own one shutdown timeline and one independently scheduled cleanup operation."""

    def __init__(
        self,
        *,
        router: McpRouter,
        orchestrator: Orchestrator,
        export_tasks: MutableMapping[str, asyncio.Task[Any]],
        settings: Settings,
    ) -> None:
        self._router = router
        self._orchestrator = orchestrator
        self._export_tasks = export_tasks
        self._settings = settings
        self._state_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._started_at: float | None = None
        self._deadline: float | None = None
        self._request_deadline: float | None = None
        self._serving = False
        self._draining: Future[float] | None = None
        self._essential_cleanup: Future[None] | None = None
        self._control_future: Future[None] | None = None
        self._http_drained = threading.Event()
        self._finish_task: asyncio.Task[None] | None = None
        self._closed = False
        self._logger = logging.getLogger(__name__)

    @property
    def started(self) -> bool:
        with self._state_lock:
            return self._started_at is not None

    @property
    def deadline(self) -> float | None:
        with self._state_lock:
            return self._deadline

    def initialize(self) -> None:
        """Create and prestart execution capacity reserved for shutdown control."""
        with self._state_lock:
            if self._executor is not None:
                return
            if self._closed:
                raise RuntimeError("Shutdown coordinator is closed.")
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="backend-shutdown",
            )
            self._executor = executor
        executor.submit(lambda: None).result()

    def request_shutdown(
        self,
        started_at: float | None = None,
        *,
        serving: bool,
    ) -> float:
        """Start the shared shutdown controller and return its fixed deadline."""
        with self._state_lock:
            if self._deadline is not None:
                return self._deadline
        self.initialize()
        with self._state_lock:
            if self._deadline is not None:
                return self._deadline
            started_at = started_at if started_at is not None else time.monotonic()
            self._started_at = started_at
            self._deadline = started_at + self._settings.backend_shutdown_total_seconds
            self._request_deadline = min(
                started_at + self._settings.backend_shutdown_request_seconds,
                self._deadline,
            )
            self._serving = serving
            self._draining = Future()
            self._essential_cleanup = Future()
            executor = self._executor
            assert executor is not None
            self._orchestrator.begin_shutdown(self._deadline)
            self._control_future = executor.submit(self._run_shutdown)
            deadline = self._deadline
        self._logger.info(
            "backend_shutdown_begin total_budget_seconds=%.2f serving=%s",
            self._settings.backend_shutdown_total_seconds,
            serving,
        )
        return deadline

    def begin(self, started_at: float | None = None) -> float:
        """Compatibility entrypoint for non-server shutdown callers."""
        return self.request_shutdown(started_at, serving=False)

    async def begin_async(self, started_at: float | None = None) -> float:
        deadline = self.request_shutdown(started_at, serving=False)
        await self.wait_until_draining()
        return deadline

    def _run_shutdown(self) -> None:
        draining = self._draining
        essential = self._essential_cleanup
        assert draining is not None and essential is not None
        shutdown_error: BaseException | None = None
        try:
            self._router.begin_shutdown(deadline=self.deadline)
        except BaseException as exc:
            shutdown_error = exc
            if not draining.done():
                draining.set_exception(exc)
            self._logger.exception("backend_shutdown_draining_failed")
        else:
            if not draining.done():
                draining.set_result(self.deadline or time.monotonic())
            with self._state_lock:
                serving = self._serving
                request_deadline = self._request_deadline
            if serving and request_deadline is not None:
                self._http_drained.wait(max(0.0, request_deadline - time.monotonic()))

        with self._state_lock:
            total_deadline = self._deadline
        if total_deadline is None:
            return
        try:
            worker_deadline = min(
                total_deadline,
                time.monotonic() + self._settings.backend_shutdown_worker_seconds,
            )
            self._router.stop(deadline=worker_deadline)
        except BaseException as exc:
            if shutdown_error is None:
                shutdown_error = exc
            self._logger.exception("backend_shutdown_worker_cleanup_failed")
        if shutdown_error is None:
            if not essential.done():
                essential.set_result(None)
        elif not essential.done():
            essential.set_exception(shutdown_error)

    async def wait_until_draining(self) -> float:
        """Wait for admission to close without consuming a request-executor thread."""
        with self._state_lock:
            future = self._draining
            deadline = self._deadline
        if future is None or deadline is None:
            raise RuntimeError("Shutdown has not been requested.")
        return await self._await_future(future, deadline)

    def http_drain_finished(self) -> None:
        """Tell the controller that Uvicorn has finished its request drain."""
        self._http_drained.set()

    def remaining_seconds(self) -> float:
        deadline = self.deadline
        if deadline is None:
            return self._settings.backend_shutdown_total_seconds
        return max(0.0, deadline - time.monotonic())

    def request_remaining_seconds(self) -> float:
        with self._state_lock:
            deadline = self._request_deadline
        if deadline is None:
            return self._settings.backend_shutdown_request_seconds
        return max(0.0, deadline - time.monotonic())

    async def finish(self) -> None:
        """Join essential cleanup and settle application tasks once."""
        if not self.started:
            self.request_shutdown(serving=False)
        self.http_drain_finished()
        with self._state_lock:
            if self._finish_task is None:
                self._finish_task = asyncio.create_task(
                    self._run_task_settlement(),
                    name="backend-shutdown-cleanup",
                )
            task = self._finish_task
        await asyncio.shield(task)

    async def _run_task_settlement(self) -> None:
        with self._state_lock:
            essential = self._essential_cleanup
            deadline = self._deadline
        if essential is None or deadline is None:
            return
        cleanup_complete = True
        try:
            await self._await_future(essential, deadline)
        except asyncio.TimeoutError:
            cleanup_complete = False
            self._logger.error("backend_shutdown_worker_cleanup_deadline_exceeded")
        except Exception:
            cleanup_complete = False
            pass

        try:
            orchestrator_complete = await self._orchestrator.shutdown_tasks(deadline)
        except Exception:
            orchestrator_complete = False
            cleanup_complete = False
            self._logger.exception("backend_shutdown_orchestrator_cleanup_failed")

        export_complete = await self._cancel_tasks(
            self._export_tasks,
            "export",
            deadline,
        )
        cleanup_complete = (
            cleanup_complete and orchestrator_complete and export_complete
        )
        elapsed = self._settings.backend_shutdown_total_seconds - self.remaining_seconds()
        if cleanup_complete:
            self._logger.info(
                "backend_shutdown_complete elapsed_seconds=%.2f remaining_seconds=%.2f",
                elapsed,
                self.remaining_seconds(),
            )
        else:
            self._logger.warning(
                "backend_shutdown_incomplete elapsed_seconds=%.2f remaining_seconds=%.2f",
                elapsed,
                self.remaining_seconds(),
            )

    async def _await_future(self, future: Future[Any], deadline: float) -> Any:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise asyncio.TimeoutError
        observer = asyncio.wrap_future(future)
        return await asyncio.wait_for(asyncio.shield(observer), timeout=remaining)

    def close(self) -> None:
        """Release the private executor after shutdown observation has completed."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=False)

    async def _cancel_tasks(
        self,
        tasks_by_id: MutableMapping[str, asyncio.Task[Any]],
        kind: str,
        deadline: float,
    ) -> bool:
        tasks = [task for task in list(tasks_by_id.values()) if not task.done()]
        if not tasks:
            return True
        for task in tasks:
            task.cancel()
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            self._logger.warning(
                "backend_shutdown_tasks_incomplete kind=%s count=%s", kind, len(tasks)
            )
            return False
        _, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            self._logger.warning(
                "backend_shutdown_tasks_incomplete kind=%s count=%s", kind, len(pending)
            )
            return False
        return True
