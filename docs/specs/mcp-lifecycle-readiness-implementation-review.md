# MCP Lifecycle Implementation Review

Date: 2026-09-13

Scope: local implementation of R1 and R2 from [the original review](mcp-lifecycle-readiness-code-review.md), against [the approved design](mcp-lifecycle-readiness-fix-design.md).

Verdict: **Changes requested. Three high-priority gaps remain.**

The normal shutdown ordering and the original concurrent replacement sequence have improved. However, shutdown can still wait behind active work, and a request arriving during replacement startup can initiate an unnecessary second replacement. These are reproducible implementation defects, not merely missing tests.

This review changed no application code or configuration. Production entrypoint/probe activation remains deferred and is not counted as a defect.

## 1. [P1 / High] Shutdown Coordination Must Not Queue Behind Request Threads

**Locations:** [lifecycle.py:64](../../src/backend/lifecycle.py#L64), [lifecycle.py:95](../../src/backend/lifecycle.py#L95), [server.py:52](../../src/backend/server.py#L52).

`ShutdownCoordinator.begin_async()` schedules the draining transition through `asyncio.to_thread()`. That uses the same default executor as application work, including MCP calls. Worker teardown is also scheduled through that executor, and `finish()` submits another `begin_async()` call even when draining has already begun.

When the executor is occupied by long-running requests or threads waiting for the MCP request lock, the shutdown task cannot run until one of those threads becomes available. `LifecycleServer.shutdown()` awaits this task before closing listeners or applying its request timeout. Thus the new finite request timeout cannot break this wait, and the router remains outside draining while shutdown is pending.

### Example Timeline

1. Request threads occupy every default executor worker. Some are blocked on MCP work.
2. SIGTERM schedules the draining callback on the event loop.
3. The callback submits `begin()` to the already-full executor.
4. Server shutdown waits for that queued task. MCP admission remains open and teardown has not started.
5. The configured shutdown budget expires while the executor is still occupied.

### Reproduction

Used the real `McpRouter` and `ShutdownCoordinator` with a two-thread default executor. Both threads were held on events, modeling occupied request threads. With a 0.15-second total shutdown budget, after 0.2 seconds:

```text
default_executor_occupied: true
after_total_budget_draining: false
begin_still_pending: true
```

Releasing the events allowed draining and cleanup to complete. No cloud service or GPU was used.

### Impact and Recommended Fix

This reintroduces R1 under load: the work that shutdown is meant to interrupt can prevent shutdown coordination from running.

Give lifecycle coordination an execution path independent of the request executor, such as a dedicated lifecycle thread/executor with capacity for essential cleanup. Keep signal handling lightweight. Ensure already-started draining does not require another default-executor submission, and bound the coordination wait using the first shutdown timestamp. Do not hold a coordinator lock across potentially blocking work that event-loop readers also need.

Add tests that saturate the real default executor before initiating shutdown and before worker teardown. Assert that admission closes and worker termination proceeds without releasing the occupied request threads first.

## 2. [P1 / High] A Request Seeing STARTING Can Restart the Healthy Replacement

**Locations:** [mcp_client.py:474](../../src/backend/mcp_client.py#L474), [mcp_client.py:339](../../src/backend/mcp_client.py#L339), [mcp_client.py:1049](../../src/backend/mcp_client.py#L1049).

Request admission raises a generic `McpError` when a worker is not ready and tags it with the current generation. During replacement startup, that is the new generation even though the request has not executed against it. The router treats this admission error as a worker failure and calls recovery.

If the replacement becomes ready before that recovery handler executes, `recover()` sees an error for the current generation, rather than an older generation, and claims another stop/start operation. The existing stale-generation protection therefore does not protect this case.

### Example Timeline

1. Request A recovers failed generation 1 and publishes generation 2 as `STARTING`.
2. Before discovery acquires the request I/O lock, Request B checks admission. It receives `McpError("MCP process is not ready (starting).", generation=2)`.
3. A completes discovery and finishes recovery. Generation 2 is healthy and available for work.
4. B's recovery handler runs with the generation-2 error.
5. B stops healthy generation 2 and starts generation 3. Any operation admitted on generation 2 can be interrupted.

### Reproduction

Used the real `start()`, `call_tool()`, request-admission checks, and `recover()` with controlled child doubles. Paused replacement discovery before it acquired request I/O, obtained B's admission error, completed A's recovery, then handled B's error:

```text
B_error: MCP process is not ready (starting).
B_failed_generation: 2
A_recovered_generation: 2
generation_after_B_recovery: 3
healthy_replacement_killed: true
replacement_count: 2
```

This demonstrates an unnecessary replacement and interruption risk. It does not demonstrate GPU OOM or simultaneous live GPU children.

### Impact and Recommended Fix

This leaves R2 incomplete: recovery is exclusive for concurrent failures associated with the same old generation, but admission errors can incorrectly identify a healthy replacement as failed.

Handle lifecycle unavailability separately from an error returned by an admitted operation. A caller encountering `STARTING` or `STOPPING` should join the relevant lifecycle operation, release the I/O lock while waiting, and revalidate readiness before sending its request. Alternatively, return a typed temporary-unavailability error that the router does not interpret as a worker failure. Preserve generation-tagged recovery only for failures attributable to an actual admitted attempt.

Add a test that delays B's error handling until after A has completed discovery, then asserts there is still only one replacement and that work on it is not interrupted.

## 3. [P1 / High] Blocking Startup Does Not Respond to the Shutdown Deadline

**Locations:** [main.py:237](../../src/backend/main.py#L237), [server.py:35](../../src/backend/server.py#L35), [mcp_client.py:226](../../src/backend/mcp_client.py#L226).

Moving `router.start()` to a thread makes the event loop responsive, but FastAPI lifespan still awaits that thread before yielding. If SIGTERM arrives while discovery is pending, the server hook sets draining but does not stop the startup worker. The in-flight discovery read continues until its normal startup timeout or a response arrives.

Uvicorn is still awaiting application startup, so it has not reached normal server shutdown. The `serve()` cleanup fallback cannot run either, because it is after the still-pending `super().serve()`. The new shutdown deadline does not interrupt this dependency.

### Example Timeline

1. With `MCP_STARTUP_BLOCKING=true`, startup waits for a worker's discovery response.
2. SIGTERM sets the router to draining.
3. The discovery read remains pending. The child is not terminated merely by setting draining.
4. Both normal teardown and the server fallback wait for startup to return.
5. The shutdown budget expires before the normal discovery timeout.

### Reproduction

Used the actual FastAPI app, `LifecycleServer`, router, and a real local child process that reads the discovery request and withholds its response. Invoked the server's SIGTERM handler with a 0.2-second shutdown budget. After 0.35 seconds:

```text
draining: true
server_still_waiting_for_startup: true
startup_child_still_alive: true
```

An explicit test-side `router.stop()` unblocked startup and reaped the child. That intervention was necessary to finish the reproduction. OS signal capture was disabled in this in-process test to avoid signaling the review runner itself.

### Impact and Recommended Fix

Clean shutdown during startup is not complete. This particularly affects local development, where blocking startup is the default, and any environment explicitly enabling it.

Track startup as a lifecycle operation that shutdown can interrupt independently of FastAPI lifespan completion. During startup, arrange bounded worker teardown when draining begins, or make discovery observe a shutdown cancellation signal and relinquish ownership through the coordinated stop path. Account for the fact that cancelling the coroutine awaiting `to_thread()` does not terminate the underlying startup thread or child.

Add a real-subprocess integration test that holds discovery, sends SIGTERM to the parent, and checks parent exit plus child reaping without an extra test-side stop call. Exercise both blocking and deferred startup.

## What Is Working

- Normal server shutdown activates draining before Uvicorn's request wait when the coordination task can run.
- The original concurrent-recovery reproduction now waits for old-child exit and shares one replacement handshake.
- Failures from an older generation reuse a newer ready worker in the tested case.
- An unsuccessful teardown retains the child handle and prevents the immediate replacement in the existing test.
- The readiness endpoint and existing timeout/no-replay behavior pass their focused regression tests.

## Verification and Test Gaps

Re-ran the relevant existing suite: **27 passed, 151 deselected**.

```sh
.venv310/bin/python -m pytest tests/test_mcp_lifecycle_logs.py tests/test_backend_server_lifecycle.py tests/test_backend_api.py -q -k 'mcp_lifecycle_logs or backend_server_lifecycle or healthz or readyz or interrupted_by_shutdown' --maxfail=1
```

The server test substitutes a coordinator whose `begin_async()` sets an event immediately and whose `finish()` returns immediately. It therefore cannot detect default-executor starvation or actual cleanup delays. Recovery tests cover errors already attributed to the old generation, but not a new caller rejected during replacement startup. The previously reported real-worker SIGTERM smoke test starts shutdown after readiness and does not cover blocked startup.

The full API suite was not rerun for this review. The earlier broad-suite failures were not independently classified here. No production queries, deployment changes, or customer data were involved.
