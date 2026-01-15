from __future__ import annotations

"""MCP stdio server entrypoint that routes JSON-RPC tool calls."""

import argparse
import json
import sys
import traceback
import logging
import os
import time
from typing import Any, Dict, Optional, Set

from src.mcp.tools import call_tool, list_tools
from src.mcp.logging_utils import configure_logging, summarize_payload


def _error_response(request_id: Optional[Any], code: int, message: str) -> Dict[str, Any]:
    """Build a JSON-RPC error response envelope."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result_response(request_id: Optional[Any], result: Any) -> Dict[str, Any]:
    """Build a JSON-RPC success response envelope."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


_MODE_TOOL_ALLOWLIST: Dict[str, Set[str]] = {
    "cpu": {"parse_score", "modify_score", "list_voicebanks", "get_voicebank_info"},
    "gpu": {"synthesize", "save_audio"},
    "all": set(),
}


def _get_allowlist(mode: str) -> Optional[Set[str]]:
    """Return the tool allowlist for a given mode, or None for unrestricted."""
    if mode == "all":
        return None
    return _MODE_TOOL_ALLOWLIST.get(mode, set())


def _handle_request(request: Dict[str, Any], device: str, mode: str) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC request payload and return a response (or None)."""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {}) or {}
    logger = logging.getLogger(__name__)
    logger.debug("MCP request method=%s params=%s", method, summarize_payload(params))

    if method == "initialize":
        # Handshake request: advertise server info and tool capability surface.
        result = {
            "protocolVersion": params.get("protocolVersion", "1.0"),
            "serverInfo": {"name": "ai-singer-diffsinger-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        }
        return _result_response(request_id, result)

    if method == "tools/list":
        # Return tools optionally filtered by mode.
        result = {"tools": list_tools(_get_allowlist(mode))}
        logger.debug("MCP response id=%s result=%s", request_id, summarize_payload(result))
        return _result_response(request_id, result)

    if method == "tools/call":
        # Validate tool name and arguments before execution.
        name = params.get("name")
        if not name:
            return _result_response(
                request_id,
                {"error": {"message": "name is required", "type": "ValueError"}},
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result_response(
                request_id,
                {"error": {"message": "arguments must be an object", "type": "ValueError"}},
            )
        allowlist = _get_allowlist(mode)
        if allowlist is not None and name not in allowlist:
            return _result_response(
                request_id,
                {"error": {"message": f"Tool not available in mode: {name}", "type": "ValueError"}},
            )
        try:
            result = call_tool(name, arguments, device)
            logger.debug("MCP response id=%s result=%s", request_id, summarize_payload(result))
            return _result_response(request_id, result)
        except Exception as exc:
            return _result_response(
                request_id,
                {"error": {"message": str(exc), "type": exc.__class__.__name__}},
            )

    if request_id is None:
        return None

    return _error_response(request_id, -32601, f"Unknown method: {method}")


def run_server(device: str, mode: str) -> None:
    """Run a blocking stdio loop that reads JSON-RPC lines and writes responses."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            # Parse each line as a single JSON-RPC request.
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error_response(None, -32700, f"Invalid JSON: {exc}")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        try:
            # Dispatch into the request handler, guarding against unexpected errors.
            response = _handle_request(request, device, mode)
        except Exception:
            response = _error_response(request.get("id"), -32000, "Internal error")
            traceback.print_exc(file=sys.stderr)

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
    """Parse CLI flags, configure logging, and start the MCP server loop."""
    parser = argparse.ArgumentParser(description="SVS MCP server (stdio).")
    parser.add_argument("--device", default="cpu", help="Inference device (internal only).")
    parser.add_argument(
        "--mode",
        default="all",
        choices=["cpu", "gpu", "all"],
        help="Tool exposure mode.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--log-dir", default="logs", help="Directory for log files.")
    parser.add_argument(
        "--service-name",
        default="mcp_server",
        help="Service name used for the log filename.",
    )
    args = parser.parse_args()
    startup_start = time.monotonic()
    if args.debug:
        # Allow CLI debug to override the configured log level.
        os.environ.setdefault("BACKEND_LOG_LEVEL", "debug")
    mcp_config = "config/logging.mcp.prod.json"
    if os.getenv("APP_ENV", "dev").lower() in {"dev", "development", "local", "test"}:
        mcp_config = "config/logging.mcp.dev.json"
    os.environ.setdefault("LOG_CONFIG", mcp_config)
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("mcp_startup_begin service=%s device=%s mode=%s", args.service_name, args.device, args.mode)
    nltk_start = time.monotonic()
    try:
        # Warm NLTK resources so first request is not impacted by download latency.
        import nltk

        nltk.data.find("taggers/averaged_perceptron_tagger_eng")
        nltk_status = "cached"
    except Exception:
        try:
            import nltk

            nltk.download("averaged_perceptron_tagger_eng")
            nltk_status = "downloaded"
        except Exception as exc:  # pragma: no cover - defensive log
            logger.warning("nltk_download_failed error=%s", exc, exc_info=True)
            nltk_status = "failed"
    nltk_ms = (time.monotonic() - nltk_start) * 1000.0
    logger.info("nltk_ready status=%s elapsed_ms=%.2f", nltk_status, nltk_ms)
    run_server(args.device, args.mode)
    startup_ms = (time.monotonic() - startup_start) * 1000.0
    logger.info("mcp_startup_ready elapsed_ms=%.2f", startup_ms)


if __name__ == "__main__":
    main()
