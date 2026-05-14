#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
PID_FILE="${LOG_DIR}/billing_backend_dev.pid"
LOG_FILE="${LOG_DIR}/billing_backend_dev.log"
ENV_FILE="${ROOT_DIR}/env/dev.env"
LOCAL_ENV_FILE="${ROOT_DIR}/env/local.env"
BACKEND_AUTH_DISABLED_OVERRIDE="${BACKEND_AUTH_DISABLED_OVERRIDE-${BACKEND_AUTH_DISABLED-}}"

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

: "${FIRESTORE_EMULATOR_HOST:=127.0.0.1:8080}"
: "${FIREBASE_AUTH_EMULATOR_HOST:=127.0.0.1:9099}"
: "${GOOGLE_CLOUD_PROJECT:=sightsinger-app}"
: "${APP_ENV:=dev}"
: "${BACKEND_AUTH_DISABLED:=false}"
: "${BILLING_BACKEND_HOST:=127.0.0.1}"
: "${BILLING_BACKEND_PORT:=8001}"
: "${BACKEND_LOG_LEVEL:=debug}"
: "${BILLING_BACKEND_FOREGROUND:=false}"

if [[ -f "${ROOT_DIR}/.venv310/bin/python" ]] && "${ROOT_DIR}/.venv310/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  DEFAULT_PYTHON="${ROOT_DIR}/.venv310/bin/python"
elif [[ -f "${ROOT_DIR}/../ai-singer-diffsinger/.venv310/bin/python" ]]; then
  DEFAULT_PYTHON="${ROOT_DIR}/../ai-singer-diffsinger/.venv310/bin/python"
else
  DEFAULT_PYTHON="${ROOT_DIR}/.venv310/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-${DEFAULT_PYTHON}}"

mkdir -p "${LOG_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}")"
  if kill -0 "${existing_pid}" >/dev/null 2>&1; then
    echo "Billing backend already running (pid ${existing_pid})."
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

export FIRESTORE_EMULATOR_HOST
export FIREBASE_AUTH_EMULATOR_HOST
export GOOGLE_CLOUD_PROJECT
export APP_ENV
export BACKEND_AUTH_DISABLED
export PYTHONPATH="${ROOT_DIR}"

cd "${ROOT_DIR}"
if [[ "${BILLING_BACKEND_FOREGROUND}" == "true" || "${BILLING_BACKEND_FOREGROUND}" == "1" ]]; then
  exec "${PYTHON_BIN}" -m uvicorn src.backend.billing_api:app \
    --host "${BILLING_BACKEND_HOST}" \
    --port "${BILLING_BACKEND_PORT}" \
    --log-level "${BACKEND_LOG_LEVEL}" \
    --access-log
fi

nohup "${PYTHON_BIN}" -m uvicorn src.backend.billing_api:app \
  --host "${BILLING_BACKEND_HOST}" \
  --port "${BILLING_BACKEND_PORT}" \
  --log-level "${BACKEND_LOG_LEVEL}" \
  --access-log \
  > "${LOG_FILE}" 2>&1 &

echo $! > "${PID_FILE}"
echo "Billing backend started (pid $(cat "${PID_FILE}")). Logs: ${LOG_FILE}"
