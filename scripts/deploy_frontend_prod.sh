#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="${ROOT_DIR}/ui"
FRONTEND_ENV_FILE="${UI_DIR}/.env.production"

: "${FIREBASE_PROJECT:=}"

if [[ -f "${FRONTEND_ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${FRONTEND_ENV_FILE}"
  set +a
else
  echo "Missing frontend production env file: ${FRONTEND_ENV_FILE}" >&2
  exit 1
fi

if [[ -z "${VITE_STRIPE_PUBLISHABLE_KEY:-}" ]]; then
  echo "Missing VITE_STRIPE_PUBLISHABLE_KEY in ${FRONTEND_ENV_FILE}" >&2
  exit 1
fi

APP_VERSION="$(cd "${UI_DIR}" && node -p "require('./package.json').version")"
BUILD_NUMBER="$(git -C "${ROOT_DIR}" rev-parse --short=8 HEAD)"

echo "Building frontend Version ${APP_VERSION}, build ${BUILD_NUMBER}"

VITE_STRIPE_PUBLISHABLE_KEY="${VITE_STRIPE_PUBLISHABLE_KEY}" \
VITE_APP_VERSION="${APP_VERSION}" \
VITE_APP_BUILD_NUMBER="${BUILD_NUMBER}" \
npm --prefix "${UI_DIR}" run build

if ! grep -R -q -- "${VITE_STRIPE_PUBLISHABLE_KEY}" "${UI_DIR}/dist/assets"; then
  echo "Built frontend assets do not contain VITE_STRIPE_PUBLISHABLE_KEY from ${FRONTEND_ENV_FILE}" >&2
  echo "Abort deploy: embedded Stripe checkout would fail with 'Stripe checkout is not configured.'" >&2
  exit 1
fi

if [[ -n "${FIREBASE_PROJECT}" ]]; then
  firebase deploy --only hosting --project "${FIREBASE_PROJECT}"
else
  firebase deploy --only hosting
fi
