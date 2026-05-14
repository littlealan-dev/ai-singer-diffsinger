#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:=sightsinger-app}"
: "${GCP_REGION:=us-east4}"
: "${BILLING_BACKEND_IMAGE:=gcr.io/${GCP_PROJECT}/sightsinger-billing-api:latest}"
: "${CLOUD_RUN_SERVICE_ACCOUNT:=sightsinger-billing-api-as@${GCP_PROJECT}.iam.gserviceaccount.com}"

gcloud run deploy sightsinger-billing-api \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --image="${BILLING_BACKEND_IMAGE}" \
  --env-vars-file=env/prod.env \
  --service-account="${CLOUD_RUN_SERVICE_ACCOUNT}" \
  --allow-unauthenticated \
  --cpu=1 \
  --memory=512Mi \
  --min-instances=0 \
  --max-instances=10 \
  --timeout=60
