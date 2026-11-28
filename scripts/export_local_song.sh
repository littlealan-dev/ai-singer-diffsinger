#!/usr/bin/env bash
set -euo pipefail

# Render the Song on the local stack, then run this command to export it.
# Inspect the bundle, run import_song_prod.sh with DRY_RUN=1, then run it with
# DRY_RUN=0 and --share to publish. Open the share link it prints.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "$ROOT/env/dev.env"
if [[ -f "$ROOT/env/local.env" ]]; then source "$ROOT/env/local.env"; fi
if [[ -n "${BACKEND_EXTRA_ENV_FILE:-}" ]]; then source "$BACKEND_EXTRA_ENV_FILE"; fi
set +a

export FIRESTORE_EMULATOR_HOST="${FIRESTORE_EMULATOR_HOST:-127.0.0.1:8080}"
export STORAGE_EMULATOR_HOST="${STORAGE_EMULATOR_HOST:-http://${FIREBASE_STORAGE_EMULATOR_HOST:-127.0.0.1:9199}}"
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-sightsinger-app}"
export STORAGE_BUCKET="${STORAGE_BUCKET:-${GOOGLE_CLOUD_PROJECT}.appspot.com}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python="${SIGHTSINGER_PYTHON:-$ROOT/.venv310/bin/python}"
if [[ ! -x "$python" && -z "${SIGHTSINGER_PYTHON:-}" ]]; then python=python3; fi
exec "$python" -m scripts.export_local_song "$@"
