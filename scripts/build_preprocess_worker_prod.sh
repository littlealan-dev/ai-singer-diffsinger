#!/usr/bin/env bash
set -euo pipefail

# Build the CPU-only line-preparation worker image (Dockerfile.preprocess-worker).
# Build it from the same commit as the API image, so both agree on the job format.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${PREPROCESS_WORKER_IMAGE:=gcr.io/${GCP_PROJECT}/sightsinger-preprocess-worker:latest}"

gcloud builds submit "${ROOT_DIR}" \
  --project="${GCP_PROJECT}" \
  --config="${ROOT_DIR}/cloudbuild.preprocess-worker.yaml" \
  --ignore-file=.gcloudignore.preprocess-worker \
  --substitutions="_PREPROCESS_WORKER_IMAGE=${PREPROCESS_WORKER_IMAGE}"
