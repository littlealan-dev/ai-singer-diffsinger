#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
PID_FILE="${LOG_DIR}/backend_dev.pid"
LOG_FILE="${LOG_DIR}/backend_dev.log"

: "${FIRESTORE_EMULATOR_HOST:=127.0.0.1:8080}"
: "${FIREBASE_AUTH_EMULATOR_HOST:=127.0.0.1:9099}"
: "${FIREBASE_STORAGE_EMULATOR_HOST:=127.0.0.1:9199}"
: "${STORAGE_EMULATOR_HOST:=http://${FIREBASE_STORAGE_EMULATOR_HOST}}"
: "${GOOGLE_CLOUD_PROJECT:=sightsinger-app}"
: "${STORAGE_BUCKET:=${GOOGLE_CLOUD_PROJECT}.appspot.com}"
: "${MCP_DEBUG:=true}"
: "${BACKEND_AUTH_DISABLED:=true}"
: "${BACKEND_USE_STORAGE:=true}"
: "${BACKEND_HOST:=0.0.0.0}"
: "${BACKEND_PORT:=8000}"
: "${BACKEND_LOG_LEVEL:=debug}"

PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

mkdir -p "${LOG_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}")"
  if kill -0 "${existing_pid}" >/dev/null 2>&1; then
    echo "Backend already running (pid ${existing_pid})."
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

export FIRESTORE_EMULATOR_HOST
export FIREBASE_AUTH_EMULATOR_HOST
export FIREBASE_STORAGE_EMULATOR_HOST
export GOOGLE_CLOUD_PROJECT
export STORAGE_BUCKET
export MCP_DEBUG
export BACKEND_AUTH_DISABLED
export BACKEND_USE_STORAGE
export STORAGE_EMULATOR_HOST
export PYTHONPATH="${ROOT_DIR}"

cd "${ROOT_DIR}"
nohup "${PYTHON_BIN}" -m uvicorn src.backend.main:app \
  --host "${BACKEND_HOST}" \
  --port "${BACKEND_PORT}" \
  --log-level "${BACKEND_LOG_LEVEL}" \
  --access-log \
  > "${LOG_FILE}" 2>&1 &

echo $! > "${PID_FILE}"
echo "Backend started (pid $(cat "${PID_FILE}")). Logs: ${LOG_FILE}"
