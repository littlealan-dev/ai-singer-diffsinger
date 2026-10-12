#!/usr/bin/env bash
set -euo pipefail

# Deploy the CPU-only line-preparation worker: the backend built by
# Dockerfile.preprocess-worker (no CUDA, no vocoder), without a GPU, billed per
# request, reachable only by the Cloud Tasks invoker. Each job runs inside one
# request (up to 30 minutes).
#
# Prerequisites: scripts/setup_preprocess_tasks_prod.sh, and an image built by
# scripts/build_preprocess_worker_prod.sh from the same commit as the API's.
#
# To switch the API over afterwards, add to env/prod.env and redeploy it:
#   PREPROCESS_DISPATCH_MODE=cloud_tasks
#   PREPROCESS_WORKER_URL=https://sightsinger-preprocess-worker-373752827978.us-east4.run.app
#   PREPROCESS_WORKER_AUDIENCE=https://sightsinger-preprocess-worker-373752827978.us-east4.run.app
#   PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT=sightsinger-preprocess-invoker@sightsinger-app.iam.gserviceaccount.com
#   PREPROCESS_TASKS_QUEUE=projects/sightsinger-app/locations/us-east4/queues/preprocess-jobs

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="${GCP_REGION:-us-east4}"
SERVICE="${PREPROCESS_WORKER_SERVICE:-sightsinger-preprocess-worker}"
IMAGE="${PREPROCESS_WORKER_IMAGE:-gcr.io/${PROJECT}/sightsinger-preprocess-worker:latest}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"

# The region URL (https://SERVICE-PROJECT_NUMBER.REGION.run.app) is known before
# the service exists, so the worker gets its own OIDC audience in one deploy.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
if [[ -z "${PROJECT_NUMBER}" ]]; then
  echo "Could not determine project number for ${PROJECT}." >&2
  exit 1
fi
worker_url="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

# gcloud reads --env-vars-file as KEY=VALUE only when the name ends in .env.
merged_env_dir="$(mktemp -d)"
merged_env="${merged_env_dir}/preprocess-worker.env"
trap 'rm -rf "${merged_env_dir}"' EXIT

# prod.env first, then the worker's overrides, each key once.
python3 - "${ROOT_DIR}/env/prod.env" "${ROOT_DIR}/env/prod.preprocess-worker.env" "${worker_url}" > "${merged_env}" <<'PY'
import sys
base, overrides, url = sys.argv[1], sys.argv[2], sys.argv[3]
values = {}
for path in (base, overrides):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().replace("__PREPROCESS_WORKER_URL__", url)
for key, value in values.items():
    print(f"{key}={value}")
PY

gcloud run deploy "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${RUNTIME_SA}" \
  --no-allow-unauthenticated \
  --cpu=2 \
  --memory=4Gi \
  --cpu-throttling \
  --concurrency=4 \
  --timeout=1800 \
  --min-instances=0 \
  --max-instances=5 \
  --env-vars-file="${merged_env}"

gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --member="serviceAccount:${INVOKER_SA}" \
  --role="roles/run.invoker" >/dev/null

echo "Worker URL: ${worker_url}"
