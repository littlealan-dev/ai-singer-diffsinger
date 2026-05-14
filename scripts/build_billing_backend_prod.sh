#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${BILLING_BACKEND_IMAGE:=gcr.io/${GCP_PROJECT}/sightsinger-billing-api:latest}"

gcloud builds submit "${ROOT_DIR}" \
  --project="${GCP_PROJECT}" \
  --config="${ROOT_DIR}/cloudbuild.billing.yaml" \
  --substitutions="_BILLING_BACKEND_IMAGE=${BILLING_BACKEND_IMAGE}"
