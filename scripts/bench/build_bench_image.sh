#!/usr/bin/env bash
set -euo pipefail

# Build the GPU benchmark image on top of the production Blackwell image,
# pinned to that image's current digest. Build the Blackwell image first
# (scripts/build_synthesis_worker_blackwell_prod.sh).
# DRY_RUN=1 prints the commands without building.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${BLACKWELL_IMAGE:=gcr.io/${GCP_PROJECT}/ai-singer-api-blackwell:latest}"
: "${BENCH_IMAGE:=gcr.io/${GCP_PROJECT}/ai-singer-gpu-bench:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  base_image="${BLACKWELL_IMAGE%%:*}@sha256:<current digest of ${BLACKWELL_IMAGE}>"
else
  digest="$(gcloud container images describe "${BLACKWELL_IMAGE}" \
    --project="${GCP_PROJECT}" --format='value(image_summary.digest)')"
  base_image="${BLACKWELL_IMAGE%%:*}@${digest}"
fi
printf 'Benchmark base image: %s\n' "${base_image}"

command=(
  gcloud builds submit "${ROOT_DIR}"
  --project="${GCP_PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.bench.yaml"
  --substitutions="_BASE_IMAGE=${base_image},_BENCH_IMAGE=${BENCH_IMAGE}"
)
if [[ "${DRY_RUN}" == "1" ]]; then
  printf '%q ' "${command[@]}"
  printf '\n'
else
  "${command[@]}"
fi
