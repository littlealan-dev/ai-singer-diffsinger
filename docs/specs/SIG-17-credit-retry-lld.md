# SIG-17 Inline Credit Retry LLD

## Purpose

Define the low-level design for `SIG-17`: add an inline retry layer for transient credit reserve / atomic complete-and-settle / release failures.

This document is for design review only. It does not imply that implementation has started.

Related Linear issues:
- `SIG-16`: transactional credit hardening
- `SIG-17`: inline retry mechanism for credit settle/release operations

## Problem Summary

After `SIG-16`, the billing flow is structurally correct:
- reserve before synthesis
- settle before complete
- release on startup/cancel/failure
- fail the job and release credits when billing finalization is unresolved and the user cannot receive audio

The remaining gap is transient infrastructure failure handling.

Today, the first transient Firestore failure in any of these paths immediately degrades the job into a terminal billing-error path:
- `reserve_credits(...)`
- `settle_credits_and_complete_job(...)`
- `release_credits(...)`

That is safe, but pessimistic. Most transient failures should be retried inline before the system gives up and defers to reconciliation.

## Design Goals

- Retry transient billing infrastructure failures inline before falling back to terminal failure handling.
- Keep existing terminal semantics from `SIG-16`.
- Avoid changing the Firestore transaction logic in [credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py).
- Keep retry behavior explicit and configurable from environment settings.
- Preserve deterministic fallback behavior after retry exhaustion.

## Non-Goals

- Replacing the scheduled reconciliation job from `SIG-16`
- Retrying every backend write indiscriminately
- Changing credit pricing or reservation semantics
- Adding jitter, circuit breaking, or distributed rate limiting

## Current Retry Gaps

### Gap 1: reserve failure degrades immediately

In [src/backend/orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py), `_execute_tool_calls(...)` calls `reserve_credits(...)` before synthesis startup. If it returns `infra_error`, the request fails immediately even if the issue is transient.

### Gap 2: complete-and-settle failure degrades immediately

In [src/backend/orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py), `_run_synthesis_job(...)` calls the atomic complete-and-settle operation once. If it returns `infra_error`, the job falls into immediate billing-error handling even when the failure may be transient.

### Gap 3: release failure degrades immediately

Both release paths are single-shot:
- cancel/failure handling in `_run_synthesis_job(...)`
- startup compensation in `_execute_tool_calls(...)`

If the first release attempt hits a transient Firestore problem, the reservation is marked unresolved immediately.

## Proposed Design

## 1. New Retry Helper Module

Create:
- [src/backend/credit_retry.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credit_retry.py)

Primary helper:

```python
async def retry_credit_op(
    fn,
    *args,
    max_attempts: int = 3,
    base_delay: float = 0.5,
):
    ...
```

Behavior:
- execute `fn(*args)` via `asyncio.to_thread(...)`
- expect the return object to expose a `.status` field
- if `status != "infra_error"`, return immediately
- if `status == "infra_error"` and attempts remain, sleep and retry
- if attempts are exhausted, return the last result

Backoff policy:
- deterministic exponential backoff
- delay for retry attempt `n`:

```python
base_delay * (2 ** (attempt - 1))
```

Examples with base `0.5`:
- retry 1: `0.5s`
- retry 2: `1.0s`

Reasoning:
- simple
- predictable in tests
- good enough for the low-throughput billing path

### Result Contract

The helper is intentionally status-based, not exception-based.

It should be used only with functions that:
- return a result object with `.status`
- convert infrastructure exceptions into `status="infra_error"`

This already matches:
- `reserve_credits(...)`
- `settle_credits_and_complete_job(...)`
- `release_credits(...)`

## 2. Config Externalization

Add to [src/backend/config/__init__.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/__init__.py):

New `Settings` fields:
- `credit_retry_max_attempts: int`
- `credit_retry_base_delay_seconds: float`

Environment variables:
- `CREDIT_RETRY_MAX_ATTEMPTS`
- `CREDIT_RETRY_BASE_DELAY_SECONDS`

Defaults:
- `CREDIT_RETRY_MAX_ATTEMPTS=3`
- `CREDIT_RETRY_BASE_DELAY_SECONDS=0.5`

Loading pattern should match existing waitlist retry configuration:
- `brevo_waitlist_max_attempts`
- `brevo_waitlist_retry_base_delay_seconds`

Environment templates:
- add to [env/dev.env](/Users/alanchan/antigravity/ai-singer-diffsinger/env/dev.env)
- optionally add to [env/prod.env](/Users/alanchan/antigravity/ai-singer-diffsinger/env/prod.env) only if production wants explicit overrides; defaults are acceptable otherwise

## 3. Orchestrator Retry Points

## 3.1 Reserve with retry

In `_execute_tool_calls(...)`, wrap the initial `reserve_credits(...)` call with `retry_credit_op(...)`.

Behavior:
- `reserved` or `reservation_exists`: continue
- `insufficient_balance`, `overdrafted`, `expired`: stop immediately, no retry
- `infra_error`: retry until exhausted, then return the existing billing-setup failure response

## 3.2 Complete-and-settle with retry

In `_run_synthesis_job(...)`, replace the single combined publish call with:

```python
complete_result = await retry_credit_op(
    settle_credits_and_complete_job,
    user_id,
    job_id,
    session_id,
    duration_seconds,
    output_path=output_path,
    audio_url=audio_url,
    max_attempts=self._settings.credit_retry_max_attempts,
    base_delay=self._settings.credit_retry_base_delay_seconds,
)
```

Behavior:
- `completed_and_settled` or `already_completed_and_settled`: continue
- `reservation_missing`, `already_released`, `reconciliation_required`: stop immediately, no retry
- `infra_error`: retry until exhausted, then attempt `release_credits(...)`
- if release succeeds, fail the job and state that no credits were charged
- if release fails, mark the reservation `reconciliation_required` for ops and fail the job with a billing rollback message

Why this is the right retry boundary:
- credits, reservation, ledger, and completed job are committed together
- retry after an ambiguous commit is idempotent because the reservation is no longer `pending`
- the UI never sees `audioUrl` before settled billing

## 3.3 Release with retry in cancel/failure

In `_run_synthesis_job(...)`, replace direct `release_credits(...)` calls with:

```python
release_result = await retry_credit_op(
    release_credits,
    user_id,
    job_id,
    max_attempts=self._settings.credit_retry_max_attempts,
    base_delay=self._settings.credit_retry_base_delay_seconds,
)
```

Behavior:
- `released`, `already_released`, `reservation_missing`: treat as terminal-safe
- `already_settled`, `reconciliation_required`: stop immediately, no retry
- `infra_error`: retry until exhausted, then mark the reservation `reconciliation_required` for ops and keep the user-facing job state terminal (`failed` or `cancelled`)

## 3.4 Release with retry in startup compensation

In `_execute_tool_calls(...)`, replace the single startup-compensation release call with the same retry helper.

Behavior:
- if release eventually succeeds, return the normal startup failure message
- if retry exhausts and release remains unresolved:
  - best-effort mark reservation `reconciliation_required`
  - return the existing stronger user-facing message about billing rollback needing repair

## 4. Logging / Observability

`retry_credit_op(...)` should emit structured logs:
- operation name
- attempt number
- max attempts
- result status
- sleep delay before next retry

Recommended events:
- `credit_retry_attempt`
- `credit_retry_exhausted`
- `credit_retry_succeeded_after_retry`

For the completed-job update wrapper, log separate context because this is not a credit operation:
- `job_update_retry_attempt`
- `job_update_retry_exhausted`

## 5. Failure Matrix

| Case | Scenario | Inline Retry Outcome | Final Fallback |
| --- | --- | --- | --- |
| A | `reserve_credits` transient infra failure | retry reserve up to `N` | return billing-setup failure response |
| B | `settle_credits_and_complete_job` transient infra failure | retry atomic complete-and-settle up to `N` | release reservation; if that fails, mark reservation unresolved for ops and fail job |
| C | `release_credits` transient infra failure | retry release up to `N` | mark reservation unresolved for ops; keep job terminal |
| D | worker crash after reserve / before settle-release | not covered inline | scheduled reconciliation job from `SIG-16` |

## 6. Test Plan

New file:
- [tests/test_credit_retry.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_credit_retry.py)

Required unit tests for helper:
- immediate success: no sleep, 1 attempt
- retry then success: `infra_error`, `infra_error`, `completed_and_settled`
- exhausted retries: always `infra_error`, returns last result
- non-retryable terminal status: `already_completed_and_settled` or `reservation_missing`, returns immediately

Required orchestrator tests:
- reserve single transient failure then success -> synthesis starts normally
- complete-and-settle single transient failure then success -> job completes normally with `audioUrl`
- release single transient failure then success in cancel/failure path -> terminal status remains `failed` or `cancelled`, not `credit_reconciliation_required`
- startup compensation release single transient failure then success -> user sees normal startup failure, not billing repair failure
- settle retry exhaustion with successful release -> job fails and user credits are not held
- settle retry exhaustion with release failure -> reservation becomes `reconciliation_required` for ops, but job still fails

Existing tests to update:
- any monkeypatches that call `settle_credits_and_complete_job(...)` / `release_credits(...)` directly should still return structured result objects

## 7. Rollout Notes

- `credits.py` adds the atomic `settle_credits_and_complete_job(...)` transaction result
- retry helper is additive
- orchestrator logic changes are localized to the existing reserve / complete-and-settle / release call sites
- no data migration required

## Open Questions

None at design level.

The key implementation detail to keep explicit is idempotency: retry must only mutate when the reservation is still `pending`, and must return `already_completed_and_settled` after a successful prior commit.
