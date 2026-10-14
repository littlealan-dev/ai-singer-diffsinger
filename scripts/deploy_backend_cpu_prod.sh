#!/usr/bin/env bash
set -euo pipefail

# Prepare only: run this production deploy after the synthesis worker has been
# deployed and verified. Build the CPU image first with build_cpu_backend_prod.sh.
# DRY_RUN=1 prints commands without contacting Google Cloud or deploying.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="sightsinger-app"
REGION="us-east4"
SERVICE="sightsinger-api"
IMAGE="${CPU_BACKEND_IMAGE:-gcr.io/${PROJECT}/sightsinger-cpu-backend:latest}"
CONCURRENCY="${BACKEND_CPU_CONCURRENCY:-20}"
MAX_INSTANCES="${BACKEND_CPU_MAX_INSTANCES:-10}"
DRY_RUN="${DRY_RUN:-0}"

for setting in CONCURRENCY MAX_INSTANCES; do
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

# The .env suffix tells gcloud to parse KEY=VALUE rather than YAML. Keep the
# merged production values private, including when printing a dry run.
umask 077
merged_env_dir="$(mktemp -d)"
merged_env="${merged_env_dir}/api-cpu.env"
trap 'rm -rf "${merged_env_dir}"' EXIT
python3 - "${ROOT_DIR}/env/prod.env" "${ROOT_DIR}/env/prod.api-cpu.env" > "${merged_env}" <<'PY'
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
invoker = values.get("PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT", "")
if not invoker:
    raise SystemExit("prod.env must set PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT")
if values.get("SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT") != "__PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT__":
    raise SystemExit("CPU API overlay must reuse the preprocess invoker")
values["SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT"] = invoker
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

traffic_args=(
  run services describe "${SERVICE}"
  --project="${PROJECT}"
  --region="${REGION}"
  --format='json(status.traffic,status.latestReadyRevisionName)'
)
if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'Read the previous serving revision(s) before deploying:\n'
  print_command "${traffic_args[@]}"
else
  previous_traffic="$(gcloud "${traffic_args[@]}")"
  printf '%s' "${previous_traffic}" | python3 -c '
import json, sys
status = json.load(sys.stdin).get("status", {})
serving = [
    "{}: {}%".format(
        entry.get("revisionName") or status.get("latestReadyRevisionName") or "unknown",
        entry.get("percent", 0),
    )
    for entry in status.get("traffic", [])
    if entry.get("percent", 0) > 0
]
print("Previous serving revision(s): " + (", ".join(serving) or "none"))
'
fi

deploy_args=(
  run services update "${SERVICE}"
  --project="${PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --gpu=0
  --cpu-throttling
  --cpu=2
  --memory=4Gi
  --concurrency="${CONCURRENCY}"
  --max="${MAX_INSTANCES}"
  --max-instances="${MAX_INSTANCES}"
  --min-instances=0
  --timeout=900
  --env-vars-file="${merged_env}"
  --port=8080
  --command=python3
  --args=-m,src.backend.server,--host,0.0.0.0,--port,8080,--log-level,debug,--access-log
  --startup-probe='httpGet.path=/readyz,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=10,timeoutSeconds=2,failureThreshold=24'
)
run_gcloud "${deploy_args[@]}"
run_gcloud run services update-traffic "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" --to-latest
run_gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
