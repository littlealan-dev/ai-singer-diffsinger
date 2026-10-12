#!/usr/bin/env bash
set -euo pipefail

# Deploy the CPU-only line-preparation worker: the same image as
# sightsinger-api, without a GPU, billed per request, reachable only by the
# Cloud Tasks invoker. Each job runs inside one request (up to 30 minutes).
#
# Prerequisites: scripts/setup_preprocess_tasks_prod.sh, and an image built by
# scripts/build_backend_prod.sh.
#
# To switch the API over afterwards, add to env/prod.env and redeploy it:
#   PREPROCESS_DISPATCH_MODE=cloud_tasks
#   PREPROCESS_WORKER_URL=<the worker URL printed below>
#   PREPROCESS_WORKER_AUDIENCE=<the same URL>
#   PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT=sightsinger-preprocess-invoker@sightsinger-app.iam.gserviceaccount.com
#   PREPROCESS_TASKS_QUEUE=projects/sightsinger-app/locations/us-east4/queues/preprocess-jobs

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="${GCP_REGION:-us-east4}"
SERVICE="${PREPROCESS_WORKER_SERVICE:-sightsinger-preprocess-worker}"
IMAGE="${BACKEND_IMAGE:-gcr.io/${PROJECT}/ai-singer-api:latest}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"

# The worker's URL is only known once the service exists; its env needs it as
# the OIDC audience, so a first deploy is followed by an env update.
merged_env="$(mktemp)"
trap 'rm -f "${merged_env}"' EXIT

write_env() {
  local worker_url="$1"
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
}

existing_url="$(gcloud run services describe "${SERVICE}" --project="${PROJECT}" --region="${REGION}" --format='value(status.url)' 2>/dev/null || true)"
write_env "${existing_url:-https://pending.invalid}"

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

worker_url="$(gcloud run services describe "${SERVICE}" --project="${PROJECT}" --region="${REGION}" --format='value(status.url)')"
if [[ "${worker_url}" != "${existing_url}" ]]; then
  write_env "${worker_url}"
  gcloud run services update "${SERVICE}" \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --env-vars-file="${merged_env}"
fi

gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --member="serviceAccount:${INVOKER_SA}" \
  --role="roles/run.invoker" >/dev/null

echo "Worker URL: ${worker_url}"
