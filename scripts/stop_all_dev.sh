#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_ROOT="${ROOT_DIR}/../sightsinger-webmcp"

"${FRONTEND_ROOT}/scripts/stop-frontend.sh"
"${ROOT_DIR}/scripts/stop_backend_dev.sh"
