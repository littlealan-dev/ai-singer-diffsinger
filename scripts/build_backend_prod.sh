#!/usr/bin/env bash
set -euo pipefail

# Build the standard (L4) synthesis worker image (Dockerfile) with Cloud Build
# (cloudbuild.synthesis-worker.yaml). Cloud Build first bakes the enabled
# voicebanks of env/voicebank_manifest.prod.json into the image, from the
# voicebank bucket in env/prod.env (VOICEBANK_BUCKET, else STORAGE_BUCKET).
# Build from a checkout with the bundled vocoder as a real folder
# (bundled_assets/), not a symlink: the upload does not follow symlinks.
# DRY_RUN=1 prints the command without building.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${BACKEND_IMAGE:=gcr.io/${GCP_PROJECT}/ai-singer-api:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi
if [[ -L "${ROOT_DIR}/bundled_assets" || ! -d "${ROOT_DIR}/bundled_assets/vocoders" ]]; then
  printf 'bundled_assets/vocoders must be a real folder in %s.\n' "${ROOT_DIR}" >&2
  exit 1
fi

env_value() {
  sed -n "s/^$1=//p" "${ROOT_DIR}/env/prod.env" | tail -n 1
}
VOICEBANK_BUCKET="$(env_value VOICEBANK_BUCKET)"
: "${VOICEBANK_BUCKET:=$(env_value STORAGE_BUCKET)}"
if [[ -z "${VOICEBANK_BUCKET}" ]]; then
  printf 'VOICEBANK_BUCKET or STORAGE_BUCKET must be set in env/prod.env.\n' >&2
  exit 1
fi

command=(
  gcloud builds submit "${ROOT_DIR}"
  --project="${GCP_PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.synthesis-worker.yaml"
  --substitutions="_IMAGE=${BACKEND_IMAGE},_VOICEBANK_BUCKET=${VOICEBANK_BUCKET}"
)
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%q ' "${command[@]}"
  printf '\n'
else
  "${command[@]}"
fi
