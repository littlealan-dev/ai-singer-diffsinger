# MCP Lifecycle Safety and Readiness: Fix Design

Date: 2026-09-13

Status: **Implemented locally on 2026-09-13. Production entrypoint and probe activation remain deferred pending the user's local integration test and separate approval.**

Addresses R1 and R2 in [the local code review](mcp-lifecycle-readiness-code-review.md). This extends the existing uncommitted lifecycle/readiness implementation rather than replacing it.

## 1. Outcome in Plain Language

Two rules make the lifecycle predictable:

1. **When the server starts shutting down, immediately close admission to new MCP work and worker restarts.** Give work already running a short opportunity to finish, then stop the workers within a shared deadline.
2. **When a worker fails, only one caller replaces it.** Other callers wait for that replacement to finish starting, or receive its failure. A replacement must never start while the old worker is still alive.

Readiness becomes healthy only when both required workers have completed their MCP discovery handshakes and are alive. During startup, recovery, or shutdown, it is unhealthy.

This does not guarantee that a long audio generation finishes during shutdown, or that every GPU/model operation will succeed after readiness becomes healthy.

**Production hold:** Do not change the production startup probe to `/readyz`, replace its TCP probe, or deploy lifecycle changes until local integration testing is complete and production activation is separately approved.

## 2. Scope and Current Gaps

| Finding | Current behavior | Required change |
| --- | --- | --- |
| R1, P1 | FastAPI lifespan sets draining only after Uvicorn has waited for active HTTP requests. That wait currently has no configured deadline. | Enter draining at server shutdown initiation, before request draining; apply finite shutdown budgets. |
| R2, P1 | `stop()` clears the process handle before the old child exits. Concurrent recovery can mistake it for a stopped worker and start another. | Retain teardown ownership and the child handle until exit is confirmed; share recovery results by worker generation. |

The background-only synthesis case usually reaches lifespan teardown promptly because it is not an active HTTP request. The pending-HTTP-request case exposes R1. This distinction does not make background synthesis durable across shutdown.

Out of scope: durable job migration, a new queue, changes to credit accounting, automatic retries of timed-out synthesis, full model warmup, unrelated API behavior, billing-service lifecycle changes, and production probe activation.

## 3. Invariants

1. After the router's draining transition completes, no new MCP operation is admitted and no new worker is spawned. Operations admitted before that transition can finish within the remaining budget.
2. At most one live process exists per worker slot, CPU or GPU. A failed or uncertain teardown blocks replacement.
3. One lifecycle owner performs a worker's stop/start sequence; other callers join its outcome.
4. A worker is ready only after its own generation completes discovery and is still alive.
5. A failure from an older generation cannot tear down a newer generation.
6. Success, failure, and shutdown all wake lifecycle waiters. Wakeup is not itself proof of success.
7. All waits have finite deadlines. Notifications and repeated shutdown calls do not reset those deadlines.
8. Termination does not require the request I/O lock; it must be able to interrupt a blocked MCP read.

"Atomic stop" means an indivisible ownership decision and a protected lifecycle transition. It does not mean process termination itself happens instantaneously or cannot be interrupted by the OS.

## 4. R1: Early, Bounded Shutdown

### Server Integration

Add a backend-specific Uvicorn entrypoint in `src/backend/server.py`, running the existing FastAPI app with a `LifecycleServer` subclass. Keep the same app and one router instance; do not create a second router for the shutdown hook.

- Preserve Uvicorn's signal capture, `should_exit`, repeated-signal behavior, and normal connection shutdown.
- A lightweight `handle_exit()` override delegates to Uvicorn, records the first monotonic shutdown timestamp, and schedules an event-loop callback. Do not acquire application locks, wait for children, or perform cleanup inside the signal handler.
- The scheduled callback starts one shared draining task. It invokes `router.begin_shutdown()` off the event loop if acquiring the spawn gate can block.
- `LifecycleServer.shutdown()` awaits that same draining task **before** delegating to Uvicorn's request-drain phase. Programmatic shutdown without a signal must follow the same path.
- Supply a finite `timeout_graceful_shutdown`, clipped to the remaining request-drain budget.
- A `serve()` cleanup fallback covers startup failure or early exit before Uvicorn reaches its usual shutdown method. Cleanup is idempotent and uses the same deadline.
- Retain the lifespan shutdown call as a fallback for tests and other ASGI hosts. Plain `python -m uvicorn` alone does not provide the early-drain guarantee.

Move blocking MCP startup off the event loop when `mcp_startup_blocking` is enabled so signal-driven coordination can run during discovery. The application may still wait for startup completion before serving; only the blocking execution location changes.

The admission guarantee begins at the synchronized draining transition, not at the exact instant the OS delivers SIGTERM. A spawn already admitted under the shared gate may finish publishing its handle; teardown must find and stop it.

### Shutdown Budget

Use one monotonic deadline shared by the server, lifespan, router, and workers. Initial values for local validation:

| Phase | Initial allowance | Behavior |
| --- | --- | --- |
| Active HTTP requests | Up to 3 seconds from shutdown initiation | Existing calls may complete; new MCP calls and recovery are rejected. |
| Worker teardown | Up to 4 seconds, clipped to remaining time | Stop CPU/GPU concurrently; terminate, then kill and reap if necessary. |
| Task settlement and final bookkeeping | Remaining time within 9 seconds total | Let failed/cancelled tasks unwind; log incomplete cleanup rather than waiting indefinitely. |
| Platform margin | Target at least 1 second | Scheduling overhead before the platform's forced termination. |

These are proposed defaults, not measured guarantees. Cloud Run documents a 10-second SIGTERM grace period before SIGKILL in its [container runtime contract](https://docs.cloud.google.com/run/docs/container-contract#instance-shutdown).

Implement every nested wait using the earlier of its phase deadline and the total deadline. Starting another phase must not create a fresh full shutdown budget. Record elapsed time and remaining time in lifecycle logs.

### Cleanup Order

1. Set draining and notify startup/recovery waiters without stopping a healthy in-flight call immediately.
2. Let Uvicorn close admission and wait for existing requests within the request budget.
3. On timeout, allow Uvicorn to cancel pending request tasks. Do not start recovery because cancellation or teardown produces EOF.
4. Stop required MCP workers concurrently. If a recovery thread already owns teardown, join that teardown rather than issuing a competing stop/start.
5. Settle or cancel tracked background work within the remaining deadline. Preserve existing cancellation/error handling and credit-release semantics; do not invent new billing outcomes.
6. Skip optional expired-session disk cleanup during shutdown. It must not delay essential worker termination; retain its normal non-shutdown cleanup paths.

Cancelling an `asyncio.to_thread()` await does not stop its underlying thread. Child termination must unblock MCP I/O, and lifecycle threads need their own finite waits. Integration tests must include actual process exit and executor cleanup, not just successful return from lifespan. No new hard-exit mechanism is proposed; if bounded exit cannot be demonstrated, block rollout and report the residual blocker.

### Example Timeline After the Fix

| Time | Event |
| --- | --- |
| Before shutdown | Request A is waiting for an MCP result. |
| T+0 | SIGTERM initiates server shutdown; the scheduled hook closes MCP admission before Uvicorn waits for A. Readiness state is now draining. |
| T+0 to 3s | A can finish its already-admitted operation. If the worker fails, A cannot trigger a replacement. A later tool call is rejected as shutting down. |
| T+3s at latest | The request-drain allowance expires; pending HTTP tasks are cancelled. |
| Next phase | Workers are stopped/reaped concurrently, interrupting remaining MCP reads. Background tasks unwind within the remaining deadline. |
| Target before T+9s | Application exits after bounded cleanup; incomplete cleanup is explicitly logged. |

The timestamps illustrate the configured budget. They are not a claim that Python can intercept SIGKILL or guarantee completion under an unresponsive OS or runtime.

## 5. R2: One Owner per Worker Recovery

### State and Ownership

Replace the ambiguous combination of `_proc` and `_ready` with explicit per-worker lifecycle state protected by a `threading.Condition` using the state lock:

| State | Meaning | Can admit tool calls? |
| --- | --- | --- |
| `STOPPED` | No child remains; exit/reaping confirmed. | No |
| `STARTING` | One owner is spawning or performing discovery. | No; discovery uses an internal path. |
| `READY` | This generation completed discovery and is alive. | Yes, unless draining |
| `STOPPING` | An owner is terminating/reaping a retained child. | No |
| `FAILED` | Startup, recovery, or teardown failed. A child may still require cleanup. | No |

Maintain a monotonically increasing generation number, the current child handle, lifecycle operation ID/owner, and the completed operation's outcome. A failed teardown retains its handle and failure state; `FAILED` must not be interpreted as permission to spawn.

Typical transitions:

```text
STOPPED -> STARTING -> READY
READY -> STOPPING -> STOPPED -> STARTING -> READY   (recovery)
READY/STARTING -> STOPPING -> STOPPED              (shutdown)
STARTING/STOPPING -> FAILED                       (unsuccessful lifecycle operation)
```

The recovery owner retains its reservation across the intermediate `STOPPED` state. Another caller cannot claim that gap and spawn a second replacement.

### Capture the Failed Generation

At actual MCP request admission, capture the selected child and generation together. Attach that generation to lifecycle-relevant exceptions, including classified GPU tool errors and request timeouts, without changing their existing public error payloads.

Do not read `process.generation` only later inside a router exception handler: it may already describe a replacement. Re-check the generation and readiness after acquiring the request I/O lock so a queued caller cannot write to a stale child.

### Recovery Algorithm

Add a per-worker `recover(failed_generation, deadline)` operation:

1. Under the condition lock, reject draining and inspect the generation and lifecycle operation.
2. If another owner is recovering this failure, join its outcome using a predicate loop and finite deadline.
3. If a newer generation is already ready, reuse it. Do not restart it for an old failure. If it is still starting/recovering, wait for that operation; if it failed, return that outcome.
4. Otherwise claim recovery ownership and mark the old generation `STOPPING`. Release the condition lock before any blocking work.
5. Terminate the old child and confirm it has been reaped. Keep the handle visible until then. If exit cannot be confirmed, record failure, notify waiters, and do not spawn.
6. Re-check draining through the shared spawn gate. Publish one new generation in `STARTING`, then perform discovery outside lifecycle locks.
7. Publish `READY` only if that generation still owns startup, its child is alive, and the router is not draining.
8. Store success or failure, release ownership, and `notify_all()` under the condition lock in every completion path.

Concurrent `start()` calls join startup through discovery; they do not return successfully merely because `_proc` is non-null. Concurrent `stop()` calls join teardown. Shutdown requests supersede replacement startup and wake joiners with the shutdown outcome.

If shutdown arrives during discovery, teardown can terminate that child to interrupt the handshake. The startup owner must not later publish a stale `READY` result. Ownership tokens make that publication check explicit.

### How Request B Is Notified

| Step | Request A / recovery owner | Request B |
| --- | --- | --- |
| 1 | Detects failure on GPU generation 7 and claims recovery. | Also receives a failure associated with generation 7. |
| 2 | Marks generation 7 stopping; waits for its exit. | Finds recovery in progress and calls `condition.wait(remaining_time)`, releasing the state lock. |
| 3 | Confirms exit, starts generation 8, performs discovery. | Remains waiting; the existence of generation 8 alone is insufficient. |
| 4 | Marks generation 8 ready, records success, calls `notify_all()`. | Wakes, reacquires the lock, and checks the recorded outcome and current state. |
| 5 | May retry according to the existing policy. | May retry on generation 8 according to that same policy. |

The condition is an in-process notification, not an HTTP push or a polling call to `/readyz`. Checking the predicate and starting the wait under the same lock prevents a lost notification. Spurious wakeups are handled by checking again.

Failure or shutdown uses the same notification path, but B raises the corresponding error instead of retrying. B timing out stops B's wait; it does not take over recovery or cancel the shared owner. Calls remain serialized by the worker's request I/O lock after recovery.

### Preserve Retry Behavior

| Failure | Behavior after coordinated recovery |
| --- | --- |
| Existing retryable transport/MCP failure | Retry the original call at most once. |
| Classified GPU-health error from `synthesize` | Retry at most once; preserve existing retry metadata. |
| Request timeout | Recover worker health, but do not replay the timed-out operation. |
| Ordinary tool/business error | Return error without recovery/retry. |
| Shutdown or failed recovery | Return the typed error; do not retry. |

### Locking Rules

- Lifecycle state changes and waiter registration occur under the per-worker condition lock; blocking I/O and child waits occur outside it.
- If both the global spawn gate and worker condition lock are needed, acquire the spawn gate first. Never wait for that gate while retaining the condition lock.
- Hold the global gate only for the draining/spawn admission decision and child publication, not through discovery or termination waits.
- Never hold a lifecycle lock while waiting for the request I/O lock. Request admission can briefly check state after taking the I/O lock, then release state protection before I/O.
- Terminate/kill without the I/O lock; close streams after readers have unwound, with a bounded wait and generation-specific handles.
- Notify per-worker waiters after publishing router draining, without nesting locks in the reverse order.

## 6. Readiness Contract

Keep `/healthz` as lightweight process liveness. Keep `/readyz` as the lifecycle readiness endpoint:

- HTTP 200 only when both required workers are `READY`, their children are alive, and the router is not draining.
- HTTP 503 during initial startup, recovery, failed startup/teardown, or draining.
- Preserve existing response keys and typed shutdown HTTP responses, including `Retry-After` behavior. Derive worker booleans from explicit lifecycle state.
- Do not expose customer data or exception internals in readiness responses.

A successful discovery handshake establishes MCP protocol availability, not successful loading of every voicebank or a guaranteed GPU inference result. Full synthesis warmup is a separate design.

The deferred startup probe will gate initial traffic only after production configuration is approved and applied. This design does not claim that an unhealthy `/readyz` automatically removes an already-serving Cloud Run instance from routing. Runtime MCP admission and error handling must remain correct independently of probes.

## 7. Component / Module / Function Change Inventory

Names marked **new** identify modules and interfaces introduced by this implementation. The production-only entries remain deferred.

| Component / module | Function or interface | Implemented or deferred change |
| --- | --- | --- |
| **New** `src/backend/server.py` | `LifecycleServer.handle_exit()`, `_ensure_draining()`, `shutdown()`, `serve()`, `main()` | Preserve signal behavior; schedule early draining; configure bounded request wait; cover startup-abort cleanup; expose a local CLI entrypoint. |
| **New** `src/backend/lifecycle.py` | `ShutdownCoordinator.begin()`, `remaining_seconds()`, `finish()` | Own one timestamp/deadline and idempotent shared cleanup; coordinate server and lifespan without duplicate teardown. Keep this module limited to lifecycle coordination. |
| `src/backend/main.py` | `create_app()` and nested `lifespan()` | Attach coordinator to `app.state`; make blocking startup event-loop responsive; use bounded cleanup fallback; remove optional disk-expiry work from essential shutdown. |
| `src/backend/main.py` | Nested `readyz()` | Preserve HTTP contract; use corrected router lifecycle snapshot. |
| `src/backend/mcp_client.py` | `McpProcess.__init__()`; **new** lifecycle state/operation records | Add condition, generation, ownership, terminal outcome, and deadline-aware waiting. |
| `src/backend/mcp_client.py` | `McpProcess.start()`, `stop()`, `is_ready()` | Join existing operations; retain handle until reaped; require handshake completion; handle interrupted startup. |
| `src/backend/mcp_client.py` | **New** `McpProcess.recover()` and private wait/completion helpers | Own complete replacement operation; share success/failure; reject stale-generation restarts. |
| `src/backend/mcp_client.py` | `McpProcess.call_tool()`, `_send_request()`, `_raise_process_error()`, error classes | Capture attempted generation at admission; propagate it internally; prevent queued writes to stale/non-ready children. Keep discovery possible through a private startup path. |
| `src/backend/mcp_client.py` | `McpRouter._restart_process()`, `_call_with_retry()` | Delegate recovery using the failed generation; preserve existing retry/no-replay rules. |
| `src/backend/mcp_client.py` | `McpRouter.begin_shutdown()`, `stop()` | Notify all worker waiters; join owned teardown; replace unbounded thread joins with remaining-deadline waits. |
| `src/backend/mcp_client.py` | `start()`, `start_background()`, `_ensure_started_for_call()`, `readiness()` | Align startup completion and shutdown wakeups with explicit worker state; prevent a completion notification from implying success. |
| `src/backend/orchestrator.py` | **New** `shutdown_tasks(deadline)` | Bounded settlement/cancellation of tracked synthesis/preprocess tasks; reuse existing task handlers without changing business outcomes. |
| `src/backend/main.py` / lifecycle coordinator | `app.state.export_mix_tasks` cleanup | Include already-tracked export tasks in bounded shutdown settlement, without changing export behavior. |
| `src/backend/config/__init__.py` | `Settings`, `Settings.from_env()` | Define and validate total/request/worker shutdown budgets; retain existing startup timeout settings for normal recovery. |
| `scripts/start_backend_dev.sh`, `scripts/start_backend_dev_fake_llm.sh` | Backend launch commands | Use the lifecycle-aware entrypoint, preserving host/port/log options and interpreter selection. Billing launcher unchanged. |
| `requirements.txt` | Uvicorn dependency constraint | Establish a tested compatibility bound for the adapter. Local reviewed version is 0.40.0; do not assume all versions allowed by `>=0.27.0` have identical lifecycle hooks. |
| `tests/test_mcp_lifecycle_logs.py` | Existing lifecycle tests plus new deterministic concurrency cases | Verify ownership, generations, notifications, startup completion, failed teardown, and shutdown races. |
| `tests/test_backend_api.py` | Lifespan, readiness, shutdown-response tests | Verify HTTP contracts and cleanup fallback remain correct. |
| **New** `tests/test_backend_server_lifecycle.py` | Adapter ordering and real-subprocess SIGTERM tests | Verify early draining and actual bounded exit with pending requests and worker activity. |
| `tests/test_mcp_lifecycle_logs.py` | Controllable in-memory worker fixtures | Support blocked discovery, delayed exit, failed teardown, and generation races without GPU or cloud access. |
| `Dockerfile` | Production `CMD` | **Deferred activation:** production must eventually use the lifecycle-aware entrypoint for R1 to apply there. No change/deployment now. |
| Production Cloud Run configuration | Startup probe | **Deferred:** keep current TCP probe until local integration tests finish and the user approves activation of `/readyz`. |

Implementation should keep helper interfaces private where possible. No standalone recovery service, new external dependency, or distributed synchronization is needed.

## 8. Local Verification Plan

### Deterministic Worker Tests

Use barriers/events and controlled child-process doubles, not timing sleeps as the primary synchronization mechanism.

1. Hold old-child exit; run two recoveries for the same generation. Assert zero replacement spawns until exit is confirmed and exactly one afterward.
2. Hold replacement discovery. Assert all joiners remain waiting and readiness stays 503 until discovery succeeds.
3. Release an old-generation error after the replacement is ready. Assert it cannot stop the replacement.
4. Fail discovery, time out one waiter, and simulate spurious notifications. Assert correct outcomes, no lost wakeups, and no second owner.
5. Fail terminate/kill/reap. Assert the child handle remains tracked, readiness is false, and replacement is blocked.
6. Begin shutdown during old-child wait, spawn admission, and discovery. Assert waiters wake and no spawn is admitted after draining.
7. Call stop concurrently and repeatedly. Assert callers join existing teardown and deadlines do not reset.
8. Assert timeouts do not replay tool calls; ordinary tool errors do not restart workers; existing retry limits/metadata remain unchanged.

### Real Server Integration Tests

Launch the proposed entrypoint in a subprocess on an available local port with controlled protocol workers. Use local/fake session and LLM dependencies. Do not use production credentials, send email, or change live credits.

| Scenario | Required observation |
| --- | --- |
| Startup discovery blocked | `/healthz` responds; `/readyz` is 503 until both worker handshakes finish, then 200. |
| Pending HTTP request + SIGTERM | Draining log/state appears before that request finishes; process exits within the target deadline. |
| MCP EOF after draining | No replacement-start event occurs; request follows shutdown error/cancellation handling. |
| Background synthesis only + SIGTERM | Cleanup starts promptly, worker exits, tracked task settles or cancellation is reported within budget. |
| Deferred and blocking startup + SIGTERM | Startup is interrupted; no late ready publication or orphan child remains. |
| Concurrent recovery + SIGTERM | Existing teardown is joined; replacement does not bypass draining. |
| Slow child exit / hung handshake | Kill/reap path is exercised; waits remain bounded and incomplete cleanup is visible. |
| Repeated signal / programmatic stop | No duplicate teardown or restart; Uvicorn exit semantics remain intact. |

After listener closure, do not depend on a new HTTP connection to query `/readyz`. Verify draining through structured lifecycle events or direct app-state assertions in adapter tests. Check child PIDs and the parent exit status, not only log messages.

Run current regression tests plus the new tests using `.venv310/bin/python -m pytest`. The existing focused suite is a baseline, not proof of these new guarantees:

```sh
.venv310/bin/python -m pytest tests/test_mcp_lifecycle_logs.py tests/test_backend_api.py -q -k 'mcp_lifecycle_logs or healthz or readyz or interrupted_by_shutdown' --maxfail=1
```

After deterministic tests pass, manually run a local real-worker smoke test: readiness transition, synthesis, one controlled worker interruption, and SIGTERM. Actual GPU validation requires a suitable GPU environment; fake-worker tests establish coordination correctness but cannot prove GPU memory behavior.

## 9. Acceptance and Rollout Gates

- [ ] Both P1 reproductions fail against the old behavior and pass against the implementation.
- [ ] Exactly one recovery owns each failed generation; old and replacement children never overlap.
- [ ] Recovery joiners receive success, failure, timeout, and shutdown outcomes correctly.
- [ ] Draining precedes Uvicorn's active-request wait in the supported entrypoint.
- [ ] Real subprocess tests demonstrate bounded parent exit and no surviving MCP children.
- [ ] Readiness and existing retry/error contracts remain intact.
- [ ] Existing cancellation/credit handling is unchanged; any newly discovered persistence bug is reported separately.
- [ ] Local integration results and measured shutdown timings are recorded for user review.
- [ ] Production entrypoint and startup-probe activation remain unapplied until separately approved.

Rollout order: implement locally after approval, run deterministic and real-server tests, complete the user's local integration test, then propose production activation. A healthy local readiness endpoint alone is not evidence that production traffic is gated by it.

## 10. Limitations and Decisions to Validate

- The shutdown budget must be measured with actual workers and cancellation handlers. Hard process termination cannot guarantee final persistence or credit release.
- A handshake proves protocol readiness only. Model-specific initialization can still fail on the first relevant request.
- Generation safety prevents overlapping recovery; it does not establish the root cause of historical GPU OOM incidents.
- Uvicorn lifecycle integration needs explicit compatibility tests; the dependency currently permits a broad version range.
- Recovery failure should remain visible and non-ready rather than entering an uncontrolled restart loop. Automated replacement of an unhealthy instance is outside this local fix.
- If testing discovers separate business-state or deployment issues, report them and obtain approval before extending implementation scope.
