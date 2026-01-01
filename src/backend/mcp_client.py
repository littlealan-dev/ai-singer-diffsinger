from __future__ import annotations

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
    pass


@dataclass(frozen=True)
class McpRequest:
    method: str
    params: Dict[str, Any]


class McpProcess:
    def __init__(
        self,
        args: Iterable[str],
        cwd: Path,
        timeout_seconds: float,
        startup_timeout_seconds: float,
    ) -> None:
        self._args = list(args)
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._logger = logging.getLogger(__name__)
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._args,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="mcp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self.list_tools(timeout_seconds=self._startup_timeout_seconds)

    def stop(self) -> None:
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
        return self._send_request(
            McpRequest(method="tools/list", params={}),
            timeout_seconds=timeout_seconds or self._timeout_seconds,
        )

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        result = self._send_request(
            McpRequest(method="tools/call", params={"name": name, "arguments": arguments}),
            timeout_seconds=self._timeout_seconds,
        )
        if isinstance(result, dict) and "error" in result:
            raise McpError(result["error"])
        return result

    def _send_request(self, request: McpRequest, timeout_seconds: float) -> Any:
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
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            stripped = line.rstrip()
            if stripped:
                self._logger.debug("MCP stderr: %s", stripped)


class McpRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        python_exe = sys.executable
        self._cpu = McpProcess(
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
        )
        self._gpu = McpProcess(
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
        self._cpu.start()
        self._gpu.start()

    def stop(self) -> None:
        self._cpu.stop()
        self._gpu.stop()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        worker = self._tool_to_worker.get(name, "cpu")
        return self._call_with_retry(worker, name, arguments)

    def _call_with_retry(self, worker: str, name: str, arguments: Dict[str, Any]) -> Any:
        process = self._gpu if worker == "gpu" else self._cpu
        try:
            return process.call_tool(name, arguments)
        except McpError as exc:
            logging.getLogger(__name__).warning(
                "MCP call failed tool=%s worker=%s error=%s; restarting",
                name,
                worker,
                exc,
            )
            process.stop()
            process.start()
            return process.call_tool(name, arguments)
