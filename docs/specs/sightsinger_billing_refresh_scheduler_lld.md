# SightSinger Billing Credit Refresh Scheduler LLD

## 1. Purpose

Define the low-level design for the Firebase scheduled function that runs recurring SightSinger credit refreshes.

This LLD implements the scheduler described in [sightsinger_billing_refresh_scheduler_hld.md](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/docs/specs/sightsinger_billing_refresh_scheduler_hld.md).

The scheduler handles:

- free-plan monthly refreshes
- annual paid plan monthly credit refreshes
- retry of due refreshes skipped because credits were reserved

Stripe remains the source of truth for subscription state. The scheduler applies credits based on the local billing mirror maintained by checkout sync, subscription sync, and Stripe webhooks.

## 2. Runtime Design

Add a Firebase scheduled function named:

```text
refreshCredits
```

The scheduled function is a thin runtime wrapper around:

```python
src.backend.billing_refresh.run_credit_refresh()
```

Business logic remains in `src/backend/billing_refresh.py`; the Firebase function only handles:

- schedule trigger
- environment/config loading
- structured logging
- timeout and batch-size configuration
- returning a run summary

## 3. Schedule

Default production schedule:

```text
Every 2 hours
```

The schedule must be configurable.

Recommended config:

```text
BILLING_REFRESH_SCHEDULE="every 2 hours"
```

If the Firebase scheduler API requires the schedule at deploy time rather than runtime, the deploy configuration should read this value during function definition.

## 4. Batch Limit

Default maximum due users processed per run:

```text
300
```

The batch size must be configurable.

Recommended config:

```text
BILLING_REFRESH_MAX_DUE_USERS=300
```

The scheduler should process at most this many due users per invocation.

If more than 300 users are due, they remain due because their `billing.nextCreditRefreshAt` is still in the past. They will be picked up by the next scheduled run.

## 5. Timeout

Recommended scheduled function timeout:

```text
300 seconds
```

Recommended config:

```text
BILLING_REFRESH_TIMEOUT_SECONDS=300
```

The batch size should be chosen so normal runs complete well inside this timeout.

The scheduler should not implement elapsed-time early stopping for v1. `BILLING_REFRESH_MAX_DUE_USERS` is the bounding mechanism.

## 6. Function Entry Point

Add a scheduled Firebase function wrapper in:

```text
src/billing_functions/main.py
```

Target export:

```text
refreshCredits
```

Expected wrapper behavior:

1. Read scheduler config.
2. Record `startedAt`.
3. Call `run_credit_refresh(now=startedAt, max_users=max_due_users)`.
4. Log structured result.
5. Return normally unless startup/config errors prevent execution.

Example structured log payload:

```json
{
  "event": "billing_credit_refresh_run",
  "processed": 42,
  "skippedReserved": 3,
  "failed": 1,
  "limit": 300,
  "startedAt": "2026-05-02T12:00:00Z",
  "finishedAt": "2026-05-02T12:00:17Z"
}
```

## 7. Backend Refresh API Contract

Update the refresh function signature to support bounded runs:

```python
def run_credit_refresh(
    *,
    now: datetime | None = None,
    max_users: int | None = None,
) -> dict[str, int]:
    ...
```

Expected return shape:

```python
{
    "processed": 0,
    "skipped_reserved": 0,
    "failed": 0,
}
```

Optional useful fields:

```python
{
    "scanned": 0,
    "limit": 300,
    "has_more_due_users": False,
}
```

## 8. Due User Query

The scheduler must query only due users:

```python
db.collection("users")
  .where("billing.nextCreditRefreshAt", "<=", now)
  .order_by("billing.nextCreditRefreshAt")
  .limit(max_users)
```

Ordering by `billing.nextCreditRefreshAt` makes processing deterministic and ensures the oldest due users are handled first.

The scheduler must not scan the full `users` collection.

## 9. Required Firestore Index

The `users` collection requires an index for the due-user scheduler query.

Required datastore:

```text
Collection ID: users
Query scope: Collection
Field: billing.nextCreditRefreshAt
Order: Ascending
```

If Firestore accepts the single-field nested field index automatically, no composite index is needed. However, the index should be explicitly documented and verified before production launch because the scheduler depends on this query.

Expected `firestore.indexes.json` representation if an explicit composite index is required:

```json
{
  "collectionGroup": "users",
  "queryScope": "COLLECTION",
  "fields": [
    {
      "fieldPath": "billing.nextCreditRefreshAt",
      "order": "ASCENDING"
    }
  ]
}
```

If Firestore rejects a single-field composite index entry, rely on the default single-field ascending index and keep the requirement documented as:

```text
Do not disable single-field indexing for users.billing.nextCreditRefreshAt.
```

No index should be created on `credits.balance`, `credits.reserved`, or plan fields for the scheduler path.

## 10. Config Additions

Add scheduler-specific environment variables:

```text
BILLING_REFRESH_SCHEDULE="every 2 hours"
BILLING_REFRESH_MAX_DUE_USERS=300
BILLING_REFRESH_TIMEOUT_SECONDS=300
```

Recommended defaults if unset:

```text
BILLING_REFRESH_SCHEDULE="every 2 hours"
BILLING_REFRESH_MAX_DUE_USERS=300
BILLING_REFRESH_TIMEOUT_SECONDS=300
```

Validation rules:

- `BILLING_REFRESH_MAX_DUE_USERS` must be an integer between `1` and `1000`.
- `BILLING_REFRESH_TIMEOUT_SECONDS` must be an integer between `30` and `540`.
- `BILLING_REFRESH_SCHEDULE` must be accepted by Firebase scheduled functions.

## 11. Per-User Processing

Each due user is processed by:

```python
apply_due_refresh(user_id, now=run_started_at)
```

Each user must be isolated:

- one user's failure does not abort the whole run
- exceptions are logged with `userId`
- the run result increments `failed`
- remaining users continue processing

The user transaction must preserve current behavior:

- read `users/{userId}`
- check whether the user is still due
- skip if `credits.reserved > 0`
- compute effective refresh plan
- compute next monthly refresh date
- check deterministic ledger entry
- update user credits and billing metadata
- write `credit_ledger/{ledgerId}`

## 12. Idempotency

Scheduler grants must remain idempotent using deterministic ledger IDs:

```text
grant_refresh_<userId>_<yyyymmddTHHMMSS_due_at>_<grant_type>
```

Grant types:

```text
grant_free_monthly
grant_paid_annual_monthly_refresh
grant_paid_subscription_cycle
```

If the ledger entry already exists:

- do not grant credits again
- advance `billing.nextCreditRefreshAt` if needed
- return an idempotent outcome such as `already_applied`

## 13. Reserved Credit Behavior

If:

```text
credits.reserved > 0
```

then:

- return `reserved`
- do not change `credits.balance`
- do not change `credits.monthlyAllowance`
- do not change `billing.nextCreditRefreshAt`
- do not create a grant ledger entry

Leaving `billing.nextCreditRefreshAt` unchanged keeps the user due for the next run.

## 14. Per-User Refresh Audit Fields

The user object should store the latest scheduler refresh attempt status for manual inspection.

Current implementation already stores successful refresh timestamps through:

```text
billing.lastCreditRefreshAt
billing.nextCreditRefreshAt
credits.lastGrantType
credits.lastGrantAt
credits.lastGrantInvoiceId
```

The scheduler implementation should add explicit scheduler attempt metadata under `billing.refreshScheduler`:

```text
billing.refreshScheduler.lastAttemptAt
billing.refreshScheduler.lastStatus
billing.refreshScheduler.lastErrorMessage
billing.refreshScheduler.lastRunId
```

Allowed `lastStatus` values:

```text
applied
already_applied
reserved
missing
failed
waiting_for_invoice
billing_state_inconsistent
```

Status rules:

- On successful grant, set `lastStatus = "applied"` and clear `lastErrorMessage`.
- On idempotent replay, set `lastStatus = "already_applied"` and clear `lastErrorMessage`.
- On reserved skip, set `lastStatus = "reserved"` and clear `lastErrorMessage`.
- On unexpected per-user exception, set `lastStatus = "failed"` and write a short sanitized `lastErrorMessage`.

`lastErrorMessage` must not contain secrets, Stripe API keys, raw webhook payloads, auth tokens, or full stack traces.

With these fields in the user object, a separate ops collection for failed users is not required for v1.

## 15. Monthly Subscription Repair Path

Normal monthly paid subscription refreshes are handled by Stripe `invoice.paid`.

The scheduler may repair monthly paid subscriptions only if local state indicates a paid invoice exists but the credit grant was not applied.

Repair eligibility:

- `billing.billingInterval == "month"`
- active plan is not `free`
- `billing.latestInvoiceId` exists
- `billing.latestInvoiceId != credits.lastGrantInvoiceId`

If those conditions are true, the scheduler may grant the active paid allowance using:

```text
grant_paid_subscription_cycle
```

## 16. Security

The scheduled function calls backend refresh logic directly from a trusted Firebase runtime.

The scheduler should not depend on browser auth, user auth, or App Check.

Do not expose a production HTTP endpoint for manual credit refresh.

Manual production refreshes should use Cloud Scheduler's on-demand run operation:

```bash
gcloud scheduler jobs run refreshCredits --location=<location>
```

Operators can also use the Google Cloud Console Cloud Scheduler page to run the job manually.

## 17. Cloud Monitoring Metrics

The scheduler should emit Cloud Monitoring metrics in addition to structured logs.

Recommended custom metrics:

```text
billing/refresh/processed_count
billing/refresh/skipped_reserved_count
billing/refresh/failed_count
billing/refresh/scanned_count
billing/refresh/duration_ms
```

Metric labels:

```text
environment
function_name
```

These metrics allow alerting on repeated failures, zero processed users when many users are due, or unusual run duration.

Structured logs remain the primary debugging surface for run IDs and per-user details. `run_id` should not be a Cloud Monitoring metric label because it is high-cardinality.

## 18. Local Testing

### 18.1 Manual Production Run

After deployment, run the scheduled job on demand with:

```bash
gcloud scheduler jobs run refreshCredits --location=<location>
```

Cloud Scheduler dispatches the scheduled function immediately. The job still uses the same batch limit and idempotency protections.

Expected function log summary:

```json
{
  "processed": 1,
  "skipped_reserved": 0,
  "failed": 0
}
```

### 18.2 Direct Function Invocation

Against the Firestore emulator:

```bash
set -a
. env/dev.env
. env/local.env
set +a

FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \
GOOGLE_CLOUD_PROJECT=sightsinger-app \
../ai-singer-diffsinger/.venv310/bin/python -c "from src.backend.billing_refresh import run_credit_refresh; print(run_credit_refresh(max_users=300))"
```

### 18.3 Free User Test

Set a local test user:

```text
billing.activePlanKey = "free"
billing.billingInterval = "none"
billing.nextCreditRefreshAt = past timestamp
credits.reserved = 0
credits.balance = 0
```

Run scheduler.

Expected:

```text
credits.balance = 8
credits.monthlyAllowance = 8
credits.lastGrantType = "grant_free_monthly"
billing.lastCreditRefreshAt = run time
billing.nextCreditRefreshAt = next monthly refresh
billing.refreshScheduler.lastStatus = "applied"
billing.refreshScheduler.lastErrorMessage = null
credit_ledger grant entry exists
```

### 18.4 Annual Paid User Test

Set a local test user:

```text
billing.activePlanKey = "solo_annual"
billing.billingInterval = "year"
billing.nextCreditRefreshAt = past timestamp
credits.reserved = 0
credits.balance = 0
```

Run scheduler.

Expected:

```text
credits.balance = 30
credits.monthlyAllowance = 30
credits.lastGrantType = "grant_paid_annual_monthly_refresh"
billing.lastCreditRefreshAt = run time
billing.nextCreditRefreshAt = next monthly refresh
billing.refreshScheduler.lastStatus = "applied"
billing.refreshScheduler.lastErrorMessage = null
credit_ledger grant entry exists
```

For Pro annual, expected allowance is 120 credits.

### 18.5 Reserved Credit Test

Set:

```text
billing.nextCreditRefreshAt = past timestamp
credits.reserved = 1
```

Run scheduler.

Expected:

```text
skipped_reserved increments
credits.balance unchanged
billing.nextCreditRefreshAt unchanged
billing.refreshScheduler.lastStatus = "reserved"
billing.refreshScheduler.lastErrorMessage = null
no grant ledger entry created
```

Then set:

```text
credits.reserved = 0
```

Run scheduler again. The refresh should apply.

### 18.6 Batch Limit Test

Create more than `BILLING_REFRESH_MAX_DUE_USERS` due users.

Run scheduler.

Expected:

```text
processed + skipped_reserved + failed <= BILLING_REFRESH_MAX_DUE_USERS
remaining due users still have billing.nextCreditRefreshAt <= now
next scheduler run processes the next batch
```

## 19. Deployment Checklist

1. Add Firebase scheduled function wrapper `refreshCredits`.
2. Add scheduler config env vars.
3. Update `run_credit_refresh` to accept `max_users`.
4. Add due-user query ordering and limit.
5. Add per-user failure isolation.
6. Verify or add Firestore index for `users.billing.nextCreditRefreshAt ASC`.
7. Verify Cloud Scheduler on-demand run access for operators.
8. Deploy scheduler to the same project as the billing Firestore datastore.
9. Verify logs from one dry run.
10. Verify local and production Firestore writes on a controlled test user.
11. Emit Cloud Monitoring metrics.
12. Verify per-user `billing.refreshScheduler` audit fields.

## 20. Final Decisions

1. The scheduler should not stop early based on elapsed time. The max user count is the bounding mechanism.
2. The scheduler should emit Cloud Monitoring metrics in addition to structured logs.
3. Failed refresh attempts should be written to the user object through `billing.refreshScheduler`; a separate ops collection is not required for v1.
