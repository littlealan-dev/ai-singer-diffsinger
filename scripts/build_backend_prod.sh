#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${BACKEND_IMAGE:=gcr.io/${GCP_PROJECT}/ai-singer-api:latest}"

gcloud builds submit "${ROOT_DIR}" \
  --project="${GCP_PROJECT}" \
  --tag "${BACKEND_IMAGE}"
