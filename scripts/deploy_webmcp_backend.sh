#!/usr/bin/env bash
set -euo pipefail

# Deploy an already-built private WebMCP challenge image. This script never
# updates the production sightsinger-api service.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${GCP_REGION:=us-east4}"
: "${ARTIFACT_REPOSITORY:=sightsinger-webmcp-backend}"
: "${SERVICE_NAME:=sightsinger-webmcp-backend}"
: "${IMAGE_NAME:=backend}"
: "${CLOUD_RUN_SERVICE_ACCOUNT:=sightsinger-webmcp-backend@${GCP_PROJECT}.iam.gserviceaccount.com}"
: "${IMAGE_TAG:?Set IMAGE_TAG to the immutable tag printed by build_webmcp_backend.sh.}"

ENV_FILE="${ROOT_DIR}/env/webmcp.prod.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing challenge environment file: ${ENV_FILE}" >&2
  exit 1
fi

IMAGE_URI="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${ARTIFACT_REPOSITORY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "Deploying ${IMAGE_URI} as Cloud Run service ${SERVICE_NAME} in ${GCP_REGION}"
gcloud run deploy "${SERVICE_NAME}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --image="${IMAGE_URI}" \
  --env-vars-file="${ENV_FILE}" \
  --service-account="${CLOUD_RUN_SERVICE_ACCOUNT}" \
  --allow-unauthenticated \
  --ingress=all \
  --execution-environment=gen2 \
  --cpu=4 \
  --memory=16Gi \
  --no-cpu-throttling \
  --gpu=1 \
  --gpu-type=nvidia-l4 \
  --no-gpu-zonal-redundancy \
  --concurrency=2 \
  --min=0 \
  --max=1 \
  --timeout=1200s \
  --startup-probe="httpGet.path=/readyz,httpGet.port=8080,periodSeconds=10,timeoutSeconds=5,failureThreshold=30" \
  --labels="application=sightsinger,environment=webmcp-challenge"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --format='value(status.url)')"

echo
echo "WebMCP backend deployed: ${SERVICE_URL}"
echo "Set VITE_API_BASE=${SERVICE_URL} in the challenge frontend before deploying it."
