# GPU benchmark for the premium (RTX PRO 6000) synthesis worker

`bench_gpu_pool.py` measures how many takes one GPU renders at once. It makes the
same `synthesize` calls a confirmed take makes, straight into the worker's MCP
router and GPU process pool. No API, LLM, Firestore or credits are involved.

It runs as a Cloud Run job, `sightsinger-gpu-bench` in us-central1, from an image
layered on the production Blackwell image (`Dockerfile.bench`). The production
image itself carries no benchmark code.

## One-time setup

```bash
scripts/build_synthesis_worker_blackwell_prod.sh   # production image (if not built yet)
scripts/bench/build_bench_image.sh                 # bench image, FROM that image's digest
scripts/bench/deploy_bench_job.sh                  # create or update the job
```

Every script accepts `DRY_RUN=1` to print its commands first.

The RTX PRO 6000 quota (one GPU) is shared by the job and the premium worker
service. Run the benchmark while the premium service has no running instance (it
scales to zero when idle), or before it is deployed.

## Runs

Each run is one execution; only the environment changes. `--wait` returns when
the run ends.

```bash
run() { gcloud run jobs execute sightsinger-gpu-bench --project=sightsinger-app \
          --region=us-central1 --wait --update-env-vars="$1"; }
```

| Run | Settings | Answers |
|---|---|---|
| C1 | `run MCP_GPU_WORKERS=1,BENCH_TAKES=1` | single take on the RTX (compare with the L4) |
| C2 | `run MCP_GPU_WORKERS=2,BENCH_TAKES=2` | — |
| C3 | `run MCP_GPU_WORKERS=4,BENCH_TAKES=4` | SATB at once. **Review before going further** |
| C4 | `run MCP_GPU_WORKERS=6,BENCH_TAKES=6` | only if C3 scaled well |
| C5 | `run MCP_GPU_WORKERS=8,BENCH_TAKES=8` | only if C4 scaled well |
| D  | `run "MCP_GPU_WORKERS=4,BENCH_TAKES=4,BENCH_VOICES=Diffsinger LIEE Immortal Idol (JubiLIEE 2025)\,Qixuan_v2.7.0_DiffSinger_OpenUtau\,PM-31_Commercial_Indigo\,PM-31_Commercial_Scarlet"` | four voices at once: GPU memory peak, no `voice_models_unloaded` |

Each run does `BENCH_ROUNDS` rounds (default 2):
- round 1 includes downloading the voicebanks and loading the models;
- round 2 is warm.

The takes sing the Amazing Grace demo: Soprano, Alto, Tenor and Bass, cycled, at 29.4 s each.

For longer takes, `bench/amazing-grace-twice.xml` is the same score with a backward repeat on the
final barline. With `BENCH_SCORE=bench/amazing-grace-twice.xml,BENCH_EXPAND_REPEATS=true` each take
sings verse 1 twice: 58.8 s per part.

## Results

The job prints one line, `bench_result {json}`. It holds, per round:
- wall time;
- each take's time and audio length;
- failures;
- CPU use;
- the GPU memory peak, for the whole device and per process.

To read the last execution's result:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="sightsinger-gpu-bench" AND textPayload:"bench_result"' \
  --project=sightsinger-app --freshness=1h --limit=1 --format='value(textPayload)'
```

How to read it: compare each run's round-2 per-take time with C1's.
- About the same: the GPU has room for more.
- Growing in step with the number of takes: the GPU is saturated, so more processes only add memory.
