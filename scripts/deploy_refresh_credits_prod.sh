#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${FIREBASE_TOOLS_VERSION:=latest}"

cd "${ROOT_DIR}"

PROD_ENV_FILE="${ROOT_DIR}/env/prod.env"
FIREBASE_ENV_FILE="${ROOT_DIR}/.env.${GCP_PROJECT}"
FIREBASE_ENV_BACKUP=""
if [[ ! -f "${PROD_ENV_FILE}" ]]; then
  echo "Missing production env file at ${PROD_ENV_FILE}" >&2
  exit 1
fi

cleanup_env_file() {
  if [[ -n "${FIREBASE_ENV_BACKUP}" && -f "${FIREBASE_ENV_BACKUP}" ]]; then
    mv "${FIREBASE_ENV_BACKUP}" "${FIREBASE_ENV_FILE}"
  else
    rm -f "${FIREBASE_ENV_FILE}"
  fi
}
trap cleanup_env_file EXIT

if [[ ! -d "venv" ]]; then
  echo "Missing Firebase Functions Python venv at ${ROOT_DIR}/venv" >&2
  echo "Create it with: python3 -m venv venv && venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

if [[ -f "${FIREBASE_ENV_FILE}" ]]; then
  FIREBASE_ENV_BACKUP="$(mktemp "${TMPDIR:-/tmp}/refreshCredits.env.XXXXXX")"
  cp "${FIREBASE_ENV_FILE}" "${FIREBASE_ENV_BACKUP}"
fi
cp "${PROD_ENV_FILE}" "${FIREBASE_ENV_FILE}"

npx -y "firebase-tools@${FIREBASE_TOOLS_VERSION}" deploy \
  --project "${GCP_PROJECT}" \
  --only functions:billing:refreshCredits
