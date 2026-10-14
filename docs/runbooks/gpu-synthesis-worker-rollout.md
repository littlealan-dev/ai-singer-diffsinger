# GPU Synthesis Worker and CPU API Rollout (SIG-105)

This guide moves synthesis off the API to a GPU Cloud Tasks worker, then
moves the API itself to CPU-only, request-billed Cloud Run. Each phase is
checked before the next one, and each has its own rollback.

```
today:       sightsinger-api (GPU L4, max 3)  — chat, score tools, synthesis inline
after:       sightsinger-api (CPU, request-billed, max 10)  — chat, score tools
             sightsinger-synthesis-worker (GPU L4, max 3)   ← Cloud Tasks queue synthesis-jobs
             sightsinger-preprocess-worker (CPU, max 5)     ← Cloud Tasks queue preprocess-jobs (unchanged)
```

**GPU quota.** The project has 3 L4 GPUs in `us-east4`, and every GPU service's
max instances count against it. The API therefore drops to 1 GPU instance
before the worker takes 2, and the worker only rises to 3 once the API has no
GPU. Phases 3 and 6 are ordered for that.

| Phase | API | Synthesis worker | Queue `synthesis-jobs` | GPUs used (max) |
|---|---|---|---|---|
| Now | GPU, max 3, synthesis inline | — | — | 3 |
| 3. Free one GPU for the worker | GPU, **max 1**, concurrency 10 | — | — | 1 |
| 4. Worker deployed | GPU, max 1 | GPU, max 2 | 4 dispatches | 3 |
| 5. API sends synthesis to the worker | GPU, max 1, synthesis via Cloud Tasks | max 2 | 4 | 3 |
| 6. CPU API | **CPU**, max 10, concurrency 10 | max 2 | 4 | 2 |
| 7. Final limits | CPU | **max 3** | **6** | 3 |

Every command runs from the repository root. Nothing here changes the
billing backend, the frontend's hosting, or Firestore rules.

## 1. Before you start

- [ ] Merge `littlealan/gpu-synthesis-worker` into `main` and check out that
      commit. Build every image in phase 2 from this same commit.
- [ ] No maintenance window is needed, but choose a quiet hour: phase 3 caps
      the API at one GPU instance until phase 6.
- [ ] Note the API revision serving now; it is the rollback target for
      phases 3 to 5:

```bash
gcloud run services describe sightsinger-api --project=sightsinger-app --region=us-east4 \
  --format='value(status.latestReadyRevisionName,status.traffic)'
```

## 2. Build the images

All three are built from the same commit.

```bash
scripts/build_backend_prod.sh            # GPU image: the API now, and the synthesis worker
scripts/build_preprocess_worker_prod.sh  # preprocess worker image
scripts/build_cpu_backend_prod.sh        # CPU image for the API in phase 6
```

## 3. Update the preprocess worker, and cap the API at one GPU

The preprocess worker runs the same backend code, so it is redeployed from
this commit. Then the API drops to one GPU instance, freeing two GPUs for the
worker. Synthesis still runs on the API in this phase.

```bash
scripts/deploy_preprocess_worker_prod.sh
gcloud run services update-traffic sightsinger-preprocess-worker --project=sightsinger-app --region=us-east4 --to-latest
BACKEND_CONCURRENCY=10 BACKEND_MAX_INSTANCES=1 scripts/deploy_backend_prod.sh
gcloud run services update-traffic sightsinger-api --project=sightsinger-app --region=us-east4 --to-latest
```

Both services follow their latest revision today, so the `--to-latest` steps
only matter if traffic was pinned since.

Check:

- [ ] The API's new revision serves 100% (`describe` as in step 1). Note its
      name: it is the rollback target for phase 6.
- [ ] One line preparation (a split) and one take work in the app.

Rollback: `scripts/rollback_backend_to_revision.sh <revision from step 1>`.

## 4. Create the queue and deploy the synthesis worker

```bash
scripts/setup_synthesis_tasks_prod.sh       # queue synthesis-jobs, 4 concurrent dispatches
scripts/deploy_synthesis_worker_prod.sh     # GPU worker, max 2 instances; grants the invoker
```

The worker script prints the worker URL and **five lines for
`env/prod.env`**; keep them for phase 5.

Check:

- [ ] The worker started and both MCP processes became ready:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="sightsinger-synthesis-worker" AND "mcp_start_ready"' \
  --project=sightsinger-app --limit=5 --format='value(timestamp,jsonPayload.message)'
```

- [ ] Users are unaffected: nothing sends work to the worker yet.

Rollback: delete the worker service, which frees its GPUs; the API is untouched.

```bash
gcloud run services delete sightsinger-synthesis-worker --project=sightsinger-app --region=us-east4
```

## 5. Send synthesis to the worker, with the API still on GPU

Add the five lines from phase 4 to `env/prod.env`:

```
SYNTHESIS_DISPATCH_MODE=cloud_tasks
SYNTHESIS_WORKER_URL=https://sightsinger-synthesis-worker-373752827978.us-east4.run.app
SYNTHESIS_WORKER_AUDIENCE=https://sightsinger-synthesis-worker-373752827978.us-east4.run.app
SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT=sightsinger-preprocess-invoker@sightsinger-app.iam.gserviceaccount.com
SYNTHESIS_TASKS_QUEUE=projects/sightsinger-app/locations/us-east4/queues/synthesis-jobs
```

Then redeploy the API with the same limits:

```bash
BACKEND_CONCURRENCY=10 BACKEND_MAX_INSTANCES=1 scripts/deploy_backend_prod.sh
gcloud run services update-traffic sightsinger-api --project=sightsinger-app --region=us-east4 --to-latest
```

Check, with one take in the app (a quote, confirm, then playback):

- [ ] The API handed it to the queue: `synthesis_job_dispatched … mode=cloud_tasks`.
- [ ] The worker claimed and rendered it: `synthesis_worker_claim … outcome=claimed attempt=1`,
      then `mcp_tool_call tool=synthesize worker=gpu`.
- [ ] Credits settled once, as quoted: `synthesis_billing_settled` on the worker.
- [ ] The audio plays, and the instrumental MIDI loads (`instrumental_midi_restored` on the API).
- [ ] The API itself rendered nothing (no `tool=synthesize` in its logs since the deploy).

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND ("synthesis_job_dispatched" OR "synthesis_worker_claim" OR "synthesis_billing_settled" OR "synthesis_interrupted_by_shutdown")' \
  --project=sightsinger-app --freshness=30m --format='value(timestamp,resource.labels.service_name,jsonPayload.message)'
```

Rollback: remove the five lines from `env/prod.env` and redeploy as above,
or route back without a deploy:
`scripts/rollback_backend_to_revision.sh <phase 3 revision>`. Takes already
queued still finish on the worker.

## 6. Move the API to CPU

Deploy the CPU revision **with no traffic**, check it on its own URL, then
send everyone to it.

```bash
scripts/deploy_backend_cpu_prod.sh          # DEPLOY_TRAFFIC=none is the default
```

It prints the serving revision (the rollback target), the tagged URL, and the
command that sends all traffic. Check the new revision first:

- [ ] `curl -s https://cpu---sightsinger-api-373752827978.us-east4.run.app/readyz`
      answers `ready: true`, with `status` `on_demand` (MCP starts on the first tool call).
- [ ] Its startup logs show no configuration errors:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="sightsinger-api" AND resource.labels.revision_name="<new revision>" AND severity>=WARNING' \
  --project=sightsinger-app --freshness=30m --format='value(timestamp,jsonPayload.message)'
```

Then send all traffic to it, which also makes the service follow latest again:

```bash
gcloud run services update-traffic sightsinger-api --project=sightsinger-app --region=us-east4 --to-latest
```

Check in the app, as a user would:

- [ ] Upload a score; split a part; add solfège; sing a part; play it back.
- [ ] Two users at once (two browsers): both takes finish, each charged once.
- [ ] No `ERROR` on the API (the CPU API logs `mcp_start_ready name=mcp_cpu` and
      `mcp_cpu_2` on its first tool call).
- [ ] Over the next hours, API latency and CPU in Cloud Monitoring. Raise
      concurrency only if instances are mostly idle:
      `BACKEND_CPU_CONCURRENCY=20 DEPLOY_TRAFFIC=all scripts/deploy_backend_cpu_prod.sh`.

Rollback (no build, no deploy): `scripts/rollback_backend_to_revision.sh <phase 5 revision>`.
That revision keeps its own GPU configuration and sends synthesis to the worker.
It pins traffic; return to "follow latest" with `update-traffic --to-latest`
after the next good deploy.

## 7. Final limits

Once the CPU API has run well for a day or two, give the worker the third GPU
and the queue its final dispatch limit:

```bash
SYNTHESIS_WORKER_MAX_INSTANCES=3 scripts/deploy_synthesis_worker_prod.sh
SYNTHESIS_QUEUE_MAX_CONCURRENT_DISPATCHES=6 scripts/setup_synthesis_tasks_prod.sh
```

**After this, rolling the API back to a GPU revision exceeds the GPU quota.**
Lower the worker to 2 first (`SYNTHESIS_WORKER_MAX_INSTANCES=2
scripts/deploy_synthesis_worker_prod.sh`), then roll back.

## Behaviour to expect

| Situation | What happens |
|---|---|
| A worker instance stops mid-take (scale-in, reclaim, deploy) | The take goes back to the queue with its credits reserved and runs again on another instance (`synthesis_interrupted_by_shutdown`, then a claim with `attempt=2`). If its third attempt is interrupted too, it fails and the credits are released. |
| Two takes reach one worker instance | Both are accepted; renders run one at a time on its GPU, so the second waits for the first. |
| The queue is full | The take waits in Cloud Tasks with its "getting ready" status until a worker is free. |
| The worker service is down for good (all ~10 task retries, ~15 min, fail) | **Known gap:** the job stays queued and its credits stay reserved; there is no sweep yet. Check for stuck jobs if the worker is ever unavailable for long. |

## Not part of this rollout

- The frontend change that backs progress polling off to every 5 s ships with
  the next `scripts/deploy_frontend_prod.sh`, at any time.
- The daily analytics report's synthesis-worker section (SIG-110) is added
  after this rollout.
- A sweep that fails stuck jobs and releases their credits; a cross-instance
  per-session chat lock; the export mix.
