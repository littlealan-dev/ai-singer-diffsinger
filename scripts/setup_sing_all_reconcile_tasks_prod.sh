#!/usr/bin/env bash
set -euo pipefail

# Create the sing-all-reconcile queue (us-east4). With SING_ALL_RECONCILE_DISPATCH_MODE=cloud_tasks,
# every Sing All wake (a split, a take or the package ending, a start, a retry) becomes a task that
# POSTs /internal/sing-all/reconcile on the API, so the reconcile runs inside a request with the
# CPU it needs. Inline, it ran as a background task after a response, which the CPU-throttled API
# and preprocess worker starve: Sing All stalled after the split.
#
#   API / preprocess worker / synthesis workers  --enqueue-->  sing-all-reconcile  --OIDC POST-->  sightsinger-api
#
# The tasks sign as the preprocess invoker, like the synthesis tasks; the API runtime's enqueue
# permission and that invoker's OIDC bindings are project-wide, so this script changes no service
# account. Safe to re-run. DRY_RUN=1 prints the commands without calling gcloud.

PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="${GCP_REGION:-us-east4}"
QUEUE="sing-all-reconcile"
INVOKER_SA="sightsinger-preprocess-invoker@${PROJECT}.iam.gserviceaccount.com"
# Wakes are short and idempotent; many Sing All jobs can be waking at once.
MAX_CONCURRENT="${SING_ALL_RECONCILE_QUEUE_MAX_CONCURRENT_DISPATCHES:-50}"
DRY_RUN="${DRY_RUN:-0}"

if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  printf 'DRY_RUN must be 0 or 1.\n' >&2
  exit 1
fi
if [[ ! "${MAX_CONCURRENT}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SING_ALL_RECONCILE_QUEUE_MAX_CONCURRENT_DISPATCHES must be a positive integer.\n' >&2
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

# A reconcile that fails (an error, or the API restarting) is retried; running it again is safe.
create_queue() {
  run_gcloud tasks queues create "${QUEUE}" \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --max-attempts=10 \
    --min-backoff=10s \
    --max-backoff=120s \
    --max-doublings=3 \
    --max-concurrent-dispatches="${MAX_CONCURRENT}"
}

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'If the queue is absent:\n'
  create_queue
elif gcloud tasks queues describe "${QUEUE}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "Queue ${QUEUE} exists."
else
  create_queue
fi

printf 'Queue: projects/%s/locations/%s/queues/%s\n' "${PROJECT}" "${REGION}" "${QUEUE}"
printf 'Tasks sign as: %s\n' "${INVOKER_SA}"
printf 'Next: redeploy the API, the preprocess worker and both synthesis workers (env/prod.env has the SING_ALL_RECONCILE_* settings).\n'
