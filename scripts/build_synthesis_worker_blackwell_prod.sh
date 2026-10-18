#!/usr/bin/env bash
set -euo pipefail

# Build the premium synthesis worker image for the RTX PRO 6000 Blackwell
# (Dockerfile.blackwell, Python 3.11, CUDA 13 from pip). The L4 worker keeps
# its own image (build_backend_prod.sh). Build from a checkout with the bundled
# vocoder as a real folder (bundled_assets/), not a symlink: the upload does
# not follow symlinks.
# DRY_RUN=1 prints the command without building.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${BLACKWELL_IMAGE:=gcr.io/${GCP_PROJECT}/ai-singer-api-blackwell:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi
if [[ -L "${ROOT_DIR}/bundled_assets" || ! -d "${ROOT_DIR}/bundled_assets/vocoders" ]]; then
  printf 'bundled_assets/vocoders must be a real folder in %s.\n' "${ROOT_DIR}" >&2
  exit 1
fi

command=(
  gcloud builds submit "${ROOT_DIR}"
  --project="${GCP_PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.blackwell.yaml"
  --substitutions="_BLACKWELL_IMAGE=${BLACKWELL_IMAGE}"
)
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%q ' "${command[@]}"
  printf '\n'
else
  "${command[@]}"
fi
