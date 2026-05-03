# SightSinger Billing Credit Refresh Scheduler HLD

## 1. Purpose

Define the high-level design for running SightSinger recurring credit refreshes with Firebase scheduled functions.

The scheduler exists because not every monthly credit refresh maps to a monthly Stripe invoice:

- free-plan users receive recurring monthly free credits
- annual paid users pay yearly but receive service credits monthly
- deferred refreshes must be retried when credits were reserved during the original refresh window

Stripe remains the source of truth for subscription state. The scheduler is responsible for applying app-side credit refreshes when the user's stored billing state says a refresh is due.

## 2. Goals

- Run recurring free-tier credit refreshes.
- Run monthly credit refreshes for annual paid subscriptions.
- Retry skipped refreshes where `credits.reserved > 0`.
- Keep refresh processing idempotent and transaction-safe per user.
- Avoid collection-wide recomputation by querying only due users.
- Keep the scheduler callable locally for testing.

## 3. Non-Goals

- Replace Stripe webhooks.
- Create or cancel Stripe subscriptions.
- Perform plan reconciliation against Stripe for every user on every run.
- Implement usage-based Stripe metering.
- Change the synthesis reservation and settlement workflow.

## 4. Current Implementation Baseline

The refresh business logic already exists in:

- [src/backend/billing_refresh.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/backend/billing_refresh.py)

The core entrypoint is:

```python
run_credit_refresh()
```

The scheduled function entrypoint lives in [src/billing_functions/main.py](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/billing_functions/main.py).

## 5. Proposed Runtime Architecture

Add a Firebase scheduled Cloud Function named:

```text
refreshCredits
```

The function should run on a fixed schedule and call:

```python
src.backend.billing_refresh.run_credit_refresh()
```

The scheduled function should be thin. All business logic should stay in `src/backend/billing_refresh.py` so the same behavior can be tested locally without the Cloud Functions runtime.

High-level flow:

1. Firebase scheduler invokes `refreshCredits`.
2. `refreshCredits` obtains server time.
3. `refreshCredits` calls `run_credit_refresh(now=server_time)`.
4. `run_credit_refresh` queries due users by `billing.nextCreditRefreshAt`.
5. Each due user is processed independently in a Firestore transaction.
6. The scheduled function logs the summary result.

Example result:

```json
{
  "processed": 12,
  "skipped_reserved": 2
}
```

## 6. Schedule Frequency

Recommended production schedule:

```text
Every 2 hours
```

Running every 2 hours gives reasonable retry behavior when a user has reserved credits at the exact monthly refresh time. The refresh logic is idempotent, so repeated runs are safe.

A daily schedule is acceptable for v1 if operational simplicity is preferred, but it means users whose refresh was skipped due to reserved credits may wait up to a day before retry.

## 7. Data Selection

The scheduler must query only users that are due:

```text
users where billing.nextCreditRefreshAt <= now
```

It must not scan every user and recompute eligibility from scratch.

Required user fields:

```text
billing.activePlanKey
billing.billingInterval
billing.creditRefreshAnchor
billing.nextCreditRefreshAt
billing.lastCreditRefreshAt
credits.balance
credits.reserved
credits.monthlyAllowance
```

## 8. Refresh Rules

### 8.1 Free Plan

For users whose effective refresh plan is free:

- grant the free-plan monthly allowance
- set `credits.balance` to the free allowance
- set `credits.monthlyAllowance` to the free allowance
- write grant type `grant_free_monthly`
- advance `billing.nextCreditRefreshAt` to the next monthly refresh date

### 8.2 Annual Paid Plans

For annual paid users:

- Stripe bills annually
- SightSinger refreshes credits monthly
- grant the active paid plan allowance
- write grant type `grant_paid_annual_monthly_refresh`
- advance `billing.nextCreditRefreshAt` to the next monthly refresh date

Examples:

- annual Solo: 30 credits per month
- annual Pro: 120 credits per month

### 8.3 Monthly Paid Plans

Monthly paid plans should normally refresh through Stripe `invoice.paid` webhooks.

The scheduler may act as a repair path only when local billing metadata indicates a paid invoice exists but the credit grant has not been applied yet.

## 9. Reserved Credit Handling

If a due user has:

```text
credits.reserved > 0
```

the scheduler must skip the user.

The scheduler should leave `billing.nextCreditRefreshAt` unchanged so the user remains due and will be retried on the next scheduled run.

This prevents overwriting balances while a generation job is still holding reserved credits.

## 10. Idempotency

Every scheduler-driven grant must write a deterministic ledger entry.

Ledger ID format:

```text
grant_refresh_<userId>_<yyyymmddTHHMMSS_due_at>_<grant_type>
```

Before granting credits, the scheduler checks whether that ledger entry already exists.

If the ledger entry exists, the scheduler must not grant credits again. It may still advance refresh metadata if needed.

## 11. Failure Isolation

The scheduled run must process users independently.

If one user fails:

- log the user ID and exception
- continue processing other due users
- do not abort the entire scheduled run

The current `run_credit_refresh` implementation should be reviewed for this behavior before production deployment. The desired production behavior is partial success with per-user failure logging.

## 12. Security Model

The production scheduler should not depend on a public unauthenticated HTTP endpoint.

Preferred production model:

- Firebase scheduled function calls `run_credit_refresh()` directly inside a trusted backend runtime.
- No end-user token is involved.
- No browser-accessible route is required for normal scheduled execution.

Manual production execution should use Cloud Scheduler's on-demand run operation instead of a public backend refresh endpoint.

Recommended production model:

- keep only the scheduled function for production refreshes
- run the Cloud Scheduler job manually from Google Cloud Console when needed
- or run the job with `gcloud scheduler jobs run`

## 13. Observability

Each scheduled run should log:

- run start timestamp
- run end timestamp
- number of processed users
- number of skipped users due to reserved credits
- number of failed users
- per-user failure details

The function should emit structured logs so Firebase / Cloud Logging can be filtered by:

```text
function=refreshCredits
event=credit_refresh_run
```

## 14. Local Testing Strategy

Local testing should use the same business logic, without requiring a real deployed scheduler.

### 14.1 Manual Production Trigger

After deployment, manually trigger the Cloud Scheduler job from Google Cloud Console or with:

```bash
gcloud scheduler jobs run refreshCredits --location=<location>
```

This dispatches the same scheduled function without exposing a public app API.

### 14.2 Direct Python Invocation

For focused local tests, invoke the function directly against the Firestore emulator:

```bash
set -a
. env/dev.env
. env/local.env
set +a

FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \
GOOGLE_CLOUD_PROJECT=sightsinger-app \
../ai-singer-diffsinger/.venv310/bin/python -c "from src.backend.billing_refresh import run_credit_refresh; print(run_credit_refresh())"
```

### 14.3 Test Data Setup

Create or modify local test users so:

```text
billing.nextCreditRefreshAt = timestamp in the past
credits.reserved = 0
```

Then run the refresh.

Expected verification:

- `credits.balance` equals the plan allowance
- `credits.monthlyAllowance` equals the plan allowance
- `credits.lastGrantType` is set correctly
- `billing.lastCreditRefreshAt` is updated
- `billing.nextCreditRefreshAt` moves to the next monthly date
- one deterministic `credit_ledger` entry is created

### 14.4 Reserved Credit Test

Set:

```text
credits.reserved = 1
```

Expected result:

- user is counted as skipped
- `credits.balance` is unchanged
- `billing.nextCreditRefreshAt` remains due
- no grant ledger is created

Then set:

```text
credits.reserved = 0
```

Run the scheduler again. The refresh should apply.

## 15. Deployment Notes

The scheduled function should run with the same Firebase Admin / Firestore credentials used by backend billing code.

Required deployment work:

- add Firebase scheduled function wrapper for `refreshCredits`
- configure schedule frequency
- configure region
- ensure production environment variables are available to the function runtime
- verify Firestore indexes support the due-user query
- verify Cloud Scheduler manual run access for operators

## 16. Open Questions

1. Should the scheduler include Stripe reconciliation for annual subscriptions, or only trust local billing state maintained by webhooks?
2. Should cancellation immediately downgrade `credits.monthlyAllowance` to free allowance, or preserve current credits until the next scheduled free refresh?
