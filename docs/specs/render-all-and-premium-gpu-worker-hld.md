# One-click render-all on a premium GPU worker: high-level design

Status: draft for discussion, 2026-10-16. Nothing here is implemented yet.

## 1. Goal

"One-click render-all": the user renders every vocal part of a song in one request.

* For a compact SATB score whose parts share staves, this includes splitting the shared staves into separate parts first. Adding solfège is optional.
* The parts render **at the same time**, on one more powerful GPU. A 4-part render then takes about as long as one part.
* Paid users only, because the GPU is expensive.

This builds on SIG-105, which moved synthesis to a GPU Cloud Tasks worker and the API to request-billed CPU. A render-all can't run inside a live request, so everything runs as background jobs.

Delivered in two product phases:

| Phase | User experience |
|---|---|
| 1 | Two steps. "Split all parts" → the user reviews the split → "Render all". |
| 2 | One click: split, add solfège if requested, and render every part, with no review in between. It stops only when something needs the user. |

## 2. Where we are today

### 2.1 Services (after SIG-105)

| Service | Region | Hardware | Role |
|---|---|---|---|
| `sightsinger-api` | us-east4 | CPU, 2 vCPU / 4 GiB, request-billed, concurrency 10 | chat, quotes, upload, dispatch, stream |
| `sightsinger-preprocess-worker` | us-east4 | CPU | voice-part splitting (LLM planning plus deterministic transform) |
| `sightsinger-synthesis-worker` | us-east4 | 1× L4, 4 vCPU / 16 GiB, concurrency 2, max 2 (3 after SIG-105 phase 7) | synthesis |
| Cloud Tasks `synthesis-jobs` | us-east4 | — | max 4 concurrent dispatches (6 after phase 7) |

Other locations: Firestore is in `eur3`, and the Storage bucket (voicebanks, audio) is in `us-east1`.

### 2.2 How many takes run at once today

Inside a synthesis worker instance, GPU renders take turns:

```
Cloud Tasks → worker HTTP (concurrency 2) → orchestrator
   async with _gpu_render_lock:              # one render per instance
       mcp_gpu process (1 subprocess)        # one JSON-RPC request at a time (_request_lock)
           mcp_server: for line in stdin     # serial
               ONNX sessions per voice; cache capped by SYNTHESIS_MAX_LOADED_VOICES (default 2)
```

| Limit | Value | Counts |
|---|---|---|
| Queue max concurrent dispatches | 4 | jobs handed to workers and not finished |
| Instances × concurrency | 2 × 2 = 4 | HTTP requests the workers accept |
| **GPU renders at once** | **2** | 1 per instance × 2 instances |

Steps outside the lock can overlap with another take's render on the same instance: instrumental MIDI, voicebank download, save audio, settle credits.

**Routing.** Cloud Run sends a request to an instance with a free slot, or queues it for one, before starting a new instance. So a second take usually lands on the warm instance and waits for the lock; a cold start costs about 40 s. A take never moves between instances once assigned. A take waiting behind a long render can therefore finish after a later take that landed on a fresh instance (head-of-line blocking). With today's short renders (seconds) this is rare.

### 2.3 Measured on the L4 (production, 2026-10-15)

| Step | Time |
|---|---|
| Worker cold start (instance start → `/readyz`) | about 20 s |
| Voicebank download and extract, per voice per cold instance | about 18 s (608 MB) |
| Render, cold models, 13.5 s of audio | 6.8 s |
| Render, warm, 4.45 s of audio | 1.05 s |
| GPU memory held, p99 | about 1 GiB |
| Split (preprocess), LLM planning per job | 50–64 s |

## 3. The premium GPU: NVIDIA RTX PRO 6000 Blackwell on Cloud Run

### 3.1 Quota (read from the project, 2026-10-15)

| Quota | Region | Limit |
|---|---|---|
| L4, no zonal redundancy | us-east4 | 3 (the synthesis worker) |
| **RTX PRO 6000, no zonal redundancy** | **us-central1** | **1 GPU** (1000 milliGPU) |
| Others | — | not set |

More quota is requested through the console. Google says requests for no-zonal-redundancy quota are more likely to be granted, and faster.

### 3.2 Requirements (Cloud Run GPU docs, updated 2026-10-13)

| | RTX PRO 6000 Blackwell | L4 |
|---|---|---|
| GPU memory | 96 GB | 24 GB |
| Minimum CPU / memory | **20 vCPU / 80 GiB** | 4 vCPU / 16 GiB (8 / 32 recommended) |
| GPUs per instance | 1 | 1 |
| Regions | us-central1, europe-west4, asia-southeast1, asia-south2 | us-east4, us-central1, europe-west1/4, … |
| Driver | 580.x (CUDA 13.0) | same |
| Billing | instance-based; can scale to zero | same |

**Compatibility risk.** The GPU image is `nvidia/cuda:12.3.2` with an unpinned `onnxruntime-gpu`. Blackwell needs CUDA 12.8 or newer and an ONNX Runtime build with sm_120 kernels. So the RTX worker needs its own image, which leaves the L4 worker untouched.

### 3.3 Cost

Instance-based billing charges for the instance's whole life, **whatever the GPU utilization**: 1 take or 4 takes cost the same per second. On-demand rates for us-central1 / us-east4 (Tier 1), GPU without zonal redundancy:

| Per instance-hour | L4 worker (4 vCPU / 16 GiB) | RTX (20 vCPU / 80 GiB) |
|---|---|---|
| GPU | $0.67 | $1.31 |
| CPU ($0.000018 per vCPU-s) | $0.26 | $1.30 |
| Memory ($0.000002 per GiB-s) | $0.12 | $0.58 |
| **Total** | **≈ $1.05** | **≈ $3.19 (≈ 3×)** |

* **Parallelism is what pays for the RTX.** 4 parts in parallel take about 1 minute of instance time (≈ $0.05). The same 4 parts one after another take about 4 minutes (≈ $0.21).
* **The idle tail dominates occasional use.** After the last take, Cloud Run keeps the instance for a while: about 5 minutes on the L4 worker in production, which is ≈ $0.27 on the RTX. An occasional render-all costs roughly $0.30–0.40, mostly startup and idle tail.
* **Keeping it warm** (min 1 instance) costs about $2,300/month.

## 4. Hosting options considered

| Option | Several takes on one GPU? | Scale to zero | Verdict |
|---|---|---|---|
| **Cloud Run service + Cloud Tasks** (today) | Only inside the instance (our code) | ✅ automatic | **Chosen** |
| Cloud Run worker pool | Only inside the instance | Manual instance count only. "GPU worker pools cannot be autoscaled": we would write our own scaler and a pull loop | No gain over a service |
| Sidecar containers | No: a GPU attaches to one container only | — | — |
| GKE with GPU time-slicing or MIG | Yes, managed by Kubernetes | Cluster plus node pools; separate Compute Engine GPU quota | Too heavy for now |
| Gemini Enterprise Agent Platform (formerly Vertex AI) online inference | No (only more replicas) | **Preview**. Requests to a scaled-down model get a 429 and are **dropped**; idle scale-down at least 5 min | Still needs our queue and retries; separate quota |
| NVIDIA Triton / Vertex endpoints | Per model only | — | Our take is a multi-stage Python pipeline: a rewrite |

**Key fact:** every managed unit (service instance, worker pool instance, job task, inference replica) gets a whole GPU. With a quota of 1 GPU, parallelism must happen **inside the one instance**, whichever product runs it.

## 5. Premium synthesis worker

### 5.1 Target

```
API (us-east4, CPU)
  dispatch a synthesis job:
    if premium routing applies to this user:             # test: allowedEmails; later: paid plan
        queue synthesis-jobs-premium (us-central1) → sightsinger-synthesis-worker-premium
    else:
        queue synthesis-jobs (us-east4) → sightsinger-synthesis-worker (L4)

premium worker (us-central1, RTX PRO 6000, 20 vCPU / 80 GiB, max 1, min 0, concurrency e.g. 8)
  GPU slots = MCP_GPU_WORKERS (e.g. 4) → asyncio.Semaphore(4)
  mcp_gpu_1 … mcp_gpu_4        # separate processes, each with its own ONNX sessions and voice cache
  SYNTHESIS_MAX_LOADED_VOICES = 4  (per process)
```

The worker code is the same for both services; env settings make it the premium one. Jobs, claims, leases, billing and the stream are unchanged.

### 5.2 In-instance parallelism: process pool vs threads

| | **Process pool (chosen)** | One process, many threads |
|---|---|---|
| How | `MCP_GPU_WORKERS=N` GPU processes | One GPU process running takes in threads (ONNX Runtime's `session.run` is thread-safe and releases the GIL) |
| GPU memory | Each process loads its own copy of its voices | One copy of each voice |
| Isolation | A crash or OOM loses one take | A crash loses all running takes |
| CPU stages (phonemize, numpy) | Parallel across 20 vCPU | Limited by Python's GIL |
| Code | Small: reuses the CPU pool's `McpProcess` lifecycle (`MCP_CPU_WORKERS`: parallel start, least-busy checkout, restart, health), already in production | Bigger: the MCP server reads stdin one request at a time; needs multiplexed requests and thread-safe model caches |

**Semaphore.** A lock lets one task in; a semaphore with N slots lets N in. The 5th take waits until a slot frees, and Python's `asyncio.Semaphore` hands slots to waiters in arrival order. Slots must equal the number of GPU processes.

### 5.3 Code changes (high level)

| # | Where | Change |
|---|---|---|
| 1 | `src/backend/mcp_client.py` | GPU process pool `MCP_GPU_WORKERS` (default 1, so the L4 worker is unchanged): parallel start, least-busy checkout, one-by-one restart. Later: prefer a process that already has the voice loaded. |
| 2 | `src/backend/orchestrator.py` | `_gpu_render_lock` → `asyncio.Semaphore(MCP_GPU_WORKERS)` |
| 3 | `src/backend/config` | `SYNTHESIS_PREMIUM_WORKER_URL`, `…_AUDIENCE`, `…_TASKS_QUEUE`; `SYNTHESIS_PREMIUM_ROUTING = off \| allowlist` (default `off`) |
| 4 | `src/backend/job_dispatch.py` | A second synthesis route, `synthesis_premium`, with its own dispatcher |
| 5 | `_dispatch_synthesis_job` | Choose the route; record `dispatchRoute` on the job and in `synthesis_job_dispatched route=…` |
| 6 | New helper | `is_premium_synthesis_user(email)`: for testing, reads `app_config/maintenance.allowedEmails` whether maintenance is on or off. Shares the parsing now in `main.py`; short cache (about 60 s). Later: the paid-plan check. |
| 7 | `/readyz` | Report each GPU process; ready only when all are up |
| 8 | Optional | `GPU_MEMORY_SAMPLE_SECONDS`: log per-process GPU memory (`nvidia-smi`) during tests; per-minute p99 metrics miss the peak |
| 9 | New Dockerfile variant | Blackwell image: CUDA ≥ 12.8 base, pinned `onnxruntime-gpu` with sm_120 support |
| 10 | Scripts | `build_synthesis_worker_premium_prod.sh`; `deploy_synthesis_worker_premium_prod.sh` (us-central1, `nvidia-rtx-pro-6000`, 20 CPU / 80Gi, concurrency 8, max 1, min 0, `MCP_GPU_WORKERS=4`, `SYNTHESIS_MAX_LOADED_VOICES=4`); `setup_synthesis_premium_tasks_prod.sh` (queue in us-central1, max dispatches = GPU slots) |
| 11 | Tests | Pool checkout and restart; the semaphore admits N; route choice (allowlist on, off, email missing); L4 defaults unchanged |
| 12 | Later (Codex) | Premium worker and queue in the daily analytics report and the error triage |

**Rollback:** `SYNTHESIS_PREMIUM_ROUTING=off` on the API sends every take back to the L4 worker. The premium worker scales to zero on its own.

### 5.4 Deploy and benchmark steps

| Step | What | Who |
|---|---|---|
| 0 | Code changes locally; the harness runs 2 GPU-pool processes on CPU to prove the pool and the routing | Claude |
| 1 | Build the Blackwell image; deploy the premium worker and queue with routing still `off`. Check `/readyz`, the CUDA provider and that there's no "no kernel image" error | user runs, Claude checks |
| 2 | Routing `allowlist`. As the test user, sing one part; confirm it ran on the premium worker and was charged correctly | user, Claude checks logs |
| 3 | **Single take**: the same score, part and voice as on the L4. Compare cold start, voicebank download, model load and each render stage | user, Claude compares |
| 4 | **Parallel**: 1, 2, 4 (then 6 and 8) takes started together. Total time, per-take time, GPU memory peak, CPU | script + Claude |
| 5 | **4 voices**: 4 parts with 4 different voicebanks at once, repeated. Expect no `voice_models_unloaded`; measure the peak (worst case: every process holds all 4 voices) | same |
| 6 | Choose pool size and concurrency from the numbers | together |

The benchmark script prepares N sessions up to the quote as the test user, then sends "yes" to all at once. That's the real path with real billing, so it spends the test account's credits.

## 6. Render-all design

### 6.1 Core idea

A **parent "render-all" job** in Firestore drives today's child jobs (preprocess and synthesis). Each child already has Cloud Tasks dispatch, claim and lease, retries and billing. When a child ends, it enqueues "reconcile the parent". No step needs a live request.

### 6.2 Stages

```
1. QUOTE (chat turn, LLM)
   "render all parts with solfège" → prepare_render_all_quote(parts, solfege, voices…)
   from score_summary / voice_part_signals: which parts need splitting, expected lanes, durations
   → one itemised quote (N vocal takes + instrumental once); the user confirms once
   → reserve the total; create the parent; reconcile()

2. SPLIT (parts that need it) — one at a time: each split writes a new score version
   (later: one preprocess job planning all targets; the planner already returns per-target results)

3. REVIEW — phase 1 always; phase 2 only when the pause rule triggers

4. SOLFÈGE (optional) — add_solfege_lyric_verse for all derived parts: deterministic, about 1 s, one new score version

5. RENDER — start one synthesis child per part at once → premium queue → GPU slots
   the instrumental MIDI is built once and reused by every child

6. FINISH — all done: settle each child, release the rest
           some failed: "partial", charge only the takes made, offer "retry failed parts"
           the takes appear in the multi-track player; mixing stays in the browser
```

### 6.3 Design problems

| Problem | Why it's hard | Proposed answer |
|---|---|---|
| **Score versions** | Splits and solfège create new score versions; a quote is tied to one version today | Score-changing steps run one at a time on the version chain. The render-all quote is tied to the render choices plus "the version this pipeline produces"; each synthesis child is tied to that final version. Note: a part rendered before another part's split finishes is tagged with an older version; the stale-take logic must not flag it |
| **Price before the split** (phase 2) | The derived parts don't exist yet | Estimate from lane signals and the source part's duration (a split lane lasts as long as its staff). If the split gives a different number of parts than quoted, pause and ask; never charge more silently |
| **Consent** | One confirmation for many takes | One quote listing every take; each synthesis child carries the parent's quote, checked by the backend (the same rule as the 2026-10-15 consent fix) |
| **Partial failure** | 3 of 4 parts may succeed | Settle per child; the parent ends `partial`; "retry the failed part" re-runs that child within the original quote |
| **One render-all at a time** | Two runs, or a chat edit during one, fight over score versions | One active render-all per session, guarded in Firestore. Chat score edits are refused while it runs, or cancel it. This also covers the known gap that the per-session chat lock only works within one API instance |
| **Preprocess "review required"** | Planning can finish with warnings or review needed | Phase 1: always review. Phase 2: pause on failure or review-required (rule to decide) |

## 7. Orchestration: custom reconciler vs Cloud Workflows vs durable-execution engines

### 7.1 The problem in its harder form

```
for each part, independently:
    split (only if needed)  — may fail → retry up to k → else the part fails
    solfège (optional)
    render                  — may fail → retry up to k → else the part fails
parts move at their own pace (S may be rendering while B is still splitting)
rules across parts: score-changing steps one at a time; one quote; per-part settlement; pause and cancel
```

The difficulty isn't the number of steps. It's parallel branches with their own retries, plus rules that cut across the branches.

### 7.2 Option A: our own code, as a reconciler

The pattern Kubernetes controllers use. Each part records what it should become and where it is now. One idempotent `reconcile()` decides the next action for every part. Any event triggers it: a child ending, a user click, a sweeper timer.

```
part: {stage: split|solfege|render|done|failed, attempts, child_job_id}

reconcile(render_all):              # Cloud Tasks; Firestore transaction; safe to repeat
    for part in parts:
        if part.child running: continue
        if child failed and attempts < k: restart the same stage; attempts++
        elif child failed:               part.stage = failed
        elif child done:                 part.stage = next stage
        if part needs a score-changing step and the score lock is free: start it (one at a time)
        if part.stage == render and not started: start its synthesis child
    if every part is done or failed: settle / release, finish
```

### 7.3 Option B: Google Cloud Workflows

A managed, durable engine in YAML. Each step calls our internal endpoint and waits for a callback:

```yaml
- parallel:
    for: {value: part, in: ${parts}}
    steps:
      - split:
          try:
            steps:
              - cb: {call: events.create_callback_endpoint, result: cb}
              - start: {call: http.post, args: {url: ${api + "/internal/split"}, body: {part: ${part}, callback: ${cb.url}}}}
              - wait: {call: events.await_callback, args: {callback: ${cb}, timeout: 900}}
          retry: {max_retries: 2, backoff: {initial_delay: 10, multiplier: 2}}
      - render: …same shape…
```

**Facts from Google's docs:**
* Maximum execution 1 year.
* 20 parallel branches or iterations at once; up to 10 branches per parallel step.
* 512 KB of variables per execution; 100,000 steps per execution.
* Callbacks: default timeout 12 h. The caller needs `workflows.callbacks.send`. An early callback waits in its slot; retrying a callback is safe.
* There is no built-in mutex, so "one score-changing step at a time" stays in our endpoints.

### 7.4 Option C: durable-execution engines (Temporal, Restate, DBOS, Inngest)

Workflows as Python code, with replay, retries and timers. They need a separate service (paid SaaS, a self-hosted cluster, or Postgres for DBOS) and always-on worker processes. That's more than this job needs.

### 7.5 Comparison

| | A: reconciler (own code) | B: Cloud Workflows | C: Temporal-class engine |
|---|---|---|---|
| Parallel parts, each with its own retries | Our loop (about 150–250 lines) | ✅ native | ✅ native |
| Durable waiting and timers | Cloud Tasks + Firestore + a sweeper (needed anyway for stuck jobs) | ✅ native | ✅ native |
| Cross-part rules (versions, credits, pause, cancel) | Same code | Still our endpoints; YAML plus Python | Python |
| Where state lives | Firestore only | Workflow execution **plus** Firestore | Engine **plus** Firestore |
| Local testing | ✅ emulators + e2e harness, as today | ❌ no local emulator: a fake runner or a GCP staging project | Possible with a local dev server |
| Visibility | Job docs + logs (+ a small admin view) | ✅ console execution history | ✅ rich UI |
| Edge cases solved by others | Fewer; we test our own | Retries, timeouts, durability | Most |
| Cost and operations | Nothing new | Cheap per step; serverless | Paid SaaS or a cluster; always-on workers |
| Lock-in | None | GCP YAML | Vendor or self-hosted server |

### 7.6 Recommendation (to confirm)

**A, the reconciler**, for render-all:
1. The hard part is our own rules (score versions, credits, review), which every option leaves in our code. Cloud Tasks already provides the dispatch retries.
2. Local testing comes first in this project, and Workflows can't run locally.
3. Firestore stays the single source of truth for the UI, billing and reports.

To keep A from becoming a badly reinvented wheel:
* one idempotent `reconcile()` per parent, inside a Firestore transaction;
* retries capped per stage;
* a sweeper that reconciles any parent with no change for N minutes;
* table-driven tests over part states × child outcomes.

**Revisit B** if orchestration outgrows render-all: several long-lived pipelines, a need for execution-history UI, or many external service calls. **Consider C** only if workflows become central to the product.

## 8. Delivery phases

| Step | Scope |
|---|---|
| A | Premium RTX worker: GPU process pool, test-user routing, benchmarks (section 5) |
| B | Product phase 1: "split all" (a preprocess for each part that needs it, then review) and "render all" for parts that are already single voices. One quote, parallel synthesis children, parent job, progress UI |
| C | Product phase 2: one click. Quote from lane signals; automatic continue after the split; optional solfège; pause rules |
| D | Later: one preprocess job planning all targets (cuts the 50–64 s per part); retry failed parts |

## 9. Open questions

**Premium worker:**
1. Region us-central1 (where the quota is)?
2. Pool size to start: `MCP_GPU_WORKERS=4`, concurrency 8, queue dispatches 4?
3. Route on `allowedEmails` only, or `allowedUids` too? And whether maintenance is on or off?
4. If the premium worker fails: fail the take (preferred, so failures show), or fall back to the L4 queue?
5. OK for the benchmark script to spend the test account's credits on real takes?

**Render-all:**
6. Paid users only on the RTX? Do free users get render-all on the L4 queue (slower), or not at all?
7. Phase 2 pause rule: pause on any review-required split, or only when a lane fails?
8. If the split yields a different number of parts than quoted: always pause and re-quote, or cap the charge at the quoted total?
9. Chat during a render-all: block score edits, or let an edit cancel it?
10. Orchestration: confirm option A (reconciler).

## 10. Sources

* [Cloud Run GPU support for services](https://docs.cloud.google.com/run/docs/configuring/services/gpu)
* [Cloud Run pricing](https://cloud.google.com/run/pricing)
* [About instance autoscaling (Cloud Run)](https://docs.cloud.google.com/run/docs/about-instance-autoscaling)
* [Worker pools: manual scaling](https://docs.cloud.google.com/run/docs/configuring/workerpools/manual-scaling)
* [GPU support for worker pools](https://docs.cloud.google.com/run/docs/configuring/workerpools/gpu)
* [Autoscale worker pools with external metrics (CREMA)](https://docs.cloud.google.com/run/docs/configuring/workerpools/crema-autoscaling)
* [Agent Platform (Vertex AI) inference autoscaling and Scale To Zero](https://docs.cloud.google.com/vertex-ai/docs/predictions/autoscaling)
* [Gemini Enterprise Agent Platform release notes](https://docs.cloud.google.com/gemini-enterprise-agent-platform/release-notes)
* [Workflows quotas and limits](https://docs.cloud.google.com/workflows/quotas)
* [Workflows callbacks](https://docs.cloud.google.com/workflows/docs/creating-callback-endpoints)
