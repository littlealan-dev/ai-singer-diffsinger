#!/usr/bin/env bash
set -euo pipefail

# Build the Sing All package job's image (Dockerfile.sing-all-package-job): the CPU API image
# plus ffmpeg, FluidSynth and the FluidR3 soundfont, which Cloud Build copies from
# gs://<bucket>/assets/soundfonts/FluidR3_GM.sf2 (the bucket from env/prod.env: VOICEBANK_BUCKET,
# else STORAGE_BUCKET). Run it after scripts/build_cpu_backend_prod.sh, so both images have the
# same code. DRY_RUN=1 prints the command without building.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
BASE_IMAGE="${CPU_BACKEND_IMAGE:-gcr.io/${PROJECT}/sightsinger-cpu-backend:latest}"
IMAGE="${SING_ALL_PACKAGE_IMAGE:-gcr.io/${PROJECT}/sightsinger-sing-all-package:latest}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

env_value() {
  sed -n "s/^$1=//p" "${ROOT_DIR}/env/prod.env" | tail -n 1
}
SOUNDFONT_BUCKET="$(env_value VOICEBANK_BUCKET)"
: "${SOUNDFONT_BUCKET:=$(env_value STORAGE_BUCKET)}"
if [[ -z "${SOUNDFONT_BUCKET}" ]]; then
  printf 'VOICEBANK_BUCKET or STORAGE_BUCKET must be set in env/prod.env.\n' >&2
  exit 1
fi

args=(
  builds submit "${ROOT_DIR}"
  --project="${PROJECT}"
  --config="${ROOT_DIR}/cloudbuild.sing-all-package.yaml"
  --ignore-file=.gcloudignore.preprocess-worker
  --substitutions="_BASE_IMAGE=${BASE_IMAGE},_IMAGE=${IMAGE},_SOUNDFONT_BUCKET=${SOUNDFONT_BUCKET}"
)

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'gcloud'
  printf ' %q' "${args[@]}"
  printf '\n'
else
  gcloud "${args[@]}"
fi
