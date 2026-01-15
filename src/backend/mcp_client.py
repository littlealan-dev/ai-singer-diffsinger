from __future__ import annotations

"""Client wrapper to run MCP server processes and route tool calls."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import json
import logging
import select
import subprocess
import threading
import time
import sys

from src.backend.config import Settings


class McpError(RuntimeError):
    """Raised when an MCP request fails or times out."""
    pass


@dataclass(frozen=True)
class McpRequest:
    """JSON-RPC request payload for MCP tools."""
    method: str
    params: Dict[str, Any]


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
    ) -> None:
        self._name = name
        self._args = list(args)
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._pipe_stderr = pipe_stderr
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._logger = logging.getLogger(__name__)
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the MCP subprocess and wait for tool discovery."""
        if self._proc is not None:
            return
        start_time = time.monotonic()
        self._logger.info("mcp_start_begin name=%s", self._name)
        self._proc = subprocess.Popen(
            self._args,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if self._pipe_stderr else None,
            text=True,
        )
        if self._pipe_stderr:
            # Drain stderr to avoid blocking the process.
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                name="mcp-stderr",
                daemon=True,
            )
            self._stderr_thread.start()
        tools_start = time.monotonic()
        self.list_tools(timeout_seconds=self._startup_timeout_seconds)
        tools_ms = (time.monotonic() - tools_start) * 1000.0
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        self._logger.info(
            "mcp_start_ready name=%s elapsed_ms=%.2f tools_list_ms=%.2f",
            self._name,
            elapsed_ms,
            tools_ms,
        )

    def stop(self) -> None:
        """Stop the MCP subprocess and close pipes."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._proc.stdin:
            self._proc.stdin.close()
        if self._proc.stdout:
            self._proc.stdout.close()
        if self._proc.stderr:
            self._proc.stderr.close()
        self._proc = None

    def list_tools(self, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Request the tool list from the MCP server."""
        return self._send_request(
            McpRequest(method="tools/list", params={}),
            timeout_seconds=timeout_seconds or self._timeout_seconds,
        )

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool by name with arguments."""
        result = self._send_request(
            McpRequest(method="tools/call", params={"name": name, "arguments": arguments}),
            timeout_seconds=self._timeout_seconds,
        )
        if isinstance(result, dict) and "error" in result:
            raise McpError(result["error"])
        return result

    def _send_request(self, request: McpRequest, timeout_seconds: float) -> Any:
        """Send a JSON-RPC request and block until the matching response."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise McpError("MCP process is not running.")
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
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            # Serialize writes/reads per request to avoid interleaving responses.
            try:
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError as exc:
                raise McpError("MCP process pipe broken.") from exc

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(f"MCP request timed out: {request.method}")
                ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
                if not ready:
                    continue
                line = self._proc.stdout.readline()
                if not line:
                    raise McpError("MCP process closed unexpectedly.")
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
                    raise McpError(response["error"])
                if "result" not in response:
                    raise McpError(f"Invalid MCP response: {response}")
                return response["result"]

    def _drain_stderr(self) -> None:
        """Continuously read stderr to avoid blocking when piped."""
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            stripped = line.rstrip()
            if stripped:
                self._logger.debug("MCP stderr: %s", stripped)


class McpRouter:
    """Route tool calls to CPU/GPU MCP processes with retry logic."""
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
        )
        self._tool_to_worker = {
            "parse_score": "cpu",
            "modify_score": "cpu",
            "list_voicebanks": "cpu",
            "get_voicebank_info": "cpu",
            "synthesize": "gpu",
            "save_audio": "gpu",
        }

    def start(self) -> None:
        """Start both CPU and GPU MCP processes."""
        self._cpu.start()
        self._gpu.start()

    def stop(self) -> None:
        """Stop both CPU and GPU MCP processes."""
        self._cpu.stop()
        self._gpu.stop()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Route a tool call to the appropriate MCP process."""
        worker = self._tool_to_worker.get(name, "cpu")
        return self._call_with_retry(worker, name, arguments)

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
        except McpError as exc:
            logging.getLogger(__name__).warning(
                "MCP call failed tool=%s worker=%s error=%s; restarting",
                name,
                worker,
                exc,
            )
            process.stop()
            process.start()
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
