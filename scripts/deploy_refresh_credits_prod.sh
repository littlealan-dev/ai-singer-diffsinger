#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${FIREBASE_TOOLS_VERSION:=latest}"

cd "${ROOT_DIR}"

if [[ ! -d "venv" ]]; then
  echo "Missing Firebase Functions Python venv at ${ROOT_DIR}/venv" >&2
  echo "Create it with: python3 -m venv venv && venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

npx -y "firebase-tools@${FIREBASE_TOOLS_VERSION}" deploy \
  --project "${GCP_PROJECT}" \
  --only functions:billing:refreshCredits
