# MCP Lifecycle and Readiness Code Review

Date: 2026-09-13

Scope: local uncommitted changes relative to HEAD `4d63c95` in `src/backend/mcp_client.py`, `src/backend/main.py`, `tests/test_mcp_lifecycle_logs.py`, and `tests/test_backend_api.py`.

Verdict: **Changes requested. Two high-priority findings remain.** The changes improve process-handle safety and readiness HTTP responses, but do not yet establish clean shutdown under active traffic or exclusive ownership of worker replacement.

This review changed no application code, tests, or deployment configuration. Production probe configuration is intentionally deferred until local integration testing is complete and is not treated as a defect.

## Findings

### R1: [P1 / High] Activate draining before Uvicorn waits for active requests

**Location:** [main.py:238](../../src/backend/main.py#L238).

`router.begin_shutdown()` runs only when FastAPI receives the lifespan shutdown event. Uvicorn sends that event after waiting for existing connections and request tasks to finish. The current Docker entrypoint does not set a graceful-shutdown timeout; the locally installed Uvicorn defaults to an unlimited wait.

An upload or chat request still waiting on MCP or an external service can therefore prevent the new shutdown guard from activating. During that interval, `_stopping` remains false, deferred startup can continue, and MCP failures can still trigger a worker restart even though server shutdown has started. This is an incomplete part of the intended fix; the underlying Uvicorn waiting behavior predates this patch.

Cloud Run allows 10 seconds between SIGTERM and forced termination. A sufficiently slow in-flight request can consume that entire allowance before the application reaches either `begin_shutdown()` or worker teardown. See the [Cloud Run container runtime contract](https://docs.cloud.google.com/run/docs/container-contract#instance-shutdown).

**Evidence:** A controlled reproduction used the installed `uvicorn.Server.shutdown()` with a pending request task and a lifespan callback invoking the real router's `begin_shutdown()`. It produced:

```text
active_request_pending: True
server_shutdown_pending: True
router_draining_during_server_shutdown: False
graceful_shutdown_timeout: None
router_draining_after_request_completes: True
```

The local implementation of Uvicorn confirms the ordering: `server.py:279` waits for request tasks; `server.py:293` invokes lifespan shutdown. Its `handle_exit()` sets `should_exit` but does not notify the router.

**Impact:** Clean idle shutdown and shutdown during deferred startup do not prove clean shutdown while serving a user. Under active traffic, the instance can still be force-killed before MCP cleanup, and the no-restart guarantee starts too late.

**Recommended fix:** Integrate draining with the beginning of server shutdown, before Uvicorn waits for requests. Use an explicit server lifecycle hook or entrypoint that preserves Uvicorn's signal handling and notifies the router promptly. Keep the signal-side action lightweight; schedule coordination on the event loop rather than performing blocking cleanup inside the signal handler. Add a finite request-drain timeout and reserve time for worker termination and necessary task cleanup within the platform's total shutdown allowance. Retain the lifespan guard as an idempotent fallback. Production configuration changes should remain subject to the user's testing/deployment hold.

**Required verification:** Hold an HTTP request open in an MCP call, send SIGTERM to the server process, and assert that the router enters draining before the request finishes. Trigger MCP EOF during that interval and assert no replacement starts. Verify that the process exits within the shutdown budget and that request cancellation does not leave cleanup waiting indefinitely. Exercise the same ordering with another pending HTTP request alongside deferred worker startup.

### R2: [P1 / High] Keep teardown ownership until the old worker has exited

**Locations:** [mcp_client.py:145](../../src/backend/mcp_client.py#L145), [mcp_client.py:103](../../src/backend/mcp_client.py#L103), and [mcp_client.py:573](../../src/backend/mcp_client.py#L573).

`McpProcess.stop()` sets `_proc = None` under `_state_lock`, then releases the lock before terminating and waiting for the child. This prevents the previous `NoneType` handle race, but also makes a worker that is still being terminated look fully stopped. A concurrent second `stop()` returns immediately, and `start()` can create a replacement while the old child is still alive.

The router's `_restart_process()` does not serialize the whole stop/start operation. The shared `_start_gate` protects spawning against application draining; it does not protect one recovery attempt against another. Concurrent callers can both fail on the same worker, enter recovery, and overlap old and replacement processes. One caller can also finish recovery prematurely because `start()` returns whenever `_proc` exists, even if another caller is still performing that worker's discovery handshake.

**Evidence:** A controlled reproduction used the real `McpProcess.stop()`, `McpProcess.start()`, and `McpRouter._restart_process()` methods. The old child was held inside its `wait()` using an event, and Popen/tool discovery were replaced with in-memory test doubles. While the first stop was still waiting, a second recovery completed with:

```text
old_worker_still_alive: True
replacement_spawned: True
replacement_marked_ready: True
```

The event was then released and both fake children were cleaned up. No GPU or production process was used in this reproduction.

**Impact:** Overlapping GPU workers can compete for memory during recovery, including recovery initiated by an allocation failure. Concurrent recovery may also interrupt a replacement or route a retry before its startup completes. This reproduction establishes the lifecycle race; it does not establish that an OOM occurred.

**Recommended fix:** Maintain per-worker lifecycle ownership across stopping, process exit, and replacement startup. Record a stopping generation and a completion event/condition so concurrent stop callers join the existing teardown and start callers cannot spawn until it completes. Serialize recovery for a failed generation so multiple failures reuse one completed recovery instead of stopping each other's replacements. Preserve the shared draining gate and keep termination independent of the request I/O lock so shutdown can interrupt an in-flight request. Report startup complete only after the selected generation's handshake has finished.

**Required verification:** Pause old-child exit, start two concurrent recovery attempts, and assert that no replacement is spawned until the old child is reaped. Then pause discovery and assert that the second recovery caller cannot proceed as if startup were complete. Test shutdown arriving at each stage and verify no new generation starts afterward. Also assert that router shutdown waits for a teardown already owned by a recovery or startup thread, rather than returning merely because `_proc` has been detached.

## Readiness Assessment

The endpoint now correctly returns HTTP 503 when workers are unavailable or the router is draining, and HTTP 200 after both discovery handshakes complete and both children remain alive. The focused endpoint tests pass.

This is readiness for the MCP protocol and process lifecycle. It does not validate every voicebank/model, preload synthesis resources, or prove that CUDA inference will succeed. That narrower contract matches the implementation described in the preceding discussion; lack of a full synthesis warmup is not counted as a finding here.

The readiness signal also depends on the lifecycle state being accurate. R1 delays the draining state, and R2 permits overlapping generations. Both should be resolved before treating the signal as evidence that lifecycle handling is complete. Cloud Run traffic gating remains a later deployment step by explicit user instruction.

## Validation Performed

- Read the scoped diff, current MCP code, application lifespan, request error handling, session cleanup, background synthesis handling, and installed Uvicorn shutdown implementation.
- Re-ran the focused tests: **21 passed, 151 deselected**.
- Reproduced R1 using the installed Uvicorn shutdown method and a controlled pending task.
- Reproduced R2 using the real lifecycle methods with deterministic in-memory child-process doubles.
- Consulted Google's official shutdown contract for the platform grace period.

Test command:

```sh
.venv310/bin/python -m pytest tests/test_mcp_lifecycle_logs.py tests/test_backend_api.py -q -k 'mcp_lifecycle_logs or healthz or readyz or interrupted_by_shutdown' --maxfail=1
```

The existing atomic-stop test uses a child whose `wait()` returns immediately, so it cannot expose R2. The existing shutdown tests invoke the router directly, so they cannot expose Uvicorn's pre-lifespan wait in R1. Passing these tests therefore does not invalidate either finding.

The broader suite was not re-run for this review. No actual GPU synthesis or production deployment was performed.
