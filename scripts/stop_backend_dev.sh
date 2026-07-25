#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${ROOT_DIR}/logs/backend_dev.pid"
BACKEND_PORT="${BACKEND_PORT:-8000}"
STOP_TIMEOUT_SECONDS="${BACKEND_STOP_TIMEOUT_SECONDS:-15}"

wait_for_exit() {
  local pid="$1"
  local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))

  while kill -0 "${pid}" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      return 1
    fi
    sleep 0.2
  done
}

port_is_listening() {
  lsof -nP -iTCP:"${BACKEND_PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

if [[ ! -f "${PID_FILE}" ]]; then
  echo "No PID file found. Backend may not be running."
  exit 0
fi

pid="$(cat "${PID_FILE}")"
if kill -0 "${pid}" >/dev/null 2>&1; then
  kill -TERM "${pid}"
  echo "Waiting for backend shutdown (pid ${pid})."
  if ! wait_for_exit "${pid}"; then
    echo "Backend did not exit within ${STOP_TIMEOUT_SECONDS}s; sending SIGKILL."
    kill -KILL "${pid}" 2>/dev/null || true
    if ! wait_for_exit "${pid}"; then
      echo "Backend process ${pid} is still running after SIGKILL." >&2
      exit 1
    fi
  fi
else
  echo "Process ${pid} is not running."
fi

rm -f "${PID_FILE}"

if port_is_listening; then
  echo "Port ${BACKEND_PORT} remains in use after backend shutdown; refusing restart." >&2
  exit 1
fi

echo "Backend stopped."
