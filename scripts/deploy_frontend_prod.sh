#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="${ROOT_DIR}/ui"

: "${FIREBASE_PROJECT:=}"

npm --prefix "${UI_DIR}" run build

if [[ -n "${FIREBASE_PROJECT}" ]]; then
  firebase deploy --only hosting --project "${FIREBASE_PROJECT}"
else
  firebase deploy --only hosting
fi
