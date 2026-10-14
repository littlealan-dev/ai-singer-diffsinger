#!/usr/bin/env bash
set -euo pipefail

# Build the shared CPU backend image for the API and preprocess worker from
# Dockerfile.preprocess-worker. The original preprocess build script still
# produces its existing image name.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
IMAGE="${CPU_BACKEND_IMAGE:-gcr.io/${PROJECT}/sightsinger-cpu-backend:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

args=(
  builds submit "${ROOT_DIR}"
  --project="${PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.preprocess-worker.yaml"
  --ignore-file=.gcloudignore.preprocess-worker
  --substitutions="_PREPROCESS_WORKER_IMAGE=${IMAGE}"
)

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'gcloud'
  printf ' %q' "${args[@]}"
  printf '\n'
else
  gcloud "${args[@]}"
fi
