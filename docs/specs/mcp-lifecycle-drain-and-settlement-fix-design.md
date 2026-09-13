# MCP Lifecycle: Atomic Draining, Startup Wakeup, and Honest Settlement

Date: 2026-09-13

Status: **Implemented locally on 2026-09-13. Production activation remains deferred.**

This specification addresses the three findings from the latest local code review. It supplements [the follow-up design](mcp-lifecycle-readiness-follow-up-fix-design.md) and supersedes its implementation details only for the behaviors described below.

## 1. Scope and Intended Outcome

| Finding | Severity | Required outcome |
| --- | --- | --- |
| Shutdown races worker spawning and request admission. | P1 | Draining has a synchronized boundary. Once it is established, no new worker spawn or MCP request can be admitted. Successful cleanup cannot precede an already-authorized spawn. |
| Requests waiting for initial startup do not wake on shutdown. | P2 | Waiting requests promptly receive the typed shutdown error without waiting for discovery to finish. The startup completion future still describes actual startup completion. |
| Pending background jobs can be reported as successful shutdown. | P2 | Pending synthesis/preprocess tasks make the overall shutdown result incomplete. |

Keep the dedicated shutdown executor, existing worker recovery policy, HTTP error contracts, and configured shutdown budgets. This is not a new lifecycle framework.

Out of scope:

- Production Docker entrypoint changes and Cloud Run startup/readiness probe activation. These remain deferred until local integration testing and separate approval.
- Credit accounting changes, durable job migration, or guarantees that external persistence finishes during termination.
- New configuration knobs, changes to GPU inference, or additional readiness checks.
- Unrelated findings, refactoring, or changes to customer-facing workflows.

## 2. Finding 1: Establish an Atomic Drain Boundary

### Problem

`McpProcess.start()` and `_acquire_ready_request()` use the shared `_start_gate` for their shutdown check and admission decision. `McpRouter.begin_shutdown()` currently sets `_stopping` without taking that gate.

An operation can pass its shutdown check, pause, and resume after `begin_shutdown()` or even `router.stop()` returns. A recovery thread is particularly important: it is not the router's initial startup thread, so joining initial startup does not close this race.

### Two Distinct Milestones

1. **Shutdown requested:** `_stopping` is set. Readiness becomes unhealthy and waiting callers can fail promptly. This is an early rejection signal, not proof that all concurrent admission decisions have finished.
2. **Draining established:** the shutdown controller has acquired the same gate used by startup and request admission, then recorded that the barrier has been crossed. Every operation that previously passed its shutdown check has finished its protected admission/publication section.

Use a separate router-owned `_drain_established` event or equivalent protected flag. Do not use `_stopping.is_set()` as evidence that milestone 2 has occurred.

The coordinator's existing draining future represents milestone 2. `mcp_router_draining` must also describe milestone 2, not merely shutdown intent. The public readiness response may continue reporting `draining` from the earlier intent signal; that conservative response is not cleanup evidence.

### Minimal Synchronization Change

Retain the existing shared `_start_gate`, including its protection of `Popen` and child-handle publication. Do not move process creation outside the gate in this patch: doing so would require an additional reservation barrier that this small fix does not need.

The protected sections are:

| Operation | Work protected by `_start_gate` |
| --- | --- |
| Worker start or replacement | Shutdown check, `STARTING` reservation, process creation, and publication of the child handle or spawn-failure outcome. |
| Request admission | Shutdown check and capture of a valid ready child/generation under the worker condition. |
| Establish draining | Cross the same gate, confirm shutdown intent, and publish the established-drain flag. |

Process creation remains outside the worker condition. Discovery, pipe I/O, process waits, termination, and thread joins remain outside the spawn gate. Keep logging out of the new drain critical section.

This intentionally distinguishes admission from physical execution: a request admitted before the boundary may write to its captured child during the existing HTTP grace period. It must not acquire a new generation or trigger replacement after shutdown intent.

### `begin_shutdown(deadline)` Algorithm

1. Set `_stopping`. Notify startup-admission waiters through the mechanism in section 3, without acquiring the spawn gate first.
2. If draining has already been established, return the shared established state; do not log a second transition.
3. Acquire `_start_gate` with the remaining time to the supplied absolute deadline. An already-expired deadline may permit a nonblocking acquisition, but never a fresh waiting allowance.
4. While holding the gate, recheck the established flag and set it once. Release the gate before notification or logging.
5. Notify worker lifecycle waiters and publish the transition. Return successfully only after the barrier was crossed.

If acquisition times out, retain shutdown intent and raise an explicit lifecycle error identifying failure to establish draining. Do not mark the established flag or settle the coordinator's draining future successfully. Repeated callers must not interpret the intent flag as successful completion.

All added condition/lock acquisition in this shutdown path must be bounded by the same absolute deadline. Do not make a bounded gate acquisition ineffective by following it with an unbounded notification-lock wait. If notification itself cannot complete within budget, expose an incomplete outcome; normal condition ownership remains short and contains no blocking work.

For a standalone call without a supplied deadline, derive one once from the existing worker shutdown budget. `McpRouter.stop()` must compute its effective deadline before calling `begin_shutdown(deadline=deadline)`, rather than calling it without a deadline and starting a fresh timer afterward. The coordinator continues supplying its fixed total deadline and clipped worker deadline.

### Teardown and Failure Rules

- A successful drain barrier guarantees that every earlier `Popen` critical section has either published its child handle or failed. Worker stop therefore cannot miss a child that is still being created behind that barrier.
- Keep concurrent CPU/GPU stop and the join of actual initial startup. Recovery threads cannot create a replacement after the barrier because all paths to `start()` use the same gate and shutdown check.
- If process creation stalls while holding the gate, the controller may time out. Report incomplete draining/cleanup, not successful stop. Python cannot reliably interrupt an arbitrary OS process-creation call.
- A late-returning creation must still publish its handle and follow the existing shutdown-aware failure/teardown path. Never discard that handle or reset the slot to `STOPPED` solely because the controller timed out.
- Preserve the first coordinator deadlines. Do not retry the gate indefinitely, extend HTTP draining, or start a second controller to hide a timeout.
- Failure to establish draining must not be mistaken for full worker cleanup. Attempt cleanup of reachable children within any remaining allowance, preserving the original failure even if that best-effort cleanup succeeds. Do not depend on the saturated default executor.
- Coordinator/server observers remain bounded and must continue through their failure/finally paths when the drain future fails. No observer may change a failure into `backend_shutdown_complete`.

### Fixed Timeline: Recovery Was Paused Before Spawning

| Time | Event | Result |
| --- | --- | --- |
| T0 | Recovery holds the gate, passes its shutdown check, then pauses before reserving `STARTING`. | The operation is inside the protected section; shutdown has not established its boundary. |
| T1 | SIGTERM triggers the controller. | Intent is set; readiness becomes unhealthy; startup waiters are notified. |
| T2 | Controller tries to acquire the gate. | It waits within the original deadline. It cannot claim draining or cleanup completed. |
| T3 | Recovery resumes and completes process publication, then releases the gate. | The child is tracked. Its later shutdown check prevents discovery/readiness from proceeding normally. |
| T4 | Controller acquires the gate and establishes draining. | No subsequent spawn or request admission can pass the gate's shutdown check. |
| T5 | Startup-failure cleanup and controller teardown use existing ownership rules. | Child cleanup is joined rather than duplicated; successful stop requires the child to be reaped. |

Alternative: if recovery remains paused beyond the deadline, T4/T5 are not reported as successful. The result is explicitly incomplete.

For a paused request-admission section, the same ordering applies: it either completes admission before the established boundary or observes shutdown and is rejected. A request cannot be admitted after that boundary.

## 3. Finding 2: Wake Startup Admission Independently of Startup Completion

### Separate the Two Questions

- The startup future answers: **Has the startup owner actually finished, and with what outcome?**
- A startup admission condition answers: **May this caller proceed, should it keep waiting, or must it reject because shutdown started?**

Do not complete or cancel the startup future to wake waiting requests. Do not set `_startup_ready` merely because draining was requested.

### Proposed Mechanism

Add `_startup_changed = threading.Condition(_startup_lock)` to the router, reusing the existing short startup metadata lock. Use this condition only for admission predicates and notifications, not discovery or teardown.

Register a completion callback on the one startup future to notify `_startup_changed` whenever that future finishes, whether successfully or exceptionally. Register outside `_startup_lock`: `add_done_callback()` can execute synchronously if the future is already done. Future settlement must likewise occur outside a lock its callbacks acquire.

`begin_shutdown()` notifies the same condition after setting shutdown intent, before waiting for the spawn gate. This notification does not depend on initial startup exiting or a request-executor slot becoming available.

### `_ensure_started_for_call()` Algorithm

1. Compute one absolute admission deadline using `backend_ready_timeout_seconds`.
2. Obtain the existing shared startup future through `start_background()` without waiting for startup to finish.
3. Under `_startup_changed`, check shutdown first. If requested, raise `McpShuttingDownError`.
4. If the startup future is done, leave the condition and inspect its result without a blocking wait. Preserve existing startup-failure translation.
5. Otherwise calculate the remaining time and wait on the condition. If time is exhausted, raise the existing `McpStartupInProgressError`.
6. Repeat the predicates after every notification or spurious wakeup. After successful startup, recheck shutdown before normal worker admission; the worker admission gate remains the final authority.

Use the condition for both the predicate check and waiting. A shutdown or completion occurring just before the caller starts waiting must not be lost. Condition waits release the lock. Notifications are hints to recheck state, not evidence of successful startup.

Preserve current HTTP behavior: upload/chat map `McpShuttingDownError` to the existing safe 503 response and retry header. A waiting caller has not attempted a tool, so waking it must not trigger recovery or replay.

The blocking lifespan remains an observer of actual startup completion. It does not switch to this request-admission condition. Shutdown still stops the child independently to release blocked discovery.

### Fixed Timeline: Request B Arrives During Deferred Startup

| Time | Event | Result |
| --- | --- | --- |
| T0 | CPU discovery is held; HTTP serving has begun in deferred mode. | Startup future remains pending. |
| T1 | Request B needs an MCP tool. | B waits on `_startup_changed`, without holding a worker I/O lock. |
| T2 | SIGTERM initiates shutdown. | Controller sets intent and notifies B immediately, without waiting for discovery. |
| T3 | B wakes and rechecks intent. | B returns the typed shutdown error, allowing the HTTP handler to return 503 during draining. |
| T4 | Startup is still held. | Its completion future remains pending; B's return did not falsely finish it. |
| T5 | The controller stops the child when HTTP draining ends or its deadline expires. | Discovery unwinds, actual startup finishes, and only then does its completion future settle. |

This makes timely 503 handling possible; it does not promise delivery if the client has disconnected or the process is forcibly killed.

## 4. Finding 3: Propagate Incomplete Task Settlement

### Small Return-Contract Change

Change `Orchestrator.shutdown_tasks(deadline)` from `None` to `bool`, matching the coordinator's existing `_cancel_tasks()` convention:

| Outcome | Return value |
| --- | --- |
| No outstanding tracked tasks | `True` |
| All selected tasks reach a terminal state within the deadline | `True` |
| Any selected task is still pending when time expires | `False` |
| Settlement operation itself raises unexpectedly | Propagate the exception; coordinator records failure. |

Keep the current cancellation and bounded `asyncio.wait` behavior. Normal task cancellation is a terminal state, not a settlement failure. A task that catches cancellation and continues running is still pending. `True` describes termination of tracked tasks, not proof of successful synthesis, credit persistence, or every external side effect.

At an already-expired deadline, issue the existing cancellation requests, inspect remaining tasks without another wait allowance, and return `False` if any are still pending. Preserve the existing incomplete-task count warning. Do not remove live tasks from tracking to manufacture success.

### Coordinator Aggregation

`_run_task_settlement()` must consume the boolean explicitly:

```text
cleanup_complete = essential worker cleanup succeeded
orchestrator_complete = await orchestrator.shutdown_tasks(original_deadline)
export_complete = await cancel_export_tasks(original_deadline)
overall_complete = cleanup_complete AND orchestrator_complete AND export_complete
```

Execute both task settlement phases even when an earlier phase fails. Do not use a short-circuit expression that skips the next cleanup action. Exceptions make that phase incomplete while allowing the remaining bounded phase to run.

Emit `backend_shutdown_complete` only for `overall_complete=True`; otherwise emit `backend_shutdown_incomplete`. Repeated `finish()` observers still share one settlement task and one final outcome. A task finishing after the reported deadline does not retroactively turn the recorded attempt into success.

Update test doubles to return explicit booleans. Do not accept `None` as success merely to preserve mocks with the obsolete contract.

### Fixed Timeline: A Job Does Not Finish After Cancellation

| Time | Event | Result |
| --- | --- | --- |
| T0 | Workers are stopped and essential cleanup succeeds. | The worker phase is complete; whole-application cleanup is not yet complete. |
| T1 | Orchestrator cancels a synthesis task. | The task catches cancellation and remains active while attempting cleanup. |
| T2 | The original total deadline expires. | Orchestrator logs its pending count and returns `False`. |
| T3 | Coordinator evaluates all phase results. | It logs `backend_shutdown_incomplete`, never `backend_shutdown_complete` for this attempt. |

No new delay is added to wait for that job. This fix makes the result accurate; it does not guarantee that the job can finish before process termination.

## 5. Component and Function Change List

| File / component | Functions or state | Planned changes |
| --- | --- | --- |
| `src/backend/mcp_client.py`: router | `__init__()`, `begin_shutdown()`, `stop()` | Add established-drain state; retain early intent; cross the shared gate with a deadline; propagate barrier failure; compute/pass stop deadline before draining. |
| `src/backend/mcp_client.py`: worker | `start()`, `_acquire_ready_request()`, `stop()` | Preserve and verify the synchronized sections and child-publication invariant. Change only what is required for the barrier contract and bounded failure handling; no redesign of recovery. |
| `src/backend/mcp_client.py`: startup admission | `__init__()`, `start_background()`, `_start_background_target()`, `_ensure_started_for_call()`; new private notification helper | Add condition-based admission waiting and completion/shutdown notifications, leaving the actual completion future independent. |
| `src/backend/lifecycle.py` | `_run_shutdown()` | Resolve draining only after the router barrier succeeds; preserve barrier/cleanup failures and remaining-budget cleanup behavior. |
| `src/backend/orchestrator.py` | `shutdown_tasks()` | Return explicit task-settlement completeness and preserve pending-task warnings. |
| `src/backend/lifecycle.py` | `_run_task_settlement()` | Aggregate worker, orchestrator, and export results without skipping cleanup; report an accurate final outcome. |
| `src/backend/server.py` | `_ensure_draining()`, `_serve()` fallback | Verify bounded failure propagation with the real coordinator. Change only if needed to prevent swallowed failure or unbounded observation introduced by the barrier contract. |
| `tests/test_mcp_lifecycle_logs.py` | Race and startup-admission cases | Reproduce pre-reservation/pre-admission pauses; test gate timeout, startup wakeup, and actual completion semantics. |
| `tests/test_backend_server_lifecycle.py` | Coordinator and server cases; `_Orchestrator` double | Return explicit success from the double; test real pending-job outcomes, drain failures, and deferred-startup shutdown. |
| `tests/test_backend_api.py` | Existing startup/shutdown response coverage | Assert an actual waiting request maps shutdown to the existing 503 contract without tool execution. |
| `tests/helpers/mcp_lifecycle_server.py` and worker helper | Existing controlled subprocess harness | Extend only as needed for held discovery, recorded PIDs, and event-controlled shutdown tests. |

No planned production configuration or API route changes. Other mocks are updated only if they directly depend on the changed `shutdown_tasks()` return contract.

## 6. Verification and Acceptance

First add regression tests that reproduce each finding against the current implementation. Use events/barriers rather than sleep-based scheduling. Avoid committing line-number-dependent trace hooks; a controlled lock wrapper or narrow test double can pause the relevant real critical section.

| Test | Required assertions |
| --- | --- |
| Pause recovery after its shutdown check but before `STARTING` reservation. | Intent can be observed, but established draining and successful router stop cannot complete until the protected section releases. No child appears after successful router stop. |
| Pause request admission after its shutdown check but before child/generation capture. | The request either finishes admission before the established barrier or rejects shutdown. It is never admitted after the barrier. |
| Hold process creation, then return a child after intent. | Drain barrier waits within budget; handle is published and reaped before successful cleanup; no late `READY`. |
| Hold gate beyond the deadline. | Bounded failure, intent remains set, no successful draining/essential-cleanup result, and no `backend_shutdown_complete`. Release synthetic blocking work in test cleanup. |
| Repeated shutdown and concurrent stop callers. | One established transition, same coordinator deadlines, no duplicate controller and no deadline refresh. |
| Shutdown while Request B waits for initial startup. | B exits with `McpShuttingDownError` while discovery is still held and the startup future remains pending. The test must not release discovery to make B wake. |
| Startup success/failure, notification before wait, spurious notifications, and timeout. | Correct existing typed outcomes; no lost wakeup, recovery, tool replay, or new timeout allowance. |
| No tracked tasks, cooperative cancellation, and pending synthesis/preprocess tasks. | Explicit `True`, `True`, and `False` settlement results respectively. |
| Pending orchestrator task with successful workers and exports. | Pending count is logged and final outcome is incomplete. Assert absence of the success event. |
| Worker failure, orchestrator exception, or export failure. | Overall incomplete; remaining settlement phases still execute within the original deadline. |
| Several concurrent `finish()` observers. | One settlement operation and final log; cancellation of one observer does not cancel shared cleanup. |
| Real SIGTERM during deferred startup with an HTTP request waiting for MCP. | Waiting request can receive the shutdown 503 before held discovery is released; actual startup later unwinds; parent exits and children are reaped within the configured budget. |
| Existing executor-saturation and blocked-startup integration cases. | Dedicated cleanup remains independent of request threads; no regression in actual child teardown. |

Use real coordinator/router integration for cross-component acceptance, not only immediate-success router doubles. Record parent exit timing and child PIDs; forced harness cleanup must fail the acceptance assertion, not count as successful application cleanup.

Run the focused lifecycle/server/API tests from the prior review, then directly affected orchestrator tests. The previous 34-test pass is a baseline, not evidence that these new races are fixed. No production deployment is part of verification.

## 7. Implementation Sequence

1. Add failing regression cases for the gate race, startup waiter, and false success outcome.
2. Implement the synchronized drain barrier and deadline propagation; verify no late child after successful stop.
3. Add independent startup-admission notifications; verify callers wake without completing startup.
4. Propagate the orchestrator settlement result and update directly affected doubles.
5. Run focused tests and real-process integration, documenting timings and incomplete-path behavior.

Acceptance requires all three findings to be resolved without weakening existing recovery, child ownership, or shutdown deadlines. Production activation remains a separate approval.
