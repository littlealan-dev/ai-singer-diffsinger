#!/usr/bin/env bash
set -euo pipefail

# Build a uniquely tagged private image for the WebMCP challenge backend.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${GCP_REGION:=us-east4}"
: "${ARTIFACT_REPOSITORY:=sightsinger-webmcp-backend}"
: "${IMAGE_NAME:=backend}"

REQUIRED_VOCODER="${ROOT_DIR}/bundled_assets/vocoders/pc_nsf_hifigan_44.1k/pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx"
if [[ ! -f "${REQUIRED_VOCODER}" ]]; then
  echo "Missing required bundled vocoder: ${REQUIRED_VOCODER}" >&2
  echo "Copy the PC-NSF HiFi-GAN bundle into bundled_assets/vocoders before building." >&2
  exit 1
fi

COMMIT_SHA="$(git -C "${ROOT_DIR}" rev-parse --short=12 HEAD)"
BUILD_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
: "${IMAGE_TAG:=${COMMIT_SHA}-${BUILD_TIMESTAMP}}"
if [[ "${IMAGE_TAG}" == "latest" ]]; then
  echo "IMAGE_TAG must be unique because the Artifact Registry uses immutable tags." >&2
  exit 1
fi

IMAGE_URI="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${ARTIFACT_REPOSITORY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "Building ${IMAGE_URI}"
gcloud builds submit "${ROOT_DIR}" \
  --project="${GCP_PROJECT}" \
  --tag="${IMAGE_URI}"

echo
echo "Image built: ${IMAGE_URI}"
echo "Deploy it with: IMAGE_TAG=${IMAGE_TAG} ./scripts/deploy_webmcp_backend.sh"
