#!/usr/bin/env bash
set -euo pipefail

# Route all API traffic back to a named revision, such as the last GPU revision.
# Each revision keeps its own image, GPU/CPU and billing configuration; this
# changes traffic only. To follow future latest revisions again, run:
#   gcloud run services update-traffic sightsinger-api \
#     --project=sightsinger-app --region=us-east4 --to-latest
# DRY_RUN=1 prints commands instead of contacting Google Cloud.

if [[ "$#" -ne 1 || ! "$1" =~ ^sightsinger-api-[a-z0-9-]+$ ]]; then
  printf 'Usage: %s <sightsinger-api-revision>\n' "$0" >&2
  exit 1
fi
REVISION="$1"
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

run_gcloud run services update-traffic sightsinger-api \
  --project=sightsinger-app --region=us-east4 \
  --to-revisions="${REVISION}=100"
run_gcloud run services describe sightsinger-api \
  --project=sightsinger-app --region=us-east4 \
  --format='yaml(status.traffic)'
