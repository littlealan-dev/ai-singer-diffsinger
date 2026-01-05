from __future__ import annotations

import argparse
import json
import sys
import traceback
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from src.mcp.tools import call_tool, list_tools
from src.mcp.logging_utils import attach_context_filter, build_formatter, is_dev_env, summarize_payload


def _error_response(request_id: Optional[Any], code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result_response(request_id: Optional[Any], result: Any) -> Dict[str, Any]:
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
    if mode == "all":
        return None
    return _MODE_TOOL_ALLOWLIST.get(mode, set())


def _handle_request(request: Dict[str, Any], device: str, mode: str) -> Optional[Dict[str, Any]]:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {}) or {}
    logger = logging.getLogger(__name__)
    logger.debug("MCP request method=%s params=%s", method, summarize_payload(params))

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "1.0"),
            "serverInfo": {"name": "ai-singer-diffsinger-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        }
        return _result_response(request_id, result)

    if method == "tools/list":
        result = {"tools": list_tools(_get_allowlist(mode))}
        logger.debug("MCP response id=%s result=%s", request_id, summarize_payload(result))
        return _result_response(request_id, result)

    if method == "tools/call":
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
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error_response(None, -32700, f"Invalid JSON: {exc}")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        try:
            response = _handle_request(request, device, mode)
        except Exception:
            response = _error_response(request.get("id"), -32000, "Internal error")
            traceback.print_exc(file=sys.stderr)

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
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
    level = logging.DEBUG if args.debug else logging.INFO
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{args.service_name}.log"
    handlers = [logging.StreamHandler(sys.stderr)]
    if is_dev_env():
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    formatter = build_formatter()
    for handler in handlers:
        handler.setFormatter(formatter)
        attach_context_filter(handler)
    logging.basicConfig(
        level=level,
        handlers=handlers,
    )
    run_server(args.device, args.mode)


if __name__ == "__main__":
    main()
