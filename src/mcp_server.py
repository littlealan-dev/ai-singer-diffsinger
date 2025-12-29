from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Dict, Optional

from src.mcp.tools import call_tool, list_tools


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


def _handle_request(request: Dict[str, Any], device: str) -> Optional[Dict[str, Any]]:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {}) or {}

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "1.0"),
            "serverInfo": {"name": "ai-singer-diffsinger-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        }
        return _result_response(request_id, result)

    if method == "tools/list":
        return _result_response(request_id, {"tools": list_tools()})

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
        try:
            result = call_tool(name, arguments, device)
            return _result_response(request_id, result)
        except Exception as exc:
            return _result_response(
                request_id,
                {"error": {"message": str(exc), "type": exc.__class__.__name__}},
            )

    if request_id is None:
        return None

    return _error_response(request_id, -32601, f"Unknown method: {method}")


def run_server(device: str) -> None:
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
            response = _handle_request(request, device)
        except Exception:
            response = _error_response(request.get("id"), -32000, "Internal error")
            traceback.print_exc(file=sys.stderr)

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="SVS MCP server (stdio).")
    parser.add_argument("--device", default="cpu", help="Inference device (internal only).")
    args = parser.parse_args()
    run_server(args.device)


if __name__ == "__main__":
    main()
