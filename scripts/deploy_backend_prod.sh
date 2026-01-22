#!/usr/bin/env bash
set -euo pipefail

gcloud run services update sightsinger-api \
  --project=sightsinger-app \
  --region=us-east4 \
  --image=gcr.io/sightsinger-app/ai-singer-api:latest \
  --env-vars-file=env/prod.env
