#!/usr/bin/env bash
set -euo pipefail

# One-off: turn the GPU API service into the CPU-only API. Run it once, after
# the synthesis worker has been deployed and verified (build the CPU image
# first with build_cpu_backend_prod.sh). Later CPU deploys use
# deploy_backend_cpu_prod.sh; run this again only if a GPU revision has been
# deployed since.
#
# DEPLOY_TRAFFIC=none (default): the new revision gets no traffic, and a "cpu"
#   tag with its own URL to check it on; the serving revision keeps every user.
#   Send all traffic to it afterwards with:
#     gcloud run services update-traffic sightsinger-api \
#       --project=sightsinger-app --region=us-east4 --to-latest
# DEPLOY_TRAFFIC=all: deploy and send all traffic to the new revision at once.
# DRY_RUN=1 reads the service and has Cloud Run validate the new definition
#   (services replace --dry-run); nothing is applied.
#
# Why a whole service definition (services replace) rather than update: a GPU
# revision's zonal-redundancy setting stays on the service, update has no flag
# to remove it, and Cloud Run refuses it on a revision without a GPU.
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

# Keep production values private: the definition holds the merged env.
umask 077
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" --format=json > "${work_dir}/current.json"

python3 - "${work_dir}/current.json" "${ROOT_DIR}/env/prod.env" "${ROOT_DIR}/env/prod.api-cpu.env" \
  "${IMAGE}" "${CONCURRENCY}" "${MAX_INSTANCES}" "${DEPLOY_TRAFFIC}" "${TAG}" \
  "${work_dir}/service.json" "${work_dir}/summary.txt" <<'PY'
import json
import sys
import uuid

(current_path, prod_env, overlay_env, image, concurrency, max_instances,
 traffic_mode, tag, out_path, summary_path) = sys.argv[1:]

values = {}
for path in (prod_env, overlay_env):
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

service = json.load(open(current_path, encoding="utf-8"))
status = service.pop("status", {})
serving = [
    {"revisionName": entry.get("revisionName") or status.get("latestReadyRevisionName"),
     "percent": entry["percent"]}
    for entry in status.get("traffic", [])
    if entry.get("percent")
]
if not serving:
    raise SystemExit("No revision of the service serves traffic.")

metadata = service["metadata"]
for key in ("uid", "resourceVersion", "generation", "creationTimestamp", "selfLink"):
    metadata.pop(key, None)
annotations = metadata.setdefault("annotations", {})
for key in ("serving.knative.dev/creator", "serving.knative.dev/lastModifier",
            "run.googleapis.com/operation-id", "run.googleapis.com/ingress-status",
            "run.googleapis.com/urls"):
    annotations.pop(key, None)
# The service-level instance cap (gcloud --max).
annotations["run.googleapis.com/maxScale"] = max_instances

template = service["spec"]["template"]
template_metadata = template["metadata"]
template_metadata.pop("name", None)
template_annotations = template_metadata.setdefault("annotations", {})
# GPU-only settings a CPU revision must not carry.
removed = [
    key for key in ("run.googleapis.com/gpu-zonal-redundancy-disabled",)
    if template_annotations.pop(key, None) is not None
]
template_annotations["run.googleapis.com/cpu-throttling"] = "true"
template_annotations["autoscaling.knative.dev/maxScale"] = max_instances
template_annotations.pop("autoscaling.knative.dev/minScale", None)
template_metadata.setdefault("labels", {})["client.knative.dev/nonce"] = uuid.uuid4().hex[:10]

spec = template["spec"]
node_selector = spec.pop("nodeSelector", None)
spec["containerConcurrency"] = int(concurrency)
spec["timeoutSeconds"] = 900
container = spec["containers"][0]
container["image"] = image
limits = container.setdefault("resources", {}).setdefault("limits", {})
gpu_limit = limits.pop("nvidia.com/gpu", None)
limits["cpu"] = "2"
limits["memory"] = "4Gi"
# As --env-vars-file does: every plain variable comes from the files; values
# from Secret Manager stay unless the files set the same name.
secrets = [e for e in container.get("env", []) if "valueFrom" in e and e["name"] not in values]
container["env"] = [{"name": k, "value": v} for k, v in values.items()] + secrets

if traffic_mode == "none":
    # Users stay on the serving revision; the new one is reachable by its tag.
    service["spec"]["traffic"] = serving + [{"latestRevision": True, "percent": 0, "tag": tag}]
else:
    service["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]

json.dump(service, open(out_path, "w", encoding="utf-8"), indent=2)
with open(summary_path, "w", encoding="utf-8") as summary:
    summary.write(
        "Serving now: " + ", ".join(f"{s['revisionName']}: {s['percent']}%" for s in serving) + "\n"
        f"New revision: image={image} cpu=2 memory=4Gi gpu=none (removed: limit={gpu_limit}, "
        f"selector={node_selector}, annotations={removed}) cpu-throttling=true "
        f"concurrency={concurrency} max={max_instances} timeout=900s\n"
        f"Env: {len(values)} plain variables from prod.env + prod.api-cpu.env, "
        f"secrets kept: {[e['name'] for e in secrets]}\n"
        f"Traffic: {service['spec']['traffic']}\n"
    )
    summary.write("ROLLBACK=" + serving[0]["revisionName"] + "\n")
PY

grep -v '^ROLLBACK=' "${work_dir}/summary.txt"
previous_serving="$(sed -n 's/^ROLLBACK=//p' "${work_dir}/summary.txt")"

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'Validating the definition (nothing is applied)...\n'
  gcloud run services replace "${work_dir}/service.json" \
    --project="${PROJECT}" --region="${REGION}" --dry-run
  exit 0
fi

gcloud run services replace "${work_dir}/service.json" --project="${PROJECT}" --region="${REGION}"
gcloud run services describe "${SERVICE}" \
  --project="${PROJECT}" --region="${REGION}" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'

if [[ "${DEPLOY_TRAFFIC}" == "none" ]]; then
  project_number="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
  printf '\nThe new revision serves no traffic. Check it on its own URL:\n'
  printf '  curl -s https://%s---%s-%s.%s.run.app/readyz\n' \
    "${TAG}" "${SERVICE}" "${project_number}" "${REGION}"
  printf 'Then send all traffic to it:\n'
  printf '  gcloud run services update-traffic %s --project=%s --region=%s --to-latest\n' \
    "${SERVICE}" "${PROJECT}" "${REGION}"
fi
printf 'To roll back: scripts/rollback_backend_to_revision.sh %s\n' "${previous_serving}"
