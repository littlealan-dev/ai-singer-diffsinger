#!/usr/bin/env bash
set -euo pipefail

BACKEND_CONCURRENCY="${BACKEND_CONCURRENCY:-2}"
BACKEND_MAX_INSTANCES="${BACKEND_MAX_INSTANCES:-3}"
DRY_RUN="${DRY_RUN:-0}"

if [[ ! "${BACKEND_CONCURRENCY}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'BACKEND_CONCURRENCY must be a positive integer.\n' >&2
  exit 1
fi
if [[ ! "${BACKEND_MAX_INSTANCES}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'BACKEND_MAX_INSTANCES must be a positive integer.\n' >&2
  exit 1
fi

deploy_args=(
  run services update sightsinger-api
  --project=sightsinger-app
  --region=us-east4
  --image=gcr.io/sightsinger-app/ai-singer-api:latest
  --concurrency="${BACKEND_CONCURRENCY}"
  --max="${BACKEND_MAX_INSTANCES}"
  --max-instances="${BACKEND_MAX_INSTANCES}"
  --env-vars-file=env/prod.env
)

if [[ "${DRY_RUN}" == "1" ]]; then
  printf 'gcloud'
  printf ' %q' "${deploy_args[@]}"
  printf '\n'
else
  gcloud "${deploy_args[@]}"
fi
