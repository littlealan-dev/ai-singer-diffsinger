#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="${ROOT_DIR}/ui"
LOG_DIR="${ROOT_DIR}/logs"
PID_FILE="${LOG_DIR}/frontend_dev.pid"
LOG_FILE="${LOG_DIR}/frontend_dev.log"

: "${FRONTEND_HOST:=0.0.0.0}"
: "${FRONTEND_PORT:=5173}"
: "${FRONTEND_LOG_LEVEL:=info}"
: "${VITE_APP_ENV:=dev}"

if [[ ! -d "${UI_DIR}" ]]; then
  echo "UI directory not found: ${UI_DIR}"
  exit 1
fi

mkdir -p "${LOG_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}")"
  if kill -0 "${existing_pid}" >/dev/null 2>&1; then
    echo "Frontend already running (pid ${existing_pid})."
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

cd "${UI_DIR}"

VITE_APP_ENV="${VITE_APP_ENV}" nohup npm run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}" \
  --log-level "${FRONTEND_LOG_LEVEL}" \
  > "${LOG_FILE}" 2>&1 &

echo $! > "${PID_FILE}"
echo "Frontend started (pid $(cat "${PID_FILE}")). Logs: ${LOG_FILE}"
