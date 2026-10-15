#!/usr/bin/env bash
set -euo pipefail

# Prepare only: run this production deploy after the synthesis worker has been
# deployed and verified. Build the CPU image first with build_cpu_backend_prod.sh.
#
# DEPLOY_TRAFFIC=none (default): the new revision gets no traffic, and a "cpu"
#   tag with its own URL to check it on; the serving revision keeps every user.
#   Send all traffic to it afterwards with:
#     gcloud run services update-traffic sightsinger-api \
#       --project=sightsinger-app --region=us-east4 --to-latest
# DEPLOY_TRAFFIC=all: deploy and send all traffic to the new revision at once.
# DRY_RUN=1 prints commands without contacting Google Cloud or deploying.
#
# Going from a GPU revision to CPU is done once with
# convert_backend_to_cpu_prod.sh: a GPU revision's settings stay on the
# service, and update cannot remove all of them. This script stops if they
# are still there, so it passes no GPU flag: --gpu=0 could only be a no-op,
# and it made gcloud warn "No GPU type is provided, defaulting to nvidia-l4".
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="sightsinger-app"
REGION="us-east4"
SERVICE="sightsinger-api"
IMAGE="${CPU_BACKEND_IMAGE:-gcr.io/${PROJECT}/sightsinger-cpu-backend:latest}"
# 10 per instance to start (SIG-105 M4 load test): score tools run two at a
# time per instance, so fewer users per instance means more instances' tools.
CONCURRENCY="${BACKEND_CPU_CONCURRENCY:-10}"
MAX_INSTANCES="${BACKEND_CPU_MAX_INSTANCES:-10}"
DEPLOY_TRAFFIC="${DEPLOY_TRAFFIC:-none}"
TAG="cpu"
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
if [[ "${DEPLOY_TRAFFIC}" != "none" && "${DEPLOY_TRAFFIC}" != "all" ]]; then
  printf 'DEPLOY_TRAFFIC must be none or all.\n' >&2
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
  previous_serving="<previous revision>"
else
  previous_traffic="$(gcloud "${traffic_args[@]}")"
  previous_serving="$(printf '%s' "${previous_traffic}" | python3 -c '
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
print(", ".join(serving) or "none")
')"
  printf 'Previous serving revision(s): %s\n' "${previous_serving}"
fi

if [[ "${DRY_RUN}" != "1" ]]; then
  gpu_settings="$(gcloud run services describe "${SERVICE}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format="value(spec.template.spec.nodeSelector,spec.template.metadata.annotations.'run.googleapis.com/gpu-zonal-redundancy-disabled')")"
  if [[ -n "${gpu_settings//[[:space:]]/}" ]]; then
    printf 'The service still has GPU revision settings: %s\n' "${gpu_settings}" >&2
    printf 'Run scripts/convert_backend_to_cpu_prod.sh once instead; later CPU deploys use this script.\n' >&2
    exit 1
  fi
fi

deploy_args=(
  run services update "${SERVICE}"
  --project="${PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
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
if [[ "${DEPLOY_TRAFFIC}" == "none" ]]; then
  # Traffic that followed LATEST is pinned to the revision serving it now.
  deploy_args+=(--no-traffic --tag="${TAG}")
fi
run_gcloud "${deploy_args[@]}"
if [[ "${DEPLOY_TRAFFIC}" == "all" ]]; then
  run_gcloud run services update-traffic "${SERVICE}" \
    --project="${PROJECT}" --region="${REGION}" --to-latest
fi
run_gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'

if [[ "${DEPLOY_TRAFFIC}" == "none" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    project_number="${GCP_PROJECT_NUMBER:-373752827978}"
  else
    project_number="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
  fi
  printf '\nThe new revision serves no traffic. Check it on its own URL:\n'
  printf '  curl -s https://%s---%s-%s.%s.run.app/readyz\n' \
    "${TAG}" "${SERVICE}" "${project_number}" "${REGION}"
  printf 'Then send all traffic to it:\n'
  printf '  gcloud run services update-traffic %s --project=%s --region=%s --to-latest\n' \
    "${SERVICE}" "${PROJECT}" "${REGION}"
fi
printf 'To roll back: scripts/rollback_backend_to_revision.sh %s\n' "${previous_serving%%:*}"
