#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_IMAGE="${BACKEND_IMAGE:-gcr.io/sightsinger-app/ai-singer-api:latest}"

# Build the updated image first with scripts/build_backend_prod.sh.
# This deploys directly to production; do not run concurrent backend deployments.
command -v gcloud >/dev/null 2>&1 || {
  printf 'Error: gcloud is required.\n' >&2
  exit 1
}

printf 'Deploying %s to sightsinger-api (production, us-east4).\n' "${BACKEND_IMAGE}"
printf 'Activating the lifecycle entrypoint and /readyz startup probe.\n'

gcloud run services update sightsinger-api \
  --project=sightsinger-app \
  --region=us-east4 \
  --image="${BACKEND_IMAGE}" \
  --concurrency=2 \
  --max=3 \
  --max-instances=3 \
  --env-vars-file="${ROOT_DIR}/env/prod.env" \
  --port=8080 \
  --command=python3 \
  --args=-m,src.backend.server,--host,0.0.0.0,--port,8080,--log-level,debug,--access-log \
  --startup-probe="httpGet.path=/readyz,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=10,timeoutSeconds=2,failureThreshold=24"

# Explicitly remove any prior traffic split and keep future deployments on latest.
# With set -e, a failed deployment never reaches this traffic update.
gcloud run services update-traffic sightsinger-api \
  --project=sightsinger-app \
  --region=us-east4 \
  --to-latest

gcloud run services describe sightsinger-api \
  --project=sightsinger-app \
  --region=us-east4 \
  --format='yaml(status.url,status.latestReadyRevisionName,status.traffic,spec.template.spec.containers[0].command,spec.template.spec.containers[0].args,spec.template.spec.containers[0].startupProbe)'

printf 'Deployment complete. Verify /readyz and a synthesis before disabling UI maintenance mode.\n'
