from __future__ import annotations

"""Client wrapper to run MCP server processes and route tool calls."""

from dataclasses import dataclass
from concurrent.futures import Future
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, NoReturn, Optional
import json
import logging
import select
import subprocess
import threading
import time
import sys

from src.backend.config import Settings
from src.backend.gpu_errors import classify_gpu_tool_error


class McpError(RuntimeError):
    """Raised when an MCP request fails or times out."""

    def __init__(self, message: str, *, generation: int | None = None) -> None:
        super().__init__(message)
        self.generation = generation


class McpToolError(McpError):
    """Raised when a healthy MCP worker reports a tool execution error."""

    def __init__(self, error: Any, *, generation: int | None = None) -> None:
        payload = dict(error) if isinstance(error, dict) else {"message": str(error)}
        self.payload = payload
        self.code = str(payload.get("code") or "") or None
        self.error_type = str(payload.get("type") or "") or None
        self.retryable = bool(payload.get("retryable", False))
        self.worker_restart_required = bool(payload.get("workerRestartRequired", False))
        super().__init__(str(payload.get("message") or error), generation=generation)


class McpRequestTimeoutError(McpError):
    """Raised when a single MCP JSON-RPC request exceeds its timeout."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        timeout_seconds: float,
        generation: int | None = None,
    ) -> None:
        super().__init__(message, generation=generation)
        self.method = method
        self.timeout_seconds = timeout_seconds


class McpStartupInProgressError(McpError):
    """Raised when MCP workers are still warming up."""

    code = "backend_starting"
    user_message = "SightSinger is still starting up. Please try again in a moment."


class McpShuttingDownError(McpError):
    """Raised when an MCP operation is interrupted by application shutdown."""

    code = "backend_shutting_down"
    user_message = "SightSinger is restarting. Please try again in a moment."


class McpWorkerUnavailableError(McpError):
    """Raised when a request cannot be admitted to a usable MCP worker."""

    code = "backend_unavailable"
    user_message = "SightSinger is temporarily unavailable. Please try again in a moment."


@dataclass(frozen=True)
class McpRequest:
    """JSON-RPC request payload for MCP tools."""
    method: str
    params: Dict[str, Any]


class McpWorkerState(str, Enum):
    """Lifecycle state for one MCP subprocess slot."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


class McpProcess:
    """Manage a single MCP server subprocess and JSON-RPC messaging."""
    def __init__(
        self,
        name: str,
        args: Iterable[str],
        cwd: Path,
        timeout_seconds: float,
        startup_timeout_seconds: float,
        pipe_stderr: bool,
        start_gate: threading.Lock,
        stopping: threading.Event,
    ) -> None:
        self._name = name
        self._args = list(args)
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._pipe_stderr = pipe_stderr
        self._proc: Optional[subprocess.Popen[str]] = None
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        self._start_gate = start_gate
        self._stopping = stopping
        self._state = McpWorkerState.STOPPED
        self._generation = 0
        self._operation = 0
        self._state_error: Exception | None = None
        self._active_recovery: int | None = None
        self._next_recovery = 0
        self._recovery_outcomes: Dict[int, tuple[bool, Exception | None, int]] = {}
        self._recovery_waiters: Dict[int, int] = {}
        self._next_id = 1
        self._logger = logging.getLogger(__name__)
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def generation(self) -> int:
        """Return the generation currently assigned to this worker slot."""
        with self._state_changed:
            return self._generation

    @property
    def lifecycle_state(self) -> McpWorkerState:
        """Return a snapshot of the worker lifecycle state."""
        with self._state_changed:
            return self._state

    def start(
        self,
        *,
        deadline: float | None = None,
        _recovery_token: int | None = None,
    ) -> int:
        """Start the MCP subprocess and wait for tool discovery."""
        while True:
            wait_operation: int | None = None
            wait_recovery: int | None = None
            spawn = False
            with self._start_gate:
                self._raise_if_stopping()
                with self._state_changed:
                    if (
                        self._active_recovery is not None
                        and self._active_recovery != _recovery_token
                    ):
                        wait_recovery = self._active_recovery
                        self._recovery_waiters[wait_recovery] = (
                            self._recovery_waiters.get(wait_recovery, 0) + 1
                        )
                    elif self._state == McpWorkerState.READY:
                        if self._proc is not None and self._proc.poll() is None:
                            return self._generation
                        self._state = McpWorkerState.FAILED
                        self._state_error = McpError(
                            "MCP process exited after becoming ready.",
                            generation=self._generation,
                        )
                        self._state_changed.notify_all()
                        self._raise_state_error()
                    elif self._state in {
                        McpWorkerState.STARTING,
                        McpWorkerState.STOPPING,
                    }:
                        wait_operation = self._operation
                    elif self._state == McpWorkerState.FAILED:
                        self._raise_state_error()
                    else:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise McpError("MCP startup deadline expired.")
                        self._operation += 1
                        operation = self._operation
                        self._generation += 1
                        generation = self._generation
                        self._state = McpWorkerState.STARTING
                        self._state_error = None
                        start_time = time.monotonic()
                        self._logger.info(
                            "mcp_start_begin name=%s generation=%s",
                            self._name,
                            generation,
                        )
                        spawn = True

                if spawn:
                    try:
                        proc = subprocess.Popen(
                            self._args,
                            cwd=self._cwd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE if self._pipe_stderr else None,
                            text=True,
                        )
                    except Exception as exc:
                        with self._state_changed:
                            if self._operation == operation:
                                self._state = McpWorkerState.FAILED
                                self._state_error = exc
                                self._state_changed.notify_all()
                        raise
                    with self._state_changed:
                        if self._operation != operation:
                            raise McpShuttingDownError(
                                "MCP startup ownership changed during process creation.",
                                generation=generation,
                            )
                        self._proc = proc
                        self._state_changed.notify_all()
                    break

            if wait_recovery is not None:
                self._wait_for_recovery(wait_recovery, deadline)
                continue
            if wait_operation is not None:
                self._wait_for_operation(wait_operation, deadline)
                continue

        if self._pipe_stderr:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(proc,),
                name="mcp-stderr",
                daemon=True,
            )
            self._stderr_thread.start()
        try:
            self._raise_if_stopping()
            tools_start = time.monotonic()
            startup_timeout = self._bounded_timeout(
                self._startup_timeout_seconds,
                deadline,
                "MCP startup deadline expired.",
            )
            self._send_request_with_generation(
                McpRequest(method="tools/list", params={}),
                timeout_seconds=startup_timeout,
                expected_generation=generation,
                allow_starting=True,
            )
            with self._state_changed:
                if self._stopping.is_set():
                    raise McpShuttingDownError(
                        "MCP startup was interrupted by shutdown.",
                        generation=generation,
                    )
                if (
                    self._operation != operation
                    or self._state != McpWorkerState.STARTING
                    or self._proc is not proc
                ):
                    raise McpShuttingDownError(
                        "MCP startup ownership changed before it became ready.",
                        generation=generation,
                    )
                if proc.poll() is not None:
                    raise McpError(
                        "MCP process exited during startup.",
                        generation=generation,
                    )
                self._state = McpWorkerState.READY
                self._state_error = None
                self._state_changed.notify_all()
        except Exception as exc:
            self._complete_failed_start(operation, generation, proc, exc, deadline)
            raise
        tools_ms = (time.monotonic() - tools_start) * 1000.0
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        self._logger.info(
            "mcp_start_ready name=%s generation=%s elapsed_ms=%.2f tools_list_ms=%.2f",
            self._name,
            generation,
            elapsed_ms,
            tools_ms,
        )
        return generation

    def stop(
        self,
        *,
        deadline: float | None = None,
        _recovery_token: int | None = None,
    ) -> None:
        """Stop the MCP subprocess and close pipes."""
        if deadline is None:
            deadline = time.monotonic() + 4.0
        while True:
            wait_for_start_publication = False
            with self._state_changed:
                if self._state == McpWorkerState.STOPPED:
                    return
                if self._state == McpWorkerState.STOPPING:
                    operation = self._operation
                    owner = False
                elif self._state == McpWorkerState.STARTING and self._proc is None:
                    operation = self._operation
                    owner = False
                    wait_for_start_publication = True
                elif self._state == McpWorkerState.FAILED and self._proc is None:
                    self._state = McpWorkerState.STOPPED
                    self._state_error = None
                    self._state_changed.notify_all()
                    return
                else:
                    if (
                        _recovery_token is not None
                        and self._active_recovery != _recovery_token
                    ):
                        raise McpError("MCP recovery ownership was lost.")
                    self._operation += 1
                    operation = self._operation
                    proc = self._proc
                    generation = self._generation
                    self._state = McpWorkerState.STOPPING
                    self._state_error = None
                    self._state_changed.notify_all()
                    owner = True
            if owner:
                break
            if wait_for_start_publication:
                self._wait_for_start_publication(operation, deadline)
                continue
            self._wait_for_operation(operation, deadline, allow_shutdown=True)

        if proc is None:
            reaped = True
        else:
            reaped = self._terminate_and_reap(proc, deadline)
            if reaped:
                self._close_pipes(proc, deadline)

        with self._state_changed:
            if self._operation != operation:
                return
            if reaped:
                if self._proc is proc:
                    self._proc = None
                self._state = McpWorkerState.STOPPED
                self._state_error = None
            else:
                self._state = McpWorkerState.FAILED
                self._state_error = McpError(
                    "MCP process did not exit before the teardown deadline.",
                    generation=generation,
                )
            self._state_changed.notify_all()
            if not reaped:
                self._raise_state_error()

    def recover(self, failed_generation: int | None, *, deadline: float | None = None) -> int:
        """Replace one failed generation and share the result with concurrent callers."""
        if deadline is None:
            deadline = time.monotonic() + 4.0 + self._startup_timeout_seconds
        if failed_generation is None:
            failed_generation = self.generation

        while True:
            self._raise_if_stopping()
            wait_recovery = False
            wait_operation: int | None = None
            with self._state_changed:
                if self._active_recovery is not None:
                    recovery_token = self._active_recovery
                    self._recovery_waiters[recovery_token] = (
                        self._recovery_waiters.get(recovery_token, 0) + 1
                    )
                    owner = False
                    wait_recovery = True
                elif (
                    self._generation > failed_generation
                    and self._state == McpWorkerState.READY
                    and self._proc is not None
                    and self._proc.poll() is None
                ):
                    return self._generation
                elif self._generation > failed_generation:
                    if self._state in {
                        McpWorkerState.STARTING,
                        McpWorkerState.STOPPING,
                    }:
                        wait_operation = self._operation
                        owner = False
                    else:
                        self._raise_state_error()
                else:
                    self._next_recovery += 1
                    recovery_token = self._next_recovery
                    self._active_recovery = recovery_token
                    owner = True
            if owner:
                break
            if wait_recovery:
                return self._wait_for_recovery(recovery_token, deadline)
            assert wait_operation is not None
            self._wait_for_operation(wait_operation, deadline)

        error: Exception | None = None
        recovered_generation = self.generation
        try:
            self.stop(deadline=deadline, _recovery_token=recovery_token)
            self._raise_if_stopping()
            recovered_generation = self.start(
                deadline=deadline,
                _recovery_token=recovery_token,
            )
            self._raise_if_stopping()
            return recovered_generation
        except Exception as exc:
            error = exc
            raise
        finally:
            with self._state_changed:
                if self._recovery_waiters.get(recovery_token, 0) > 0:
                    self._recovery_outcomes[recovery_token] = (
                        error is None,
                        error,
                        recovered_generation,
                    )
                if self._active_recovery == recovery_token:
                    self._active_recovery = None
                self._state_changed.notify_all()

    def notify_shutdown(self, *, deadline: float | None = None) -> None:
        """Wake lifecycle waiters so they can observe router draining."""
        if deadline is None:
            with self._state_changed:
                self._state_changed.notify_all()
            return
        remaining = max(0.0, deadline - time.monotonic())
        if not self._state_lock.acquire(timeout=remaining):
            raise McpError(f"Timed out notifying {self._name} lifecycle waiters.")
        try:
            self._state_changed.notify_all()
        finally:
            self._state_lock.release()

    def is_ready(self) -> bool:
        """Return whether the worker completed startup and is still alive."""
        with self._state_changed:
            return bool(
                self._state == McpWorkerState.READY
                and self._proc is not None
                and self._proc.poll() is None
                and not self._stopping.is_set()
            )

    def list_tools(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Request the tool list from the MCP server."""
        return self._send_request(
            McpRequest(method="tools/list", params={}),
            timeout_seconds=timeout_seconds or self._timeout_seconds,
        )

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool by name with arguments."""
        result, generation = self._send_request_with_generation(
            McpRequest(method="tools/call", params={"name": name, "arguments": arguments}),
            timeout_seconds=self._timeout_seconds,
        )
        if isinstance(result, dict) and "error" in result:
            raise McpToolError(result["error"], generation=generation)
        return result

    def _send_request(self, request: McpRequest, timeout_seconds: float) -> Any:
        """Send a JSON-RPC request and block until the matching response."""
        result, _ = self._send_request_with_generation(request, timeout_seconds)
        return result

    def _send_request_with_generation(
        self,
        request: McpRequest,
        timeout_seconds: float,
        *,
        expected_generation: int | None = None,
        allow_starting: bool = False,
    ) -> tuple[Any, int]:
        """Send one request against a validated worker generation."""
        deadline = time.monotonic() + timeout_seconds
        proc, generation = self._acquire_ready_request(
            deadline,
            expected_generation=expected_generation,
            allow_starting=allow_starting,
        )
        try:
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise McpError("MCP process is not running.", generation=generation)
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": request.method,
                    "params": request.params,
                }
            )
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._raise_process_error("MCP process pipe broken.", generation, exc)

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpRequestTimeoutError(
                        f"MCP request timed out: {request.method}",
                        method=request.method,
                        timeout_seconds=timeout_seconds,
                        generation=generation,
                    )
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], remaining)
                except (OSError, ValueError) as exc:
                    self._raise_process_error("MCP process output closed.", generation, exc)
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    self._raise_process_error(
                        "MCP process closed unexpectedly.",
                        generation,
                    )
                stripped = line.strip()
                if not stripped:
                    continue
                if not stripped.startswith("{"):
                    self._logger.debug("MCP stdout: %s", stripped)
                    continue
                try:
                    response = json.loads(stripped)
                except json.JSONDecodeError:
                    self._logger.debug("MCP parse error: %s", stripped)
                    continue
                if response.get("id") != req_id:
                    self._logger.debug("MCP out-of-order response: %s", response)
                    continue
                if "error" in response:
                    raise McpError(str(response["error"]), generation=generation)
                if "result" not in response:
                    raise McpError(
                        f"Invalid MCP response: {response}",
                        generation=generation,
                    )
                return response["result"], generation
        finally:
            self._request_lock.release()

    def _acquire_ready_request(
        self,
        deadline: float,
        *,
        expected_generation: int | None,
        allow_starting: bool,
    ) -> tuple[subprocess.Popen[str], int]:
        """Wait for and atomically claim one worker generation for I/O."""
        waited_for_lifecycle = False
        while True:
            if allow_starting:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0 or not self._request_lock.acquire(timeout=remaining):
                    raise McpError("Timed out waiting for MCP startup request I/O.")
            else:
                with self._state_changed:
                    self._raise_if_stopping()
                    proc = self._proc
                    generation = self._generation
                    if (
                        self._active_recovery is None
                        and self._state == McpWorkerState.READY
                        and proc is not None
                    ):
                        if proc.poll() is not None:
                            self._state = McpWorkerState.FAILED
                            self._state_error = McpError(
                                "MCP process exited after becoming ready.",
                                generation=generation,
                            )
                            self._state_changed.notify_all()
                            self._raise_state_error()
                    elif self._active_recovery is not None or self._state in {
                        McpWorkerState.STARTING,
                        McpWorkerState.STOPPING,
                    }:
                        remaining = max(0.0, deadline - time.monotonic())
                        if remaining <= 0:
                            raise McpWorkerUnavailableError(
                                McpWorkerUnavailableError.user_message,
                                generation=generation,
                            )
                        waited_for_lifecycle = True
                        self._state_changed.wait(remaining)
                        continue
                    elif self._state == McpWorkerState.FAILED:
                        if waited_for_lifecycle:
                            raise McpWorkerUnavailableError(
                                McpWorkerUnavailableError.user_message,
                                generation=generation,
                            ) from self._state_error
                        self._raise_state_error()
                    else:
                        raise McpWorkerUnavailableError(
                            McpWorkerUnavailableError.user_message,
                            generation=generation,
                        )

                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0 or not self._request_lock.acquire(timeout=remaining):
                    raise McpWorkerUnavailableError(
                        McpWorkerUnavailableError.user_message,
                        generation=generation,
                    )

            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0 or not self._start_gate.acquire(timeout=remaining):
                self._request_lock.release()
                error_type = McpError if allow_starting else McpWorkerUnavailableError
                message = (
                    "Timed out waiting for MCP startup admission."
                    if allow_starting
                    else McpWorkerUnavailableError.user_message
                )
                raise error_type(message, generation=generation)

            retry = False
            try:
                self._raise_if_stopping()
                with self._state_changed:
                    proc = self._proc
                    generation = self._generation
                    if allow_starting:
                        valid = bool(
                            self._state == McpWorkerState.STARTING
                            and expected_generation == generation
                            and proc is not None
                        )
                        if not valid:
                            raise McpError(
                                "MCP worker generation changed before startup request admission.",
                                generation=generation,
                            )
                    else:
                        valid = bool(
                            self._active_recovery is None
                            and self._state == McpWorkerState.READY
                            and proc is not None
                            and proc.poll() is None
                        )
                        if not valid:
                            retry = True
                    if valid:
                        assert proc is not None
                        return proc, generation
            except BaseException:
                self._request_lock.release()
                raise
            finally:
                self._start_gate.release()
            if retry:
                self._request_lock.release()
                waited_for_lifecycle = True

    def _raise_process_error(
        self,
        message: str,
        generation: int,
        exc: Exception | None = None,
    ) -> NoReturn:
        if self._stopping.is_set():
            raise McpShuttingDownError(
                "MCP operation interrupted by shutdown.",
                generation=generation,
            ) from exc
        raise McpError(message, generation=generation) from exc

    def _raise_if_stopping(self) -> None:
        if self._stopping.is_set():
            raise McpShuttingDownError(
                "MCP router is shutting down.",
                generation=self._generation,
            )

    def _raise_state_error(self) -> NoReturn:
        error = self._state_error
        if isinstance(error, McpShuttingDownError):
            raise McpShuttingDownError(str(error), generation=error.generation) from error
        if isinstance(error, McpError):
            raise McpError(str(error), generation=error.generation) from error
        if error is not None:
            raise McpError(str(error), generation=self._generation) from error
        raise McpError(
            f"MCP worker is in {self._state.value} state.",
            generation=self._generation,
        )

    def _wait_for_operation(
        self,
        operation: int,
        deadline: float | None,
        *,
        allow_shutdown: bool = False,
    ) -> None:
        with self._state_changed:
            while (
                self._operation == operation
                and self._state in {McpWorkerState.STARTING, McpWorkerState.STOPPING}
            ):
                if not allow_shutdown:
                    self._raise_if_stopping()
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise McpError("Timed out waiting for MCP lifecycle operation.")
                self._state_changed.wait(remaining)
            if not allow_shutdown:
                self._raise_if_stopping()
            if self._state == McpWorkerState.FAILED and not allow_shutdown:
                self._raise_state_error()

    def _wait_for_start_publication(self, operation: int, deadline: float) -> None:
        """Wait only until an in-flight spawn publishes its child or settles."""
        with self._state_changed:
            while (
                self._operation == operation
                and self._state == McpWorkerState.STARTING
                and self._proc is None
            ):
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise McpError("Timed out waiting for MCP child publication.")
                self._state_changed.wait(remaining)

    def _wait_for_recovery(self, recovery_token: int, deadline: float | None) -> int:
        with self._state_changed:
            try:
                while recovery_token not in self._recovery_outcomes:
                    self._raise_if_stopping()
                    remaining = self._remaining(deadline)
                    if remaining is not None and remaining <= 0:
                        raise McpError("Timed out waiting for MCP worker recovery.")
                    self._state_changed.wait(remaining)
                self._raise_if_stopping()
                success, error, generation = self._recovery_outcomes[recovery_token]
            finally:
                waiters = self._recovery_waiters.get(recovery_token, 0) - 1
                if waiters <= 0:
                    self._recovery_waiters.pop(recovery_token, None)
                    self._recovery_outcomes.pop(recovery_token, None)
                else:
                    self._recovery_waiters[recovery_token] = waiters
        if success:
            return generation
        if isinstance(error, McpShuttingDownError):
            raise McpShuttingDownError(str(error), generation=error.generation) from error
        if isinstance(error, McpError):
            raise McpError(str(error), generation=error.generation) from error
        raise McpError("MCP worker recovery failed.", generation=generation) from error

    def _complete_failed_start(
        self,
        operation: int,
        generation: int,
        proc: subprocess.Popen[str],
        error: Exception,
        deadline: float | None,
    ) -> None:
        with self._state_changed:
            if self._operation != operation:
                return
            self._operation += 1
            cleanup_operation = self._operation
            self._state = McpWorkerState.STOPPING
            self._state_error = None
            self._state_changed.notify_all()
        reaped = self._terminate_and_reap(proc, deadline)
        if reaped:
            self._close_pipes(proc, deadline)
        with self._state_changed:
            if self._operation != cleanup_operation:
                return
            if reaped and self._proc is proc:
                self._proc = None
            self._state = McpWorkerState.FAILED
            self._state_error = error
            self._state_changed.notify_all()

    def _terminate_and_reap(
        self,
        proc: subprocess.Popen[str],
        deadline: float | None,
    ) -> bool:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=self._bounded_timeout(2.0, deadline, "MCP stop deadline expired."))
            return True
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(
                    timeout=self._bounded_timeout(1.0, deadline, "MCP kill deadline expired.")
                )
                return True
            except (OSError, subprocess.TimeoutExpired, McpError):
                self._logger.warning(
                    "mcp_stop_force_kill_incomplete name=%s generation=%s",
                    self._name,
                    self._generation,
                )
                return False
        except McpError:
            return False

    def _close_pipes(self, proc: subprocess.Popen[str], deadline: float | None) -> None:
        remaining = self._remaining(deadline)
        if remaining is not None and remaining <= 0:
            self._logger.warning("mcp_stop_pipe_close_deferred name=%s", self._name)
            return
        timeout = min(1.0, remaining) if remaining is not None else 1.0
        if not self._request_lock.acquire(timeout=timeout):
            self._logger.warning("mcp_stop_pipe_close_deferred name=%s", self._name)
            return
        try:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        finally:
            self._request_lock.release()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def _bounded_timeout(
        self,
        maximum: float,
        deadline: float | None,
        message: str,
    ) -> float:
        remaining = self._remaining(deadline)
        if remaining is None:
            return maximum
        if remaining <= 0:
            raise McpError(message, generation=self._generation)
        return min(maximum, remaining)

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Continuously read stderr to avoid blocking when piped."""
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                stripped = line.rstrip()
                if stripped:
                    self._logger.debug("MCP stderr: %s", stripped)
        except (OSError, ValueError):
            if not self._stopping.is_set():
                self._logger.debug("MCP stderr pipe closed name=%s", self._name)


class McpRouter:
    """Route tool calls to CPU/GPU MCP processes with retry logic."""
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._start_gate = threading.Lock()
        self._stopping = threading.Event()
        pipe_stderr = settings.app_env.lower() in {"dev", "development", "local", "test"}
        python_exe = sys.executable
        self._cpu = McpProcess(
            name="mcp_cpu",
            args=[
                python_exe,
                "-m",
                "src.mcp_server",
                "--device",
                settings.mcp_cpu_device,
                "--mode",
                "cpu",
                "--service-name",
                "mcp_cpu",
            ]
            + (["--debug"] if settings.mcp_debug else []),
            cwd=settings.project_root,
            timeout_seconds=settings.mcp_timeout_seconds,
            startup_timeout_seconds=settings.mcp_startup_timeout_seconds,
            pipe_stderr=pipe_stderr,
            start_gate=self._start_gate,
            stopping=self._stopping,
        )
        self._gpu = McpProcess(
            name="mcp_gpu",
            args=[
                python_exe,
                "-m",
                "src.mcp_server",
                "--device",
                settings.mcp_gpu_device,
                "--mode",
                "gpu",
                "--service-name",
                "mcp_gpu",
            ]
            + (["--debug"] if settings.mcp_debug else []),
            cwd=settings.project_root,
            timeout_seconds=settings.mcp_gpu_timeout_seconds,
            startup_timeout_seconds=settings.mcp_startup_timeout_seconds,
            pipe_stderr=pipe_stderr,
            start_gate=self._start_gate,
            stopping=self._stopping,
        )
        self._tool_to_worker = {
            "parse_score": "cpu",
            "reparse": "cpu",
            "add_solfege_lyric_verse": "cpu",
            "modify_solfege_settings": "cpu",
            "preprocess_voice_parts": "cpu",
            "list_voicebanks": "cpu",
            "get_voicebank_info": "cpu",
            "synthesize": "gpu",
            "save_audio": "gpu",
        }
        self._startup_lock = threading.Lock()
        self._startup_changed = threading.Condition(self._startup_lock)
        self._startup_ready = threading.Event()
        self._startup_thread: Optional[threading.Thread] = None
        self._startup_future: Future[None] | None = None
        self._startup_error: Optional[str] = None
        self._drain_established = threading.Event()

    def start(self) -> None:
        """Start both CPU and GPU MCP processes."""
        self.start_background().result()

    def start_background(self) -> Future[None]:
        """Start both MCP processes once and return their completion future."""
        self._raise_if_stopping()
        with self._startup_lock:
            if self._startup_future is not None:
                return self._startup_future
            self._startup_ready.clear()
            self._startup_error = None
            future: Future[None] = Future()
            thread = threading.Thread(
                target=self._start_background_target,
                args=(future,),
                name="mcp-router-startup",
                daemon=True,
            )
            self._startup_future = future
            self._startup_thread = thread
        future.add_done_callback(lambda _: self._notify_startup_changed())
        try:
            thread.start()
        except BaseException as exc:
            self._startup_error = str(exc)
            self._startup_ready.set()
            future.set_exception(exc)
            raise
        return future

    def stop(self, *, deadline: float | None = None) -> None:
        """Stop both CPU and GPU MCP processes."""
        if deadline is None:
            deadline = time.monotonic() + self._settings.backend_shutdown_worker_seconds
        self.begin_shutdown(deadline=deadline)
        errors: list[tuple[str, Exception]] = []

        def stop_worker(worker: str, process: Any) -> None:
            try:
                if isinstance(process, McpProcess):
                    process.stop(deadline=deadline)
                else:
                    process.stop()
            except Exception as exc:
                errors.append((worker, exc))
                logging.getLogger(__name__).exception(
                    "mcp_worker_stop_failed worker=%s",
                    worker,
                )

        stop_threads = [
            threading.Thread(
                target=stop_worker,
                args=(worker, process),
                name=f"mcp-stop-{worker}",
                daemon=True,
            )
            for worker, process in (("cpu", self._cpu), ("gpu", self._gpu))
        ]
        for thread in stop_threads:
            thread.start()
        incomplete = False
        for thread in stop_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                incomplete = True
                logging.getLogger(__name__).error(
                    "mcp_worker_stop_deadline_exceeded thread=%s",
                    thread.name,
                )
        startup_thread = self._startup_thread
        if (
            startup_thread is not None
            and startup_thread is not threading.current_thread()
            and startup_thread.is_alive()
        ):
            startup_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if startup_thread.is_alive():
                incomplete = True
                logging.getLogger(__name__).error(
                    "mcp_startup_thread_stop_deadline_exceeded"
                )
        if errors or incomplete:
            incomplete_parts = [worker for worker, _ in errors]
            if incomplete:
                incomplete_parts.append("lifecycle thread")
            raise McpError(
                "MCP shutdown did not complete for: "
                + ", ".join(incomplete_parts)
            )

    def begin_shutdown(self, *, deadline: float | None = None) -> None:
        """Atomically prevent new calls and worker starts before teardown."""
        if deadline is None:
            deadline = time.monotonic() + self._settings.backend_shutdown_worker_seconds
        self._stopping.set()
        if self._drain_established.is_set():
            return
        self._notify_startup_changed(deadline=deadline)
        for process in (self._cpu, self._gpu):
            notify = getattr(process, "notify_shutdown", None)
            if notify is not None:
                if isinstance(process, McpProcess):
                    notify(deadline=deadline)
                else:
                    notify()
        if self._drain_established.is_set():
            return
        remaining = max(0.0, deadline - time.monotonic())
        if not self._start_gate.acquire(timeout=remaining):
            raise McpError("Timed out establishing MCP draining boundary.")
        established = False
        try:
            if not self._drain_established.is_set():
                self._drain_established.set()
                established = True
        finally:
            self._start_gate.release()
        if established:
            logging.getLogger(__name__).info("mcp_router_draining")

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Route a tool call to the appropriate MCP process."""
        self._raise_if_stopping()
        self._ensure_started_for_call()
        worker = self._tool_to_worker.get(name, "cpu")
        return self._call_with_retry(worker, name, arguments)

    def readiness(self) -> Dict[str, Any]:
        """Return lightweight MCP startup state for diagnostics."""
        draining = self._stopping.is_set()
        cpu_ready = self._cpu.is_ready()
        gpu_ready = self._gpu.is_ready()
        ready = bool(
            self._startup_ready.is_set()
            and self._startup_error is None
            and cpu_ready
            and gpu_ready
            and not draining
        )
        starting = (
            not draining
            and not self._startup_ready.is_set()
            and self._startup_thread is not None
            and self._startup_thread.is_alive()
        )
        if draining:
            status = "draining"
        elif self._startup_error:
            status = "error"
        elif ready:
            status = "ready"
        elif starting:
            status = "starting"
        elif self._startup_ready.is_set():
            status = "not_ready"
        else:
            status = "not_started"
        payload: Dict[str, Any] = {
            "status": status,
            "ready": ready,
            "starting": starting,
            "draining": draining,
            "workers": {"cpu": cpu_ready, "gpu": gpu_ready},
        }
        if self._startup_error:
            payload["error"] = self._startup_error
        return payload

    def _start_background_target(self, future: Future[None]) -> None:
        """Run startup from a daemon thread and keep any failure observable."""
        error: BaseException | None = None
        try:
            self._cpu.start()
            self._raise_if_stopping()
            self._gpu.start()
            self._raise_if_stopping()
        except McpShuttingDownError as exc:
            error = exc
            logging.getLogger(__name__).info("mcp_background_start_cancelled")
        except Exception as exc:
            error = exc
            self._startup_error = str(exc)
            logging.getLogger(__name__).exception("mcp_background_start_failed")
            if not self._stopping.is_set():
                cleanup_deadline = (
                    time.monotonic() + self._settings.backend_shutdown_worker_seconds
                )
                for process in (self._cpu, self._gpu):
                    try:
                        if isinstance(process, McpProcess):
                            process.stop(deadline=cleanup_deadline)
                        else:
                            process.stop()
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "mcp_start_failure_cleanup_failed"
                        )
        finally:
            self._startup_ready.set()
            if not future.done():
                if error is None:
                    future.set_result(None)
                else:
                    future.set_exception(error)

    def _ensure_started_for_call(self) -> None:
        """Wait for background startup before routing a real tool call."""
        self._raise_if_stopping()
        future = self.start_background()
        deadline = time.monotonic() + self._settings.backend_ready_timeout_seconds
        with self._startup_changed:
            while not future.done():
                self._raise_if_stopping()
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise McpStartupInProgressError(
                        McpStartupInProgressError.user_message
                    )
                self._startup_changed.wait(remaining)
            self._raise_if_stopping()
        try:
            future.result()
        except McpShuttingDownError:
            raise
        except Exception as exc:
            raise McpError(f"MCP startup failed: {exc}") from exc
        self._raise_if_stopping()
        if self._startup_error:
            raise McpError(f"MCP startup failed: {self._startup_error}")

    def _notify_startup_changed(self, *, deadline: float | None = None) -> None:
        """Wake request admission without changing actual startup completion."""
        if deadline is None:
            with self._startup_changed:
                self._startup_changed.notify_all()
            return
        remaining = max(0.0, deadline - time.monotonic())
        if not self._startup_lock.acquire(timeout=remaining):
            raise McpError("Timed out notifying MCP startup waiters.")
        try:
            self._startup_changed.notify_all()
        finally:
            self._startup_lock.release()

    def _call_with_retry(self, worker: str, name: str, arguments: Dict[str, Any]) -> Any:
        """Retry a tool call once after restarting a failed process."""
        process = self._gpu if worker == "gpu" else self._cpu
        try:
            start = time.monotonic()
            result = process.call_tool(name, arguments)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logging.getLogger(__name__).info(
                "mcp_tool_call tool=%s worker=%s elapsed_ms=%.2f",
                name,
                worker,
                elapsed_ms,
            )
            return result
        except McpToolError as exc:
            gpu_error = classify_gpu_tool_error(exc.payload)
            if (
                worker == "gpu"
                and name == "synthesize"
                and gpu_error is not None
            ):
                logging.getLogger(__name__).warning(
                    "mcp_gpu_worker_health_error tool=%s worker=%s code=%s "
                    "matched_pattern=%s retrying_after_restart=true",
                    name,
                    worker,
                    gpu_error.code,
                    gpu_error.matched_pattern,
                )
                self._restart_process(process, exc.generation)
                start = time.monotonic()
                try:
                    result = process.call_tool(name, arguments)
                except McpToolError as retry_exc:
                    retry_exc.payload["retryAttempted"] = True
                    retry_exc.payload["workerRestarted"] = True
                    raise
                elapsed_ms = (time.monotonic() - start) * 1000.0
                logging.getLogger(__name__).info(
                    "mcp_tool_call tool=%s worker=%s elapsed_ms=%.2f retry_after_restart=true",
                    name,
                    worker,
                    elapsed_ms,
                )
                return result
            logging.getLogger(__name__).warning(
                "mcp_tool_error tool=%s worker=%s code=%s error_type=%s retry_skipped=true",
                name,
                worker,
                exc.code,
                exc.error_type,
            )
            raise
        except McpRequestTimeoutError as exc:
            logging.getLogger(__name__).warning(
                "mcp_tool_timeout tool=%s worker=%s timeout_seconds=%.2f; restarting_without_retry",
                name,
                worker,
                exc.timeout_seconds,
            )
            self._restart_process(process, exc.generation)
            raise
        except McpShuttingDownError:
            raise
        except McpWorkerUnavailableError:
            raise
        except McpError as exc:
            logging.getLogger(__name__).warning(
                "MCP call failed tool=%s worker=%s error=%s; restarting",
                name,
                worker,
                exc,
            )
            self._restart_process(process, exc.generation)
            start = time.monotonic()
            result = process.call_tool(name, arguments)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logging.getLogger(__name__).info(
                "mcp_tool_call tool=%s worker=%s elapsed_ms=%.2f",
                name,
                worker,
                elapsed_ms,
            )
            return result

    def _restart_process(
        self,
        process: McpProcess,
        failed_generation: int | None = None,
    ) -> None:
        """Restart one worker unless application shutdown has started."""
        self._raise_if_stopping()
        if isinstance(process, McpProcess):
            process.recover(failed_generation)
        else:
            process.stop()
            self._raise_if_stopping()
            process.start()
        self._raise_if_stopping()

    def _raise_if_stopping(self) -> None:
        if self._stopping.is_set():
            raise McpShuttingDownError("MCP router is shutting down.")
