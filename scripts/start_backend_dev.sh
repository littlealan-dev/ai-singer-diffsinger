#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
PID_FILE="${LOG_DIR}/backend_dev.pid"
LOG_FILE="${LOG_DIR}/backend_dev.log"
ENV_FILE="${ROOT_DIR}/env/dev.env"
LOCAL_ENV_FILE="${ROOT_DIR}/env/local.env"
BACKEND_AUTH_DISABLED_OVERRIDE="${BACKEND_AUTH_DISABLED_OVERRIDE-${BACKEND_AUTH_DISABLED-}}"
BACKEND_DEV_USER_ID_OVERRIDE="${BACKEND_DEV_USER_ID-}"
BACKEND_DEV_USER_EMAIL_OVERRIDE="${BACKEND_DEV_USER_EMAIL-}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "${ENV_FILE}"
  set +a
fi
if [[ -f "${LOCAL_ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "${LOCAL_ENV_FILE}"
  set +a
fi

if [[ -n "${BACKEND_AUTH_DISABLED_OVERRIDE}" ]]; then
  BACKEND_AUTH_DISABLED="${BACKEND_AUTH_DISABLED_OVERRIDE}"
else
  BACKEND_AUTH_DISABLED="false"
fi
if [[ -n "${BACKEND_DEV_USER_ID_OVERRIDE}" ]]; then
  BACKEND_DEV_USER_ID="${BACKEND_DEV_USER_ID_OVERRIDE}"
fi
if [[ -n "${BACKEND_DEV_USER_EMAIL_OVERRIDE}" ]]; then
  BACKEND_DEV_USER_EMAIL="${BACKEND_DEV_USER_EMAIL_OVERRIDE}"
fi

: "${FIRESTORE_EMULATOR_HOST:=127.0.0.1:8080}"
: "${FIREBASE_AUTH_EMULATOR_HOST:=127.0.0.1:9099}"
: "${FIREBASE_STORAGE_EMULATOR_HOST:=127.0.0.1:9199}"
: "${STORAGE_EMULATOR_HOST:=http://${FIREBASE_STORAGE_EMULATOR_HOST}}"
: "${GOOGLE_CLOUD_PROJECT:=sightsinger-app}"
: "${STORAGE_BUCKET:=${GOOGLE_CLOUD_PROJECT}.appspot.com}"
: "${APP_ENV:=dev}"
: "${MCP_DEBUG:=true}"
: "${BACKEND_AUTH_DISABLED:=false}"
: "${BACKEND_USE_STORAGE:=true}"
: "${BACKEND_HOST:=0.0.0.0}"
: "${BACKEND_PORT:=8000}"
: "${BACKEND_LOG_LEVEL:=debug}"
: "${BACKEND_BUILD_ID:=dev-$(date +%s)}"
: "${BACKEND_STARTUP_TIMEOUT_SECONDS:=30}"

# Find python from .venv310 in current or parent/sibling project
if [[ -f "${ROOT_DIR}/.venv310/bin/python" ]] && "${ROOT_DIR}/.venv310/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  DEFAULT_PYTHON="${ROOT_DIR}/.venv310/bin/python"
elif [[ -f "${ROOT_DIR}/../ai-singer-diffsinger/.venv310/bin/python" ]]; then
  DEFAULT_PYTHON="${ROOT_DIR}/../ai-singer-diffsinger/.venv310/bin/python"
else
  DEFAULT_PYTHON="${ROOT_DIR}/.venv310/bin/python" # fallback default
fi
PYTHON_BIN="${PYTHON_BIN:-${DEFAULT_PYTHON}}"

mkdir -p "${LOG_DIR}"

port_is_listening() {
  lsof -nP -iTCP:"${BACKEND_PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for_ready() {
  local deadline=$((SECONDS + BACKEND_STARTUP_TIMEOUT_SECONDS))
  local response

  while (( SECONDS < deadline )); do
    response="$(curl --silent --show-error --max-time 1 "http://127.0.0.1:${BACKEND_PORT}/readyz" 2>/dev/null || true)"
    if [[ "${response}" =~ \"ready\"[[:space:]]*:[[:space:]]*true ]]; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}")"
  if kill -0 "${existing_pid}" >/dev/null 2>&1; then
    echo "Backend already running (pid ${existing_pid})."
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

if port_is_listening; then
  echo "Port ${BACKEND_PORT} is already in use; refusing to start another backend." >&2
  exit 1
fi

export FIRESTORE_EMULATOR_HOST
export FIREBASE_AUTH_EMULATOR_HOST
export FIREBASE_STORAGE_EMULATOR_HOST
export GOOGLE_CLOUD_PROJECT
export STORAGE_BUCKET
export APP_ENV
export MCP_DEBUG
export BACKEND_AUTH_DISABLED
export BACKEND_USE_STORAGE
export BACKEND_BUILD_ID
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
pid="$(cat "${PID_FILE}")"
echo "Waiting for backend and MCP workers to become ready (pid ${pid})."
if ! wait_for_ready; then
  echo "Backend did not become ready within ${BACKEND_STARTUP_TIMEOUT_SECONDS}s. Logs: ${LOG_FILE}" >&2
  exit 1
fi

echo "Backend started and MCP workers are ready (pid ${pid}). Logs: ${LOG_FILE}"
