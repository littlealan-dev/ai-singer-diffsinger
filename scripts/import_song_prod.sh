#!/usr/bin/env bash
set -euo pipefail

# Render on the local stack and export with export_local_song.sh. Inspect the
# bundle and run this command with DRY_RUN=1 (the default). Run with DRY_RUN=0
# and --share to publish, then open the link it prints.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DRY_RUN="${DRY_RUN:-1}"
set -a
source "$ROOT/env/prod.env"
set +a

# Select the destination from prod.env, independently of any local stack.
unset FIRESTORE_EMULATOR_HOST FIREBASE_AUTH_EMULATOR_HOST STORAGE_EMULATOR_HOST
unset FIREBASE_STORAGE_EMULATOR_HOST FIREBASE_SERVICE_ACCOUNT_FILE FIREBASE_CONFIG
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID:?PROJECT_ID is required in env/prod.env}"
export STORAGE_BUCKET="${STORAGE_BUCKET:?STORAGE_BUCKET is required in env/prod.env}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python="${SIGHTSINGER_PYTHON:-$ROOT/.venv310/bin/python}"
if [[ ! -x "$python" && -z "${SIGHTSINGER_PYTHON:-}" ]]; then python=python3; fi
exec "$python" -m scripts.import_song_prod "$@"
