#!/usr/bin/env bash
set -euo pipefail

# Deploy the GPU synthesis worker and grant the existing Cloud Tasks invoker
# access in one script. Build the API GPU image first with build_backend_prod.sh.
# DRY_RUN=1 prints commands without contacting Google Cloud or deploying.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="sightsinger-app"
REGION="us-east4"
SERVICE="sightsinger-synthesis-worker"
IMAGE="${SYNTHESIS_WORKER_IMAGE:-gcr.io/sightsinger-app/ai-singer-api:latest}"
MAX_INSTANCES="${SYNTHESIS_WORKER_MAX_INSTANCES:-2}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"
DRY_RUN="${DRY_RUN:-0}"

if [[ ! "${MAX_INSTANCES}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SYNTHESIS_WORKER_MAX_INSTANCES must be a positive integer.\n' >&2
  exit 1
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  # This project number is part of the SIG-105 settings contract. Override it
  # for an isolated dry run with a different project number if needed.
  PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-373752827978}"
else
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
fi
if [[ ! "${PROJECT_NUMBER}" =~ ^[0-9]+$ ]]; then
  printf 'Could not determine a numeric project number.\n' >&2
  exit 1
fi
worker_url="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

# gcloud requires the merged KEY=VALUE file to end in .env. Never print its
# contents: prod.env may contain deployment configuration.
umask 077
merged_env_dir="$(mktemp -d)"
merged_env="${merged_env_dir}/synthesis-worker.env"
trap 'rm -rf "${merged_env_dir}"' EXIT
python3 - "${ROOT_DIR}/env/prod.env" "${ROOT_DIR}/env/prod.synthesis-worker.env" "${worker_url}" > "${merged_env}" <<'PY'
import sys

base, overrides, url = sys.argv[1:]
values = {}
for path in (base, overrides):
    with open(path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().replace("__SYNTHESIS_WORKER_URL__", url)
for key, value in values.items():
    print(f"{key}={value}")
PY

print_command() {
  printf 'gcloud'
  printf ' %q' "$@"
  printf '\n'
}

run_gcloud() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    print_command "$@"
  else
    gcloud "$@"
  fi
}

run_gcloud run deploy "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${RUNTIME_SA}" \
  --no-allow-unauthenticated \
  --gpu=1 \
  --gpu-type=nvidia-l4 \
  --no-gpu-zonal-redundancy \
  --cpu=4 \
  --memory=16Gi \
  --no-cpu-throttling \
  --concurrency=2 \
  --timeout=1800 \
  --min-instances=0 \
  --max-instances="${MAX_INSTANCES}" \
  --env-vars-file="${merged_env}" \
  --port=8080 \
  --command=python3 \
  --args=-m,src.backend.server,--host,0.0.0.0,--port,8080,--log-level,debug,--access-log \
  --startup-probe="httpGet.path=/readyz,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=10,timeoutSeconds=2,failureThreshold=24"

run_gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --member="serviceAccount:${INVOKER_SA}" \
  --role=roles/run.invoker

printf 'Worker URL: %s\n' "${worker_url}"
printf 'Add these five lines to prod.env when switching the API over:\n'
printf 'SYNTHESIS_DISPATCH_MODE=cloud_tasks\n'
printf 'SYNTHESIS_WORKER_URL=%s\n' "${worker_url}"
printf 'SYNTHESIS_WORKER_AUDIENCE=%s\n' "${worker_url}"
printf 'SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT=%s\n' "${INVOKER_SA}"
printf 'SYNTHESIS_TASKS_QUEUE=projects/%s/locations/%s/queues/synthesis-jobs\n' "${PROJECT}" "${REGION}"
