# MCP Lifecycle: Design to Resolve the Three Remaining P1 Findings

Date: 2026-09-13

Status: **Implemented locally on 2026-09-13. Production activation remains deferred.**

Basis: [implementation review](mcp-lifecycle-readiness-implementation-review.md). This document supersedes the shutdown execution, request admission, and startup cancellation portions of [the earlier design](mcp-lifecycle-readiness-fix-design.md). Its other requirements remain applicable.

## 1. Intended Behavior

| Finding | Result after this change |
| --- | --- |
| Shutdown waits behind busy request threads | Shutdown has its own execution capacity. It can close admission and stop MCP workers while request threads remain occupied. |
| Request B unnecessarily restarts a healthy replacement | B waits for replacement startup to complete, then uses it. A waiting/admission timeout does not trigger worker recovery. |
| Shutdown waits for blocking startup | Shutdown can terminate the startup worker directly. The resulting EOF releases discovery and lets startup unwind. |

The central decisions are:

1. One dedicated shutdown controller performs essential synchronous cleanup independently of request execution and FastAPI lifespan completion.
2. Callers distinguish waiting for a usable worker from executing against a worker that fails.
3. Both blocking and deferred application startup use one tracked startup operation whose real completion can be observed without occupying the request executor.

Production entrypoint and Cloud Run probe changes remain deferred until the user's local integration test is complete and production activation is approved. This work does not introduce durable job migration, change credit accounting, or add full model warmup.

## 2. Shutdown Execution and Ownership

### Dedicated Controller

`ShutdownCoordinator` owns a dedicated single-thread executor, started and verified available during lifespan initialization, before MCP startup is dispatched. It accepts only the shutdown control operation. MCP startup, requests, synthesis, storage calls, and job persistence must never be submitted to it.

The control operation executes this sequence:

```text
Publish draining
    -> wait for HTTP drain completion or its deadline, if already serving
    -> stop/reap CPU and GPU workers concurrently
    -> join the actual MCP startup operation within the remaining budget
    -> publish essential cleanup outcome
```

The controller uses the router's existing concurrent child-stop mechanism. It does not submit child cleanup back into its own single-thread executor or the default request executor.

This operation is started by shutdown initiation, not by the later lifespan shutdown callback. Thus worker termination can proceed even if FastAPI is still waiting for startup or Uvicorn has not reached lifespan teardown.

### Coordinator Interfaces

Proposed interfaces in `src/backend/lifecycle.py`:

- `initialize()`: establish the dedicated executor before MCP startup. Initialization must not rely on the default request executor.
- `request_shutdown(started_at, *, serving)`: record the first shutdown timestamp, fixed deadlines, and the one shared control operation. This is called on the event loop, outside the raw signal handler.
- `_run_shutdown()`: execute the synchronous sequence above in the dedicated executor.
- `wait_until_draining()`: await the shared draining result with a deadline; do not submit another `begin()` operation.
- `http_drain_finished()`: set the controller's event so it may advance to worker teardown early.
- `finish()`: join essential cleanup, then settle tracked application tasks within the remaining total deadline.
- `close()`: release executor resources after its control operation has completed; never perform an unbounded executor join on the event loop.

Keep separate results for **draining established**, **essential cleanup completed**, and **full task settlement completed**. Merely recording a deadline does not establish draining, and reaching a deadline does not establish successful cleanup.

Use thread-safe futures for results crossing the controller/event-loop boundary and a `threading.Event` for HTTP-drain completion. Awaiting a wrapped future does not consume a default-executor thread. Await with a bounded, shielded observer so cancelling one waiter cannot cancel the shared control operation.

Coordinator locks protect only short metadata changes. Release them before router calls, waiting on events, child termination, or thread joins. Event-loop properties such as `remaining_seconds()` must not wait on a lock held by blocking cleanup.

### Deadlines

Retain the existing defaults and environment variables:

| Budget | Existing default | Rule |
| --- | --- | --- |
| Total shutdown | 9 seconds | Fixed from the first shutdown signal or programmatic shutdown request. |
| HTTP request drain | 3 seconds | Ends at `min(first_shutdown_time + request_budget, total_deadline)`. |
| Worker teardown | Up to 4 seconds | Starts when HTTP draining ends, or immediately during pre-serving startup; clipped to total time remaining. |
| Task settlement | Remaining total time | Never creates a fresh shutdown allowance. |

If the server has not begun accepting traffic, skip the HTTP-drain wait. If it is serving, preserve the bounded opportunity for admitted requests to finish, including when deferred MCP startup is still pending.

The controller advances to worker teardown when the HTTP-drain event is set **or** the request deadline expires. A missing lifespan callback must not prevent that transition.

Uvicorn's request-wait timeout uses the remaining time to the same absolute request deadline. Account for Uvicorn's own pre-wait work; do not start a fresh three-second timer when entering `shutdown()`.

Acquiring the router spawn gate and joining an existing teardown must also respect deadlines. Preserve synchronized spawn admission, but do not wait indefinitely if the gate is held. If a coordination deadline cannot be met, publish an incomplete outcome and prevent the server adapter from remaining indefinitely in `_ensure_draining()`. No timeout may be logged as successful cleanup.

An OS operation that itself fails to return cannot be made interruptible by wrapping it in an async timeout. This design does not add forced process exit. Such a failure must remain observable and block a claim of fully verified bounded shutdown.

### Server Integration

`LifecycleServer.handle_exit()` continues delegating to Uvicorn, recording the first timestamp, and scheduling an event-loop callback. It performs no locking or blocking cleanup inside the signal handler.

The callback invokes `request_shutdown(..., serving=self.started)`. That decision is taken once: before the server has started, there are no accepted requests requiring a grace period. Once shutdown intent exists, startup must not subsequently open the listener.

`shutdown()` observes draining and delegates normal listener/connection shutdown to Uvicorn with the remaining request budget. Lifespan `finish()` sets the HTTP-drained event before waiting for child cleanup. A server fallback also joins the same cleanup operation for startup failure, programmatic stop, or forced-exit paths.

The fallback must run **inside Uvicorn's signal-capture scope**, before captured signals are restored/re-raised. The current outer `serve()` finally block can run too late on early exit. For the pinned Uvicorn version, use an adapter hook around `_serve()` within inherited `serve()` and verify that ordering explicitly. Preserve Uvicorn's signal restoration and exit status; do not suppress startup exceptions using a `return` in a `finally` block.

### Finding 1: Example Timeline

| Event | Result |
| --- | --- |
| Requests occupy every default-executor thread. | The dedicated shutdown executor is still available. |
| SIGTERM arrives. | The event-loop callback starts the one controller operation. |
| Controller publishes draining. | New MCP admission and replacement spawning are rejected. |
| Existing requests remain blocked through the request deadline. | The controller advances without waiting for a default-executor slot or lifespan callback. |
| Controller stops MCP children. | MCP reads return EOF, and queued callers observe shutdown when they resume. |
| Essential cleanup completes. | Async task settlement consumes only the remaining total time. |

For unrelated external calls still occupying request threads, essential MCP cleanup must still complete independently. This does not promise that cancellation terminates arbitrary storage/network calls already running in threads.

## 3. Admission Without False Worker Failures

### Error Contract

Add `McpWorkerUnavailableError`, a distinct lifecycle/admission error with a safe public message and code `backend_unavailable`. It represents waiting-timeout, unavailable worker state, or unsuccessful shared startup/recovery before the request was admitted.

Catch it before the router's generic `McpError` recovery branch. It must not initiate a new recovery or consume a tool replay attempt. HTTP upload/chat endpoints map it to 503 with the existing temporary-unavailability `Retry-After` convention. Preserve `McpShuttingDownError` for shutdown and existing initial-startup behavior where applicable.

For diagnostics, this error may carry an observed generation, but that value must not be interpreted as a failed attempted generation. Keep the worker generation on genuine transport, tool, and execution timeout errors captured from the actual attempt.

A worker whose `poll()` confirms it died after becoming ready is a genuine worker-health failure, even if the next caller discovers it before sending. Preserve recovery for that case using the verified dead generation. The fix must not disable recovery of idle workers that have exited.

### Admission Algorithm

Add a private `_acquire_ready_request(deadline)` or equivalent helper used by normal tool calls:

1. Compute one absolute call deadline on entry, using the existing worker call timeout. Waiting for lifecycle readiness and the I/O lock consumes this same allowance.
2. Inspect lifecycle state under the worker condition. If shutdown is requested, raise the typed shutdown error.
3. If startup/recovery is in progress, register against its operation/result while holding the condition lock, then wait with the remaining time. Do not hold the request I/O lock while waiting.
4. If the shared operation fails or waiting expires, raise the lifecycle-unavailable error. Do not treat it as a fresh worker failure.
5. When ready, acquire the request I/O lock with the remaining timeout, outside lifecycle locks.
6. Under the spawn/drain gate and worker condition, recheck draining, generation, child identity, readiness, and active recovery ownership. If an operation became active while waiting for I/O, release locks and return to the readiness wait.
7. Atomically admit the request by capturing its child and generation. Release lifecycle locks before writing/reading. Keep the I/O lock until this attempt finishes.

An operation admitted before draining may continue during the request grace period. The synchronized admission decision defines this boundary; physical writing need not occur under the lifecycle lock.

The discovery handshake uses a private startup path that validates the expected generation and operation token and permits `STARTING`. It must not wait for `READY`, since its successful completion is what establishes readiness.

### Notifications and Recovery

Continue using the condition variable and shared recovery results. Register waiters and inspect predicates under the same lock; recheck after notifications and spurious wakeups. Success, failure, and shutdown all notify waiters.

Retain ownership across old-child exit, replacement spawn, and discovery. Publish replacement availability for ordinary callers only once recovery has completed successfully, including release of its ownership reservation. A caller must not observe `READY` during a still-unpublished recovery outcome and assume the operation has completed.

Keep recovery initiated by a genuine old-generation failure separate from admission. A delayed old-generation failure still reuses the healthy replacement. If a completed replacement subsequently dies, that is a new health failure, not an admission-state error.

Preserve the existing policies: one retry for eligible transport/GPU-health failures, no replay of a timed-out tool execution, and no worker restart for ordinary tool/business errors.

### Finding 2: Example Timeline

| Event | Request B's behavior |
| --- | --- |
| A replaces failed generation 1; generation 2 is `STARTING`. | B registers as a waiter and releases the condition lock. It has not attempted generation 2. |
| A completes discovery and publishes recovery success. | B wakes and validates the outcome. |
| B acquires request I/O. | B rechecks that generation 2 is still ready and no shutdown/recovery intervened. |
| B is admitted. | It sends its first attempt to generation 2. No generation 3 is created merely because B previously saw `STARTING`. |
| Alternative: B's waiting deadline expires. | B returns temporary unavailability and leaves A's recovery alone. |
| Alternative: SIGTERM arrives. | B wakes with a shutdown error and does not send a tool call. |

## 4. Startup That Shutdown Can Interrupt

### One Tracked Startup Operation

Use one router-owned startup thread for both startup modes. `McpRouter.start_background()` returns a thread-safe completion future for that operation, reusing it when called again. Protect thread creation with a short lock; do not retain that lock through discovery.

- Blocking mode: lifespan awaits the completion future directly through an async wrapper. It still waits for MCP readiness before serving, but uses no default-executor thread to wait or start workers.
- Deferred mode: lifespan starts the same operation and returns. Readiness remains 503 until the required workers finish startup.
- Shutdown notification: wakes admission waiters, but does not falsely complete the startup future. That future settles only when the startup function actually exits with success, failure, or cancellation.

Keep synchronous `start()` compatible for existing callers, while consolidating internal startup execution and outcome publication in a single owner. Separate any event used to wake callers on shutdown from evidence that the startup thread has completed.

### Interrupting Discovery

When SIGTERM arrives before serving begins, the dedicated controller immediately begins worker teardown; it does not wait for lifespan, the startup future, or the normal discovery timeout.

`McpProcess.stop()` may invalidate startup's publication token, claim teardown, and terminate the tracked child without acquiring the request I/O lock. Termination releases discovery's read. Startup observes shutdown and exits; it cannot publish `READY` or start the next worker.

Fix `_complete_failed_start()` so it claims or joins teardown **before** acting on the child. It currently calls terminate/reap before checking whether another owner already took over. The startup failure path and controller must share one teardown outcome and the same shutdown deadline.

Prevent circular waits:

- Controller stops children first, then joins the startup thread.
- Startup may join its child teardown, but must not await completion of the controller that is joining startup.
- Startup cancellation must not invoke `router.stop()` in a way that joins its own thread.
- Failed startup/recovery must not introduce fresh default cleanup budgets after shutdown has already established one.

Check deadline expiry before spawning and include discovery-timeout calculation in the protected startup failure path. A failure after a child is created must always settle startup and either reap or explicitly retain the child for cleanup.

Diagnostic provider logging must not become a new startup barrier after MCP readiness. Keep it outside required readiness and make any awaiting behavior cancellation-aware; essential shutdown must not wait behind an optional diagnostic submitted to the default executor.

### Finding 3: Example Timeline

| Event | Result |
| --- | --- |
| Blocking lifespan awaits discovery from the CPU child. | Startup future is pending; no listener is serving traffic yet. |
| SIGTERM arrives. | Controller publishes draining and skips the HTTP request wait. |
| Controller claims CPU teardown and terminates the child. | Discovery receives EOF, or kill escalation releases it. |
| Startup unwinds with shutdown cancellation. | GPU startup is not attempted; late readiness publication is rejected. |
| Controller confirms child exit and startup-thread completion. | Its essential cleanup future settles. |
| Lifespan and server fallback run. | Both observe the same cleanup result; neither starts another teardown or extends the deadline. |

For deferred startup after HTTP serving begins, use the normal bounded request grace period before stopping workers. Startup still cannot hold shutdown past the deadline.

## 5. Locking and Resource Rules

- Short state publication uses the worker condition; condition waits release its lock.
- Normal request admission lock order, when nested: request I/O lock, then spawn/drain gate, then worker condition. Never wait for the I/O lock while holding either lifecycle lock.
- Startup/stop/recovery never hold a lifecycle lock across discovery, child waiting, or pipe closing.
- The dedicated controller never waits for a default-executor task to perform essential cleanup.
- Start the shutdown executor before starting MCP; shut it down only after its control operation settles. An incomplete executor operation remains an error, not a reason to block the event loop indefinitely on `shutdown(wait=True)`.
- Repeated signals and multiple cleanup callers share the original futures and deadlines. Cancellation of an observer does not cancel the owner.
- Every retained process handle belongs to a generation and remains tracked until reaped. A timeout never makes the slot appear safely stopped.

## 6. Change List

Function names marked **new** are proposed interfaces. Equivalent private factoring is acceptable if ownership and deadlines remain explicit.

| Module / component | Functions or interfaces | Required changes | Finding |
| --- | --- | --- | --- |
| `src/backend/lifecycle.py` | `ShutdownCoordinator.__init__()`; **new** `initialize()`, `request_shutdown()`, `_run_shutdown()`, `wait_until_draining()`, `http_drain_finished()`, `close()` | Dedicated prestarted shutdown executor, one control operation, separate completion futures, HTTP-drain event, and fixed deadlines. | 1, 3 |
| `src/backend/lifecycle.py` | `begin()`, `begin_async()`, `finish()`, `_run_cleanup()`, `remaining_seconds()` | Remove default-executor dependency for essential control; replace/rework existing begin/cleanup methods around shared outcomes; no locks held across blocking router operations. Retain bounded async task settlement after worker cleanup. | 1, 3 |
| `src/backend/server.py` | `handle_exit()`, `_start_draining()`, `_ensure_draining()`, `shutdown()` | Dispatch the controller once with first timestamp and serving state; bounded draining observation; clip Uvicorn's wait to the original request deadline. | 1, 3 |
| `src/backend/server.py` | `serve()`; **new** `_serve()` integration hook | Put cleanup fallback inside signal capture, cover startup failure and forced exit, and preserve exceptions/exit semantics. Verify against the pinned Uvicorn version. | 3 |
| `src/backend/main.py` | `create_app()`, nested `lifespan()` | Initialize controller before MCP startup; await a real startup-completion future in blocking mode; reuse same operation in deferred mode; join shared cleanup in finally. | 1, 3 |
| `src/backend/main.py` | Upload/chat MCP exception handlers | Map new admission-unavailable error to a safe HTTP 503 with the existing retry-header convention. | 2 |
| `src/backend/mcp_client.py` | **New** `McpWorkerUnavailableError`; `McpProcess.call_tool()`, `_send_request_with_generation()`; **new** `_acquire_ready_request()` | Wait for usability before admission, bound I/O-lock waiting, revalidate generation/state at admission, and separate unavailable state from genuine worker failure. Keep internal discovery path distinct. | 2 |
| `src/backend/mcp_client.py` | `McpRouter._call_with_retry()`, `_restart_process()` | Catch admission errors before generic recovery; recover only evidence-backed worker failures; preserve idle-death recovery and no-replay timeout semantics. | 2 |
| `src/backend/mcp_client.py` | `McpProcess.recover()`, `_wait_for_operation()`, `_wait_for_recovery()` | Reuse operation outcomes for callers waiting for readiness; release ownership before publishing availability; preserve failure/shutdown notification and deadlines. | 2 |
| `src/backend/mcp_client.py` | `McpRouter.start()`, `start_background()`, `_start_background_target()`, `_ensure_started_for_call()` | One startup owner and observable actual completion; short creation lock; avoid using a shutdown wakeup as a startup-completed signal. | 3 |
| `src/backend/mcp_client.py` | `McpRouter.begin_shutdown()`, `stop()`; `McpProcess.start()`, `stop()`, `_complete_failed_start()` | Deadline-aware drain/teardown; stop startup child before joining startup; owner-checked failed-start cleanup; reject late spawn/readiness publication. | 1, 3 |
| `tests/test_mcp_lifecycle_logs.py` | Admission/recovery/startup concurrency cases | Reproduce delayed Request B, lifecycle wait outcomes, real dead-worker recovery, and startup/stop ownership races. | 2, 3 |
| `tests/test_backend_server_lifecycle.py` | Controller and server integration tests | Replace immediate coordinator doubles in critical integration tests with real coordinator/router behavior; test saturation and real SIGTERM during startup. | 1, 3 |
| **New** `tests/helpers/mcp_lifecycle_worker.py` and local server harness | Controlled stdio child and test-only app wiring | Hold discovery/calls, ignore SIGTERM when testing kill escalation, record child lifecycle events/PIDs, and verify parent/child exit without cloud dependencies. | 1, 3 |
| `tests/test_backend_api.py` | Lifespan and unavailable-response tests | Test both startup modes and admission-unavailable HTTP contract; adapt only affected mocks. | 2, 3 |

No new configuration knobs are required. Existing shutdown budgets and worker call/startup timeouts suffice. No planned changes to credit handlers in `orchestrator.py`, the production Docker command, deployment scripts, Cloud Run probes, or the current Uvicorn version constraint. Update only directly affected fixtures if public/internal method signatures change.

## 7. Verification and Acceptance

Tests must exercise the actual shutdown coordinator, router, and server adapter for cross-component behavior. Immediate-success coordinator doubles remain useful only for narrow adapter unit tests.

| Test | Required result |
| --- | --- |
| Saturate default executor before SIGTERM. | Draining and worker stop proceed without first freeing a request-executor slot. |
| Saturate executor after draining but before teardown. | `finish()` and essential cleanup do not queue behind it. |
| Saturate executor with real pending MCP calls/queued callers. | Controller terminates children; calls unwind; parent exits and children are reaped. |
| Hold unrelated executor work. | MCP cleanup completes independently; test then releases its synthetic external work explicitly rather than claiming arbitrary thread cancellation. |
| Repeat shutdown from signal, server, and lifespan. | One controller operation; unchanged first timestamp/deadline. |
| Pause A before replacement discovery; start B. | B waits without holding I/O, and exactly one replacement is spawned. |
| Delay B until A completes, or change generation while B waits for I/O. | B revalidates and never restarts a worker solely because it previously saw `STARTING`. |
| B wait timeout, spurious wakeup, owner failure, or shutdown. | Correct typed outcome; no second recovery; no leaked waiter record. |
| Worker dies while idle after becoming ready. | Verified dead-generation recovery still works. |
| Actual admitted execution times out. | Existing recovery policy applies without replaying the timed-out call. |
| Blocking startup holds CPU discovery; send actual SIGTERM. | Controller stops/reaps child; startup settles; GPU never starts; parent exits within configured budget. |
| Hold GPU discovery or use deferred startup. | Same ownership/deadline guarantees; serving mode receives only its configured request grace period. |
| Child ignores SIGTERM. | Kill escalation and reaping occur within the child/total budget. |
| Shutdown races successful discovery or startup failure. | No late `READY`, duplicate child teardown, orphan child, or circular join. |
| Startup failure / forced-exit fallback. | Cleanup runs inside signal capture before signal restoration/re-raise; error/exit semantics are preserved. |

Use event/barrier-controlled scheduling for race tests and subprocess timeouts for integration tests. A harness may force-clean test processes in a `finally` block, but needing that cleanup must fail the assertion, not count as a passing shutdown test.

Do not depend on new `/readyz` requests after the listener closes. Assert shutdown through lifecycle events and process state. Continue verifying readiness stays 503 until both handshakes complete, becomes 200 when usable, and never becomes healthy after draining.

For deadlines, run shortened-budget cases for rapid race tests plus at least one integration case with the unchanged production-intended defaults. Record actual parent exit time, child PIDs and reaping, and whether forced harness cleanup was necessary. Passing fake-worker tests does not validate GPU inference or guarantee external persistence during termination.

## 8. Implementation Order

1. Add the three regression reproductions as automated tests against the existing implementation; confirm their intended assertions fail.
2. Implement independent shutdown execution and shared absolute deadlines, including server fallback placement.
3. Connect tracked startup and owner-checked teardown to the controller; pass the blocked-startup SIGTERM tests.
4. Implement admission waiting and typed unavailability; pass the delayed-B and idle-death regression tests.
5. Run focused API/lifecycle tests and real-process integration tests, then document timings and any remaining limitations.

Completion requires all three reproduced defects to be resolved together. Production activation remains a separate step after the user's local integration test.
