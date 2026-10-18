#!/usr/bin/env bash
set -euo pipefail

# Create or update the Cloud Run job that runs the GPU benchmark on one
# RTX PRO 6000 in us-central1. It gets the premium worker's environment, so
# it loads voicebanks and models exactly as the worker does; it writes no
# Firestore jobs, spends no credits and calls no LLM.
#
# The project's RTX PRO 6000 quota (one GPU) is shared with the premium
# worker service: run the job while that service has no instance.
# DRY_RUN=1 prints commands without contacting Google Cloud.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="sightsinger-app"
REGION="us-central1"
JOB="sightsinger-gpu-bench"
IMAGE="${BENCH_IMAGE:-gcr.io/sightsinger-app/ai-singer-gpu-bench:latest}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

umask 077
merged_env_dir="$(mktemp -d)"
merged_env="${merged_env_dir}/gpu-bench.env"
trap 'rm -rf "${merged_env_dir}"' EXIT
python3 - \
  "${ROOT_DIR}/env/prod.env" \
  "${ROOT_DIR}/env/prod.synthesis-worker.env" \
  "${ROOT_DIR}/env/prod.synthesis-worker-premium.env" > "${merged_env}" <<'PY'
import sys

values = {}
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
# The benchmark is not a worker service: it serves no requests.
for key in ("SYNTHESIS_WORKER_URL", "SYNTHESIS_WORKER_AUDIENCE"):
    values.pop(key, None)
for key, value in values.items():
    print(f"{key}={value}")
PY

command=(
  gcloud run jobs deploy "${JOB}"
  --project="${PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --service-account="${RUNTIME_SA}"
  --gpu=1
  --gpu-type=nvidia-rtx-pro-6000
  --no-gpu-zonal-redundancy
  --cpu=20
  --memory=80Gi
  --tasks=1
  --parallelism=1
  --max-retries=0
  --task-timeout=30m
  --env-vars-file="${merged_env}"
)
if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'gcloud'
  printf ' %q' "${command[@]:1}"
  printf '\n'
else
  "${command[@]}"
fi
printf 'Run one benchmark (see scripts/bench/README.md):\n'
printf '  gcloud run jobs execute %s --project=%s --region=%s --wait --update-env-vars=MCP_GPU_WORKERS=4,BENCH_TAKES=4\n' \
  "${JOB}" "${PROJECT}" "${REGION}"
