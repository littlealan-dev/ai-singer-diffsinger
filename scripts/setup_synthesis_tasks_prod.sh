#!/usr/bin/env bash
set -euo pipefail

# Create the synthesis-jobs queue for the GPU synthesis worker. The API runtime
# already has Cloud Tasks enqueue permission and the existing preprocess invoker
# already has the OIDC bindings; this script neither creates nor changes either
# service account. Run this before deploy_synthesis_worker_prod.sh.
#
# DRY_RUN=1 prints both possible queue paths without calling gcloud.

PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="${GCP_REGION:-us-east4}"
QUEUE="synthesis-jobs"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"
MAX_CONCURRENT="${SYNTHESIS_QUEUE_MAX_CONCURRENT_DISPATCHES:-4}"
DRY_RUN="${DRY_RUN:-0}"

if [[ ! "${MAX_CONCURRENT}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SYNTHESIS_QUEUE_MAX_CONCURRENT_DISPATCHES must be a positive integer.\n' >&2
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

create_queue() {
  run_gcloud tasks queues create "${QUEUE}" \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --max-attempts=10 \
    --min-backoff=30s \
    --max-backoff=120s \
    --max-doublings=2 \
    --max-concurrent-dispatches="${MAX_CONCURRENT}"
}

update_queue() {
  run_gcloud tasks queues update "${QUEUE}" \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --max-concurrent-dispatches="${MAX_CONCURRENT}"
}

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'If queue is absent:\n'
  create_queue
  printf 'If queue exists:\n'
  update_queue
else
  # Reuse the invoker and project-level bindings installed for preprocessing.
  gcloud iam service-accounts describe "${INVOKER_SA}" --project="${PROJECT}" >/dev/null
  if gcloud tasks queues describe "${QUEUE}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
    update_queue
  else
    create_queue
  fi
fi

printf 'Queue: projects/%s/locations/%s/queues/%s\n' "${PROJECT}" "${REGION}" "${QUEUE}"
printf 'Existing preprocess invoker and IAM bindings reused; no IAM changes.\n'
