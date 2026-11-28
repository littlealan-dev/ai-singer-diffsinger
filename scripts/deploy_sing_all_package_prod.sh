#!/usr/bin/env bash
set -euo pipefail

# Deploy the Sing All package job (Cloud Run Job, us-east4): one execution per Sing All job
# renders the instruments, makes the mixes and the zip. Build the image first with
# scripts/build_sing_all_package_prod.sh.
#
# The job starts the whole backend, so it takes the deployed API's settings (copied from the
# sightsinger-api service, secrets as references) and runs as the API's service account.
# It also grants that account what Sing All packaging needs:
#   - start this job with an override (the API and both synthesis workers start it);
#   - sign its own URLs (the zip download link is a signed URL).
# Then set SING_ALL_PACKAGE_TRANSPORT=job and SING_ALL_PACKAGE_JOB in env/prod.env and redeploy the
# API and both synthesis workers. DRY_RUN=1 prints the commands without contacting Google Cloud.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="us-east4"
JOB="sightsinger-sing-all-package"
API_SERVICE="sightsinger-api"
IMAGE="${SING_ALL_PACKAGE_IMAGE:-gcr.io/${PROJECT}/sightsinger-sing-all-package:latest}"
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
env_file="${work_dir}/sing-all-package-env.yaml"
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
  run jobs deploy "${JOB}"
  --project="${PROJECT}"
  --region="${REGION}"
  --image="${IMAGE}"
  --service-account="${RUNTIME_SA}"
  --cpu=4
  --memory=8Gi
  --tasks=1
  --max-retries=1
  --task-timeout=1800s
  --env-vars-file="${env_file}"
)
if [[ "${secrets_arg}" != "--set-secrets=" ]]; then
  deploy_args+=("${secrets_arg}")
fi
run_gcloud "${deploy_args[@]}"

# The API and both synthesis workers start executions with SING_ALL_JOB_ID as an override.
run_gcloud run jobs add-iam-policy-binding "${JOB}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/run.jobsExecutorWithOverrides

# The zip download link is signed with IAM signBlob as this account.
run_gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/iam.serviceAccountTokenCreator

printf 'Package job: projects/%s/locations/%s/jobs/%s\n' "${PROJECT}" "${REGION}" "${JOB}"
printf 'env/prod.env must have SING_ALL_PACKAGE_TRANSPORT=job and SING_ALL_PACKAGE_JOB=projects/%s/locations/%s/jobs/%s;\n' \
  "${PROJECT}" "${REGION}" "${JOB}"
printf 'then redeploy the API and both synthesis workers.\n'
