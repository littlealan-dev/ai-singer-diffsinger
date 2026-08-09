#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${GCP_PROJECT:=sightsinger-app}"
: "${GCP_REGION:=us-east4}"
: "${GCP_SCHEDULER_REGION:=us-central1}"
: "${BILLING_SERVICE:=sightsinger-billing-api}"
: "${MARKETING_DOI_SCHEDULER_JOB:=marketing-doi-reconcile}"
: "${MARKETING_DOI_SCHEDULER_SERVICE_ACCOUNT:=sightsinger-marketing-scheduler-as@${GCP_PROJECT}.iam.gserviceaccount.com}"

PROD_ENV_FILE="${ROOT_DIR}/env/prod.env"
if [[ ! -f "${PROD_ENV_FILE}" ]]; then
  echo "Missing production env file at ${PROD_ENV_FILE}." >&2
  exit 1
fi

read_env_value() {
  local key="$1"
  local value
  value="$(sed -n -E "s/^${key}=(.*)$/\\1/p" "${PROD_ENV_FILE}" | tail -n 1)"
  value="${value#\"}"
  value="${value%\"}"
  printf '%s' "${value}"
}

: "${MARKETING_DOI_RECONCILE_SCHEDULE:=$(read_env_value MARKETING_DOI_RECONCILE_SCHEDULE)}"
: "${MARKETING_DOI_CONFIGURED_AUDIENCE:=$(read_env_value MARKETING_DOI_SCHEDULER_AUDIENCE)}"
if [[ -z "${MARKETING_DOI_RECONCILE_SCHEDULE}" || -z "${MARKETING_DOI_CONFIGURED_AUDIENCE}" ]]; then
  echo "MARKETING_DOI_RECONCILE_SCHEDULE and MARKETING_DOI_SCHEDULER_AUDIENCE must be set in env/prod.env." >&2
  exit 1
fi

SERVICE_URL="$(gcloud run services describe "${BILLING_SERVICE}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --format='value(status.url)')"
if [[ -z "${SERVICE_URL}" ]]; then
  echo "Could not determine Cloud Run URL for ${BILLING_SERVICE}." >&2
  exit 1
fi
if [[ "${MARKETING_DOI_CONFIGURED_AUDIENCE}" != "${SERVICE_URL}" ]]; then
  echo "MARKETING_DOI_SCHEDULER_AUDIENCE does not match the deployed billing service URL." >&2
  echo "Deploy the billing service with the current service URL configured before creating the Scheduler job." >&2
  exit 1
fi

gcloud iam service-accounts describe "${MARKETING_DOI_SCHEDULER_SERVICE_ACCOUNT}" \
  --project="${GCP_PROJECT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${MARKETING_DOI_SCHEDULER_SERVICE_ACCOUNT%@*}" \
    --project="${GCP_PROJECT}" \
    --display-name="SightSinger marketing DOI scheduler"

gcloud run services add-iam-policy-binding "${BILLING_SERVICE}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --member="serviceAccount:${MARKETING_DOI_SCHEDULER_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" >/dev/null

JOB_ARGS=(
  --project="${GCP_PROJECT}"
  --location="${GCP_SCHEDULER_REGION}"
  --schedule="${MARKETING_DOI_RECONCILE_SCHEDULE}"
  --time-zone="Etc/UTC"
  --uri="${SERVICE_URL}/internal/marketing/doi-reconcile"
  --http-method=POST
  --attempt-deadline=300s
  --oidc-service-account-email="${MARKETING_DOI_SCHEDULER_SERVICE_ACCOUNT}"
  --oidc-token-audience="${SERVICE_URL}"
  --max-retry-attempts=0
)

if gcloud scheduler jobs describe "${MARKETING_DOI_SCHEDULER_JOB}" \
  --project="${GCP_PROJECT}" \
  --location="${GCP_SCHEDULER_REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${MARKETING_DOI_SCHEDULER_JOB}" "${JOB_ARGS[@]}"
else
  gcloud scheduler jobs create http "${MARKETING_DOI_SCHEDULER_JOB}" "${JOB_ARGS[@]}"
fi

echo "Configured ${MARKETING_DOI_SCHEDULER_JOB} for ${SERVICE_URL}."
