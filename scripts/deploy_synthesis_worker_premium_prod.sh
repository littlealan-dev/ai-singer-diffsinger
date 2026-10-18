#!/usr/bin/env bash
set -euo pipefail

# Deploy the premium synthesis worker on one NVIDIA RTX PRO 6000 Blackwell in
# us-central1, and grant the Cloud Tasks invoker access. Build the image first
# with build_synthesis_worker_blackwell_prod.sh. The API sends jobs here only
# once SYNTHESIS_PREMIUM_ROUTING is on, so deploying changes nothing for users.
# DRY_RUN=1 prints commands without contacting Google Cloud or deploying.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="sightsinger-app"
REGION="us-central1"
SERVICE="sightsinger-synthesis-worker-premium"
IMAGE="${PREMIUM_WORKER_IMAGE:-gcr.io/sightsinger-app/ai-singer-api-blackwell:latest}"
# The project's RTX PRO 6000 quota in us-central1 is one GPU, shared with
# Cloud Run jobs (the GPU benchmark): one instance.
MAX_INSTANCES="${PREMIUM_WORKER_MAX_INSTANCES:-1}"
# Requests held at once: the GPU processes' renders plus as many waiting for
# a slot, so a second wave waits here rather than in Cloud Tasks.
CONCURRENCY="${PREMIUM_WORKER_CONCURRENCY:-8}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"
DRY_RUN="${DRY_RUN:-0}"

for setting in MAX_INSTANCES CONCURRENCY; do
  value="${!setting}"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive integer.\n' "${setting}" >&2
    exit 1
  fi
done
if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

if [[ "${DRY_RUN}" == "1" ]]; then
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
merged_env="${merged_env_dir}/synthesis-worker-premium.env"
trap 'rm -rf "${merged_env_dir}"' EXIT
python3 - "${worker_url}" \
  "${ROOT_DIR}/env/prod.env" \
  "${ROOT_DIR}/env/prod.synthesis-worker.env" \
  "${ROOT_DIR}/env/prod.synthesis-worker-premium.env" > "${merged_env}" <<'PY'
import sys

url, *paths = sys.argv[1:]
values = {}
for path in paths:
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
  --gpu-type=nvidia-rtx-pro-6000 \
  --no-gpu-zonal-redundancy \
  --cpu=20 \
  --memory=80Gi \
  --no-cpu-throttling \
  --concurrency="${CONCURRENCY}" \
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

printf 'Premium worker URL: %s\n' "${worker_url}"
printf 'To route allowlisted users here, add to the API env (prod.api-cpu.env):\n'
printf 'SYNTHESIS_PREMIUM_ROUTING=allowlist\n'
printf 'SYNTHESIS_PREMIUM_WORKER_URL=%s\n' "${worker_url}"
printf 'SYNTHESIS_PREMIUM_TASKS_QUEUE=projects/%s/locations/%s/queues/synthesis-jobs-premium\n' "${PROJECT}" "${REGION}"
