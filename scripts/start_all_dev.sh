#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_ROOT="${ROOT_DIR}/../sightsinger-webmcp"

"${ROOT_DIR}/scripts/start_backend_dev.sh"
"${FRONTEND_ROOT}/scripts/start-frontend.sh"
