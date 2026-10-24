"""Local fault-injecting proxy for the Gemini REST API (manual retry testing).

Point the dev backend at it with GEMINI_BASE_URL=http://127.0.0.1:9100/v1beta
(for example via BACKEND_EXTRA_ENV_FILE with scripts/start_backend_dev.sh).
It forwards every request to the real API, except that the next COUNT
`:generateContent` requests get the chosen fault instead.

Change the fault while it runs, without restarting the backend:
    curl 'http://127.0.0.1:9100/_fault?mode=drop&count=2'
    curl 'http://127.0.0.1:9100/_fault'            # show the current state

Modes:
    drop          close the connection with no response  -> RemoteDisconnected, retried
    truncate      promise a longer body than is sent      -> IncompleteRead, retried
    503           HTTP 503 "model overloaded"             -> retried
    429-retry     HTTP 429 with RetryInfo retryDelay 3s   -> retried after 3 s
    429-cap       HTTP 429 spending cap, no RetryInfo     -> NOT retried (fails at once)
    504           HTTP 504                                -> NOT retried
    hang          wait HANG seconds, then drop            -> read timeout, NOT retried
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_state = {"mode": "drop", "count": 0, "hang": 60.0, "upstream": ""}

_FORWARD_HEADERS = ("content-type", "x-goog-api-key", "authorization")


def _take_fault() -> str | None:
    with _lock:
        if _state["count"] <= 0:
            return None
        _state["count"] -= 1
        return str(_state["mode"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{time.strftime('%H:%M:%S')} {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _control(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        with _lock:
            if "mode" in query:
                _state["mode"] = query["mode"][0]
            if "count" in query:
                _state["count"] = int(query["count"][0])
            if "hang" in query:
                _state["hang"] = float(query["hang"][0])
            snapshot = {k: v for k, v in _state.items() if k != "upstream"}
        print(f"{time.strftime('%H:%M:%S')} fault state {snapshot}", flush=True)
        self._send(200, json.dumps(snapshot).encode() + b"\n")

    def _fault(self, mode: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} INJECT {mode} on {self.command} {self.path.split('?')[0]}", flush=True)
        if mode == "drop":
            self.close_connection = True
            return  # no status line at all
        if mode == "hang":
            time.sleep(float(_state["hang"]))
            self.close_connection = True
            return
        if mode == "truncate":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"candidates": [')
            self.close_connection = True
            return
        if mode == "503":
            body = {"error": {"code": 503, "message": "The model is overloaded. Please try again later.", "status": "UNAVAILABLE"}}
            self._send(503, json.dumps(body).encode())
            return
        if mode == "504":
            body = {"error": {"code": 504, "message": "Deadline exceeded.", "status": "DEADLINE_EXCEEDED"}}
            self._send(504, json.dumps(body).encode())
            return
        if mode == "429-cap":
            body = {"error": {"code": 429, "message": "Your project has exceeded its monthly spending cap.", "status": "RESOURCE_EXHAUSTED"}}
            self._send(429, json.dumps(body).encode())
            return
        if mode == "429-retry":
            body = {"error": {"code": 429, "message": "Resource exhausted.", "status": "RESOURCE_EXHAUSTED", "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"}]}}
            self._send(429, json.dumps(body).encode())
            return
        self._send(500, json.dumps({"error": f"unknown fault mode {mode}"}).encode())

    def _forward(self, body: bytes | None) -> None:
        url = _state["upstream"] + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() in _FORWARD_HEADERS}
        request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                self._send(response.status, response.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read())

    def _handle(self) -> None:
        if self.path.startswith("/_fault"):
            self._control()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if ":generateContent" in self.path:
            mode = _take_fault()
            if mode is not None:
                self._fault(mode)
                return
        self._forward(body)

    do_GET = do_POST = do_DELETE = do_PATCH = _handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--upstream", default="https://generativelanguage.googleapis.com")
    parser.add_argument("--mode", default="drop")
    parser.add_argument("--count", type=int, default=0, help="faults to inject before passing through")
    parser.add_argument("--hang", type=float, default=60.0, help="seconds to wait in hang mode")
    args = parser.parse_args()
    _state.update(mode=args.mode, count=args.count, hang=args.hang, upstream=args.upstream.rstrip("/"))
    print(f"llm_fault_proxy on 127.0.0.1:{args.port} -> {_state['upstream']} (mode={args.mode} count={args.count})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
