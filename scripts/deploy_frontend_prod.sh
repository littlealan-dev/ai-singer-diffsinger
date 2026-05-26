#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="${ROOT_DIR}/ui"

: "${FIREBASE_PROJECT:=}"

APP_VERSION="$(cd "${UI_DIR}" && node -p "require('./package.json').version")"
BUILD_NUMBER="$(git -C "${ROOT_DIR}" rev-parse --short=8 HEAD)"

echo "Building frontend Version ${APP_VERSION}, build ${BUILD_NUMBER}"

VITE_APP_VERSION="${APP_VERSION}" \
VITE_APP_BUILD_NUMBER="${BUILD_NUMBER}" \
npm --prefix "${UI_DIR}" run build

if [[ -n "${FIREBASE_PROJECT}" ]]; then
  firebase deploy --only hosting --project "${FIREBASE_PROJECT}"
else
  firebase deploy --only hosting
fi
