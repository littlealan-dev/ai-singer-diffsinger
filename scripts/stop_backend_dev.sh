#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${ROOT_DIR}/logs/backend_dev.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "No PID file found. Backend may not be running."
  exit 0
fi

pid="$(cat "${PID_FILE}")"
if kill -0 "${pid}" >/dev/null 2>&1; then
  kill "${pid}"
  echo "Sent stop signal to backend (pid ${pid})."
else
  echo "Process ${pid} is not running."
fi

rm -f "${PID_FILE}"
