#!/usr/bin/env bash
set -euo pipefail

# Build the share service's image (Dockerfile.share): python:3.11-slim with only what the share
# routes need. It can be built at any time; it doesn't depend on the API image.
# DRY_RUN=1 prints the command without building.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
IMAGE="${SHARE_IMAGE:-gcr.io/${PROJECT}/sightsinger-share:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

args=(
  builds submit "${ROOT_DIR}"
  --project="${PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.share.yaml"
  --substitutions="_SHARE_IMAGE=${IMAGE}"
)

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'gcloud'
  printf ' %q' "${args[@]}"
  printf '\n'
else
  gcloud "${args[@]}"
fi
