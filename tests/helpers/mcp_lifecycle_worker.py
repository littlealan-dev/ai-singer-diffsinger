from __future__ import annotations

"""Controlled stdio worker used by MCP lifecycle subprocess tests."""

from pathlib import Path
import os
import signal
import sys
import time


def main() -> None:
    marker = Path(sys.argv[1])
    terminated_marker = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if terminated_marker is not None:
        def record_termination(_signum: int, _frame: object) -> None:
            terminated_marker.write_text(str(time.monotonic()), encoding="utf-8")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, record_termination)
    marker.write_text(str(os.getpid()), encoding="utf-8")
    sys.stdin.readline()
    time.sleep(30)


if __name__ == "__main__":
    main()
