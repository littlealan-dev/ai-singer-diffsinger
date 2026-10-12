#!/usr/bin/env bash
set -euo pipefail

# Start the local line-preparation worker (port 8002; the dev billing backend
# uses 8001) beside the main dev backend. The main backend hands it jobs over
# HTTP, as Cloud Tasks would.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export BACKEND_DEV_NAME=preprocess_worker_dev
export BACKEND_EXTRA_ENV_FILE="${ROOT_DIR}/env/preprocess-worker.dev.env"
exec "${ROOT_DIR}/scripts/start_backend_dev.sh"
