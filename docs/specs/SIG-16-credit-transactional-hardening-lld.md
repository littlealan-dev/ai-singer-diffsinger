# SIG-16 Credit Transactional Hardening LLD

## Purpose

Define the low-level design for hardening the credit reservation and settlement workflow so credit state remains consistent across synthesis job startup, completion, failure, cancellation, and crash recovery.

This document is for design review only. It does not imply that implementation has started.

Related Linear issues:
- `SIG-13`: MCP credit enrichment failure exposure
- `SIG-16`: broader transactional credit hardening

## Problem Summary

The low-level credit mutations in [src/backend/credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py) are already Firestore transactions. That protects atomicity inside each single credit operation.

The inconsistency risk exists in the orchestration around those transactions:
- credits are reserved before synthesis job startup
- job completion is recorded before settlement succeeds
- release/settle failures are collapsed to `False` or `(0, False)`
- there is no visible reconciler for stranded `pending` reservations

This means the system is not transactionally safe end-to-end, even though each individual credit mutation is transactional.

## Design Goals

- No reservation remains stranded because of job startup failure without an explicit recovery path.
- A synthesis job is not reported as completed until settlement succeeds.
- Release and settlement failures are surfaced explicitly, not silently downgraded.
- Recovery is deterministic and idempotent.
- Audit data remains consistent with balance and reservation state.

## Non-Goals

- Changing the user-facing pricing model
- Changing trial-grant rules
- Changing how credit cost is estimated
- Full distributed transaction support across Firestore, storage, and synth execution

## Current Gaps

### Gap 1: reserve before job startup

In [src/backend/orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py), credits are reserved in `_execute_tool_calls(...)` before `_start_synthesis_job(...)`.

Risk:
- if job creation or task scheduling fails after reservation, credits remain `pending`

### Gap 2: completed before settle

In `_run_synthesis_job(...)`, the job is marked `completed` before `settle_credits(...)` is called.

Risk:
- audio is exposed as ready
- reservation may still be `pending`
- user balance may remain unchanged

### Gap 3: ambiguous credit API outcomes

Current credit APIs return:
- `reserve_credits(...) -> bool`
- `release_credits(...) -> bool`
- `settle_credits(...) -> tuple[int, bool]`

Risk:
- caller cannot distinguish business rejection, already-terminal state, and infrastructure failure

### Gap 4: no visible reconciler

Reservations have `expiresAt`, but there is no visible scheduled flow in the backend that reconciles expired `pending` reservations.

Risk:
- reserved credits can remain stuck after crash or worker loss

## Proposed Design

## 1. Reservation State Machine

Reservation document states:
- `pending`
- `settled`
- `released`
- `reconciliation_required`
- `expired`

State rules:
- only `pending` can transition to `settled`
- only `pending` can transition to `released`
- failed settlement or release paths may transition to `reconciliation_required`
- sweeper may transition expired `pending` to `expired` plus a compensating reserved-credit fix

## 2. Explicit Credit Operation Results

Replace ambiguous return values with explicit result objects.

Proposed shapes:

```python
@dataclass(frozen=True)
class ReserveCreditsResult:
    status: Literal[
        "reserved",
        "insufficient_balance",
        "overdrafted",
        "expired",
        "reservation_exists",
        "infra_error",
    ]
    estimated_credits: int


@dataclass(frozen=True)
class SettleCreditsResult:
    status: Literal[
        "settled",
        "reservation_missing",
        "already_settled",
        "already_released",
        "reconciliation_required",
        "infra_error",
    ]
    actual_credits: int
    overdrafted: bool


@dataclass(frozen=True)
class ReleaseCreditsResult:
    status: Literal[
        "released",
        "reservation_missing",
        "already_settled",
        "already_released",
        "reconciliation_required",
        "infra_error",
    ]
```

Why:
- backend callers can branch on exact state
- operational failures can no longer look like normal business outcomes

Decision for implementation:
- treat `reservation_exists` as idempotent success if the existing reservation matches the same `job_id` and `estimated_credits`
- this supports safe retry of reserve paths without double-counting

## 3. Synthesis Workflow Reordering

### Before

1. reserve credits
2. create background job
3. synthesize audio
4. mark job completed
5. settle credits

### After

1. reserve credits
2. create background job
3. if job startup fails, release immediately
4. synthesize audio
5. persist output
6. settle credits
7. only if settlement succeeds, mark job completed and expose final output

If settlement fails:
- do not mark the job completed
- mark job as failed or `credit_reconciliation_required`
- retain explicit recovery metadata on the job
- retain the output file in storage/session state for operational replay, but withhold `audioUrl` from the progress payload until reconciliation succeeds
- log at warning/error with correlation fields

## 4. Startup Compensation

Wrap the section in `_execute_tool_calls(...)` between successful reservation and successful background task creation.

Required behavior:
- if `_start_synthesis_job(...)` raises after reserve succeeded
- call `release_credits(...)`
- if release also fails
  - mark the reservation `reconciliation_required`
  - log a structured error

## 5. Recovery / Reconciliation Job

Add a scheduled reconciler for stale reservations.

Scope:
- scan `credit_reservations` where:
  - `status == "pending"` and `expiresAt < now`
  - `status == "reconciliation_required"`

Actions:
- compare reservation with current user `credits.reserved`
- apply compensating release if still applicable
- write a ledger entry with type `reconcile_release`
- mark reservation terminal:
  - `expired` for TTL expiry
  - `released` if compensation applied

This must be idempotent.

Recommended execution model:
- Firebase Scheduled Function or cron-triggered backend endpoint
- not an in-process background loop tied to API instance lifetime
- not a separate dedicated worker at current scale

## 6. Job State Handling

Extend job status semantics in [src/backend/job_store.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py):
- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `credit_reconciliation_required`

The UI/progress payload should expose this as a terminal error-like state with a user-safe message, for example:
- "Your audio was generated, but billing finalization failed. Please retry later."

This prevents silent exposure of a "completed" result when billing state is unresolved.

Decision for implementation:
- retain generated output files for operational replay and manual recovery
- do not expose `audioUrl` while job status is `credit_reconciliation_required`
- after successful reconciliation, update the job to `completed` and populate `audioUrl`; the existing client progress polling flow will pick up the transition naturally on the next poll

## Data Model Changes

### credit_reservations/{jobId}

Add:

```json
{
  "status": "pending|settled|released|reconciliation_required|expired",
  "lastError": "optional machine-readable code",
  "lastErrorMessage": "optional human-readable summary",
  "reconciliationAttemptedAt": "timestamp",
  "reconciledAt": "timestamp"
}
```

### credit_ledger/{entryId}

Add new `type` values:
- `reconcile_release`
- `reconcile_settle`
- `reservation_expired`

## Logging / Observability

All failure paths should log structured records with:
- `user_id`
- `job_id`
- `session_id` when available
- reservation status before transition
- intended transition
- error class
- error summary

Required logs:
- reserve succeeded
- reserve compensation release failed
- settle failed after synth succeeded
- reconciler repaired reservation
- reconciler could not repair reservation

## API / Function Changes

### credits.py

Change:
- `reserve_credits(...)`
- `settle_credits(...)`
- `release_credits(...)`

to return structured results rather than bare `bool` / tuple.

### orchestrator.py

Change:
- pre-reserve + job-start logic
- job completion ordering
- cancel/failure handling
- failure handling when release/settle returns non-success status

### runbook / docs

Add operational runbook for:
- stranded reservation diagnosis
- reconciliation replay
- user-impact assessment for unresolved billing states

## Failure Handling Matrix

### Reserve rejected due to insufficient balance

Behavior:
- return normal insufficient-balance response
- no job created
- no reservation created

### Reserve succeeded, job startup failed

Behavior:
- attempt immediate release
- if release succeeds, return startup failure only
- if release fails, mark reservation `reconciliation_required`

### Synthesis failed before output persistence

Behavior:
- attempt release
- if release fails, mark `reconciliation_required`

### Output persisted, settlement failed

Behavior:
- do not mark completed
- mark job `credit_reconciliation_required`
- retain output path for ops recovery, but do not expose final success state

### Reserve succeeded, synthesis in progress, worker/server crashed

Behavior:
- reservation remains `pending`
- no synchronous recovery path is available at crash time
- scheduled reconciler detects expired or reconciliation-required reservation
- reconciler releases or repairs reserved-credit state idempotently
- job remains non-completed until reconciliation or explicit operational handling

### Cancelled job

Behavior:
- release pending reservation
- if release fails, mark `reconciliation_required`

## Test Plan

Unit / integration coverage required:

1. reserve succeeds, `_start_synthesis_job(...)` raises, release succeeds
2. reserve succeeds, startup fails, release fails, reservation becomes `reconciliation_required`
3. synth succeeds, settle succeeds, job becomes completed
4. synth succeeds, settle fails, job does not become completed
5. failed settle does not decrement user balance or reserved credits because the Firestore transaction rolls back
6. release on failure returns already-released or already-settled idempotently
7. reconciler repairs expired `pending` reservation
8. repeated reconciler run is idempotent
9. ledger entries are written for reconciliation actions

## Rollout Plan

1. Introduce structured result types and update all callers/tests in the same change set
2. Update orchestrator to branch on explicit statuses
3. Add reconciliation worker
4. Add metrics/logging
5. Backfill or inspect existing `pending` reservations in staging before prod rollout

Note:
- this is a signature-breaking change for `reserve_credits(...)`, `settle_credits(...)`, and `release_credits(...)`
- rollout step 1 is not additive behind old interfaces; caller updates must land atomically

## Remaining Open Questions

None at the design level. Current implementation decisions are:
- `credit_reconciliation_required` jobs do not expose `audioUrl`
- reconciler updates the job to `completed` and sets `audioUrl` after successful reconciliation
- the client discovers this through the existing progress polling path via the status transition `credit_reconciliation_required -> completed`
