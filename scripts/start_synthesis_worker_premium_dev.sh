#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export BACKEND_DEV_NAME=synthesis_worker_premium_dev
export BACKEND_EXTRA_ENV_FILE="${ROOT_DIR}/env/synthesis-worker-premium.dev.env"
exec "${ROOT_DIR}/scripts/start_backend_dev.sh"
