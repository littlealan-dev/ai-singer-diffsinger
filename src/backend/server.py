from __future__ import annotations

"""Lifecycle-aware Uvicorn entrypoint for the SightSinger backend."""

import argparse
import asyncio
import logging
import time
from types import FrameType
from typing import Any, Sequence

import uvicorn

from src.backend.main import app


class LifecycleServer(uvicorn.Server):
    """Begin application draining before Uvicorn waits for active requests."""

    def __init__(self, config: uvicorn.Config, *, lifecycle_app: Any = app) -> None:
        super().__init__(config)
        self._lifecycle_app = lifecycle_app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._draining_task: asyncio.Task[float] | None = None
        self._shutdown_started_at: float | None = None
        self._shutdown_completed = False

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        super().handle_exit(sig, frame)
        lifespan = getattr(self, "lifespan", None)
        if lifespan is not None:
            lifespan.should_exit = True
        if self._shutdown_started_at is None:
            self._shutdown_started_at = time.monotonic()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._start_draining)

    async def _serve(self, sockets: list | None = None) -> None:
        """Keep fallback cleanup inside Uvicorn's captured-signal scope."""
        self._loop = asyncio.get_running_loop()
        try:
            await super()._serve(sockets)
        finally:
            coordinator = self._lifecycle_app.state.shutdown_coordinator
            try:
                if self.started and not self._shutdown_completed:
                    await self.shutdown(sockets)
            except Exception:
                logging.getLogger(__name__).exception("backend_server_shutdown_failed")
            try:
                await self._ensure_draining()
            except Exception:
                logging.getLogger(__name__).exception("backend_shutdown_draining_observer_failed")
            try:
                remaining = coordinator.remaining_seconds()
                if remaining <= 0:
                    logging.getLogger(__name__).error("backend_shutdown_deadline_exceeded")
                    await coordinator.finish()
                else:
                    await asyncio.wait_for(coordinator.finish(), timeout=remaining)
            except Exception:
                logging.getLogger(__name__).exception("backend_shutdown_fallback_failed")
            finally:
                close = getattr(coordinator, "close", None)
                if close is not None:
                    close()

    async def shutdown(self, sockets: list | None = None) -> None:
        coordinator = self._lifecycle_app.state.shutdown_coordinator
        draining_error: Exception | None = None
        try:
            await self._ensure_draining()
        except Exception as exc:
            draining_error = exc
        request_budget = min(
            coordinator.request_remaining_seconds(),
            coordinator.remaining_seconds(),
        )
        self.config.timeout_graceful_shutdown = max(0.001, request_budget)
        try:
            await super().shutdown(sockets)
        finally:
            coordinator.http_drain_finished()
            self._shutdown_completed = True
        if draining_error is not None:
            raise draining_error

    def _start_draining(self) -> None:
        if self._draining_task is None:
            if self._shutdown_started_at is None:
                self._shutdown_started_at = time.monotonic()
            self._draining_task = asyncio.create_task(
                self._begin_draining(),
                name="backend-begin-draining",
            )

    async def _begin_draining(self) -> float:
        coordinator = self._lifecycle_app.state.shutdown_coordinator
        deadline = coordinator.request_shutdown(
            self._shutdown_started_at,
            serving=bool(self.started),
        )
        await coordinator.wait_until_draining()
        return deadline

    async def _ensure_draining(self) -> float:
        self._start_draining()
        assert self._draining_task is not None
        return await self._draining_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the SightSinger backend.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--access-log", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=args.access_log,
        timeout_graceful_shutdown=app.state.settings.backend_shutdown_request_seconds,
    )
    server = LifecycleServer(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
