#!/usr/bin/env bash
set -euo pipefail

# One-time setup for running line-preparation (preprocess) jobs through Cloud
# Tasks on the CPU-only worker service. Safe to re-run: existing resources are
# kept. Run before scripts/deploy_preprocess_worker_prod.sh.
#
#   sightsinger-api (GPU)  --enqueue-->  queue preprocess-jobs  --OIDC POST-->  sightsinger-preprocess-worker (CPU)
#
# Identities:
# - sightsinger-cloud-run-sa: runtime of both services; enqueues tasks.
# - sightsinger-preprocess-invoker: the identity Cloud Tasks signs requests as;
#   the only caller allowed to invoke the worker service.

PROJECT="${GCP_PROJECT:-sightsinger-app}"
REGION="${GCP_REGION:-us-east4}"
QUEUE="${PREPROCESS_TASKS_QUEUE_NAME:-preprocess-jobs}"
RUNTIME_SA="sightsinger-cloud-run-sa@${PROJECT}.iam.gserviceaccount.com"
INVOKER_NAME="sightsinger-preprocess-invoker"
INVOKER_SA="${INVOKER_NAME}@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
TASKS_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

gcloud services enable cloudtasks.googleapis.com --project="${PROJECT}"

if ! gcloud iam service-accounts describe "${INVOKER_SA}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${INVOKER_NAME}" \
    --project="${PROJECT}" \
    --display-name="Cloud Tasks caller of the preprocess worker"
fi

# Queue retries cover both a lost worker (any non-2xx) and "busy" (409) while
# a previous attempt's lease runs out. The job itself allows at most
# PREPROCESS_JOB_MAX_DISPATCH_ATTEMPTS real runs.
if gcloud tasks queues describe "${QUEUE}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "Queue ${QUEUE} exists."
else
  gcloud tasks queues create "${QUEUE}" \
    --location="${REGION}" \
    --project="${PROJECT}" \
    --max-attempts=10 \
    --min-backoff=30s \
    --max-backoff=120s \
    --max-doublings=2 \
    --max-concurrent-dispatches=20
fi

# The API's runtime enqueues tasks that carry the invoker's OIDC token.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/cloudtasks.enqueuer" \
  --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${INVOKER_SA}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/iam.serviceAccountUser" >/dev/null
# Cloud Tasks mints the OIDC token as the invoker.
gcloud iam service-accounts add-iam-policy-binding "${INVOKER_SA}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${TASKS_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null

echo "Queue: projects/${PROJECT}/locations/${REGION}/queues/${QUEUE}"
echo "Invoker: ${INVOKER_SA}"
echo "Next: scripts/deploy_preprocess_worker_prod.sh, then grant the invoker run.invoker on the worker (the deploy script does this)."
