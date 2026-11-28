#!/usr/bin/env bash
set -euo pipefail

# Deploy the share service (Cloud Run service sightsinger-share, us-east4): public share links,
# their data, score, MIDI, takes and preview image (src/backend/share_app.py). Build the image
# first with scripts/build_share_prod.sh.
#
# It takes the deployed API's settings (copied from the sightsinger-api service, secrets as
# references) and runs as the API's service account, so a share link reads the same Songs and
# files on either service. It is public, like the API: Firebase Hosting sends /s/** and
# /public/s/** to it, and the app fetches share data from it (VITE_SHARE_API_BASE).
# DRY_RUN=1 prints the commands without contacting Google Cloud.

PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="us-east4"
SERVICE="sightsinger-share"
API_SERVICE="sightsinger-api"
IMAGE="${SHARE_IMAGE:-gcr.io/${PROJECT}/sightsinger-share:latest}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi

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

# The API's settings, kept private: plain values to a file, secret references as --set-secrets.
umask 077
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT
env_file="${work_dir}/share-env.yaml"
secrets_file="${work_dir}/secrets.txt"
if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'Copy the settings of the deployed API:\n'
  print_command run services describe "${API_SERVICE}" --project="${PROJECT}" --region="${REGION}" --format=json
  secrets_arg="--set-secrets=<the API's secret references>"
else
  service_file="${work_dir}/api-service.json"
  gcloud run services describe "${API_SERVICE}" --project="${PROJECT}" --region="${REGION}" --format=json \
    > "${service_file}"
  # The Python program comes from the heredoc on stdin, so the service JSON is passed as a file.
  python3 - "${service_file}" "${env_file}" "${secrets_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    service = json.load(source)
env = service["spec"]["template"]["spec"]["containers"][0].get("env", [])
plain, secrets = {}, []
for entry in env:
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
    if ref:
        secrets.append(f"{entry['name']}={ref['name']}:{ref.get('key', 'latest')}")
    else:
        plain[entry["name"]] = entry.get("value", "")
if not plain:
    raise SystemExit("The API service has no settings to copy.")
with open(sys.argv[2], "w", encoding="utf-8") as out:
    json.dump(plain, out)  # JSON is YAML, which --env-vars-file reads.
with open(sys.argv[3], "w", encoding="utf-8") as out:
    out.write(",".join(secrets))
PY
  secrets_arg="--set-secrets=$(cat "${secrets_file}")"
fi

deploy_args=(
  run deploy "${SERVICE}"
  --project="${PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --service-account="${RUNTIME_SA}"
  --allow-unauthenticated
  # Two vCPU: a share page loads every take at once when play is pressed.
  --cpu=2
  # Room for drawing a thumbnail for a Song from before thumbnails existed.
  --memory=1Gi
  --concurrency=40
  --min-instances=0
  --max-instances=10
  --cpu-throttling
  --cpu-boost
  --timeout=300
  --env-vars-file="${env_file}"
  # Checked every second: the service is ready in about 2-3 s.
  --startup-probe='httpGet.path=/readyz,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=1,timeoutSeconds=1,failureThreshold=120'
)
if [[ "${secrets_arg}" != "--set-secrets=" ]]; then
  deploy_args+=("${secrets_arg}")
fi
run_gcloud "${deploy_args[@]}"

printf 'Share service: https://%s-373752827978.%s.run.app\n' "${SERVICE}" "${REGION}"
printf 'Then deploy the frontend (ui/.env.production has VITE_SHARE_API_BASE; firebase.json sends /s/** and /public/s/** here).\n'
