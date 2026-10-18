#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${ROOT_DIR}/scripts/stop_frontend_dev.sh"
"${ROOT_DIR}/scripts/stop_backend_dev.sh"
"${ROOT_DIR}/scripts/stop_synthesis_worker_dev.sh"
"${ROOT_DIR}/scripts/stop_synthesis_worker_premium_dev.sh"
"${ROOT_DIR}/scripts/stop_preprocess_worker_dev.sh"
"${ROOT_DIR}/scripts/stop_billing_backend_dev.sh"
