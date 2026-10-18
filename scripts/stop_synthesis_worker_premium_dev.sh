#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export BACKEND_DEV_NAME=synthesis_worker_premium_dev
export BACKEND_PORT=8004
exec "${ROOT_DIR}/scripts/stop_backend_dev.sh"
