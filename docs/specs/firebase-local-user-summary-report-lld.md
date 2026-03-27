# LLD: Firebase Local One-Off User Summary Report

## Purpose

Define the smallest implementation for a one-off user summary report that runs locally against production Firebase/GCP data.

This report is intended to support trial-user segmentation for email outreach, for example:

- invite inactive trial users to re-login
- extend trial access with top-up credits
- batch outreach to control inference cost and system load

This design covers only the user summary report.

It does not cover:

- the detailed per-job deep-dive report
- an admin UI
- BigQuery pipelines
- automated email sending

## Target Output

The report output is a CSV file with one row per user and these columns:

- `user_name`
- `email`
- `first_created_at`
- `last_logged_in_at`
- `completed_synthesis_jobs`
- `credit_balance`
- `credit_reserved`
- `available_credit_balance`
- `trial_expires_at`
- `is_trial_expired`
- `last_completed_synthesis_at`

Optional derived segmentation columns for immediate batch planning:

- `trial_status`
- `engagement_bucket`
- `email_batch_candidate`

## Execution Model

The report runs locally as a Python script in this repo.

Recommended location:

- [scripts/report_user_summary.py](/Users/alanchan/antigravity/ai-singer-diffsinger/scripts/report_user_summary.py)

Execution pattern:

```bash
python scripts/report_user_summary.py \
  --project sightsinger-app \
  --output reports/user_summary_YYYYMMDD.csv
```

The script reads production data directly using:

- Firebase Admin Auth API
- Firestore

Cloud Storage is not required for the summary report.

## Existing Source Systems

### Firebase Auth

Primary source for identity metadata:

- user display name
- email
- account creation time
- last sign-in time

This is the most reliable source for:

- `user_name`
- `email`
- `first_created_at`
- `last_logged_in_at`

### Firestore `users`

Current source from [credits.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/credits.py):

- document path: `users/{uid}`
- relevant fields:
  - `email`
  - `createdAt`
  - `credits.balance`
  - `credits.reserved`
  - `credits.expiresAt`
  - `credits.overdrafted`

This is the source for:

- `credit_balance`
- `credit_reserved`
- `trial_expires_at`

### Firestore `jobs`

Current source from [job_store.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py):

- collection: `jobs`
- relevant fields:
  - `userId`
  - `status`
  - `jobKind`
  - `createdAt`
  - `updatedAt`

This is the source for:

- `completed_synthesis_jobs`
- `last_completed_synthesis_at`

## Field Mapping Rules

### Identity fields

#### `user_name`

Source:

- Firebase Auth `display_name`

Do not infer a name from email for Phase 1.

#### `email`

Source:

- Firebase Auth `email`

#### `first_created_at`

Resolution order:

1. Firebase Auth `user_metadata.creation_timestamp`
2. Firestore `users.createdAt`
3. empty string

Implementation note:

- Firebase Auth metadata timestamps are returned in milliseconds since epoch
- divide by `1000` before converting to UTC ISO-8601 strings

#### `last_logged_in_at`

Resolution order:

1. Firebase Auth `user_metadata.last_sign_in_timestamp`
2. empty string

Implementation note:

- Firebase Auth metadata timestamps are returned in milliseconds since epoch
- divide by `1000` before converting to UTC ISO-8601 strings

Do not use Firestore session activity as a substitute in Phase 1.

Reason:

- session activity is not the same as authenticated sign-in
- for outreach segmentation, actual sign-in is the more defensible metric

### Credit fields

#### `credit_balance`

Source:

- Firestore `users.credits.balance`

Default:

- `0`

#### `credit_reserved`

Source:

- Firestore `users.credits.reserved`

Default:

- `0`

#### `available_credit_balance`

Computed as:

- `credit_balance - credit_reserved`

#### `trial_expires_at`

Source:

- Firestore `users.credits.expiresAt`

Default:

- empty string

#### `is_trial_expired`

Computed at report runtime:

- `true` if `trial_expires_at < now_utc`
- `false` otherwise

If `trial_expires_at` is missing, output:

- empty string

### Job aggregate fields

#### `completed_synthesis_jobs`

Count Firestore `jobs` where:

- `userId == uid`
- `status == "completed"`
- `jobKind == "synthesis"` when present

If `jobKind` is absent on older records:

- include the job only if it has at least one of:
  - `audioUrl`
  - `outputPath`

This avoids counting preprocess jobs as synthesis completions.

#### `last_completed_synthesis_at`

For the filtered completed synthesis jobs above:

- use the maximum of `updatedAt`

Default:

- empty string

## Local Script Design

### Step 1: initialize Firebase Admin

The script uses the same Firebase Admin setup as the backend.

Preferred Phase 1 behavior:

- use a local service account JSON key
- allow optional `--project` override

Expected environment variables:

- `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json`
- optionally `GOOGLE_CLOUD_PROJECT=sightsinger-app`

The script should fail fast if:

- `GOOGLE_APPLICATION_CREDENTIALS` is missing
- the service account file cannot be read
- project access is denied

### Local Authorization Guidance

For this one-off report, use a service account, not a short-lived token string.

Recommendation:

- create or reuse a service account with read access to:
  - Firebase Auth users
  - Firestore

Minimum practical permissions:

- Firebase Authentication Admin or equivalent user-read capability
- Firestore read access for:
  - `users`
  - `jobs`

Recommended local setup:

1. download a service account JSON key
2. store it outside the repo
3. export:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/secure/local/path/firebase-admin-reporting.json
export GOOGLE_CLOUD_PROJECT=sightsinger-app
```

4. run the script

```bash
python scripts/report_user_summary.py \
  --project sightsinger-app \
  --output reports/user_summary_YYYYMMDD.csv
```

Do not store the service account JSON in the repo.

Do not store raw credential contents inside `env/dev.env` or `env/prod.env`.

If env-based storage is needed, store only the file path:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/secure/local/path/firebase-admin-reporting.json
```

Phase 1 recommendation:

- support service-account-file authentication only
- do not implement token-string ingestion

Reason:

- Firebase Admin SDK is built for ADC / service-account auth
- this is simpler and safer than custom token loading
- it matches the backend's existing Firebase Admin access model

### Step 2: list Firebase Auth users

Use paginated Admin SDK iteration to fetch all Auth users.

Build an in-memory map:

- `auth_users[uid] = {name, email, created_at, last_login_at}`

This Auth list is the canonical user universe for Phase 1.

Reason:

- the outreach target is real signed-in users
- some Firestore `users` docs may be stale or incomplete

Phase 1 filter:

- skip Auth users whose `email` is missing
- skip likely anonymous/guest users

Recommended detection rule:

- skip users where `email is null`
- skip users where `provider_data` is empty

Reason:

- anonymous or guest users are not useful for this email campaign
- they can materially inflate report size with non-actionable rows

### Step 3: fetch Firestore `users`

Read all Firestore `users` documents into a map by UID.

Build:

- `credit_users[uid] = {...}`

### Step 4: aggregate Firestore `jobs`

Read Firestore `jobs` and aggregate by `userId`.

Build per-user counters:

- `completed_synthesis_jobs`
- `last_completed_synthesis_at`

For large datasets, this can be streamed document-by-document instead of fully materialized.

Phase 1 note on cost:

- this approach incurs 1 Firestore read per job document scanned
- for example, 500,000 jobs means roughly 500,000 Firestore reads for this step

Recommendation:

- keep this streaming design in Phase 1 because it safely preserves the backward-compatibility rule for older jobs where `jobKind` may be missing
- revisit the design when the `jobs` collection exceeds approximately 1M documents

Future migration options:

- BigQuery reporting export
- per-user Firestore aggregation queries using `.count()` plus a separate latest-completed query, if the job schema and indexes become strict enough to remove the old-job fallback logic

### Step 5: join and normalize

For each Auth user UID:

1. read Auth metadata
2. merge Firestore `users` credits data if present
3. merge jobs aggregate if present
4. compute derived fields
5. emit one normalized row

Optional Phase 1.5:

- append Firestore-only `users` documents that have no matching Auth user
- mark them with `identity_status = firestore_only`

Recommendation:

- exclude Firestore-only users in Phase 1
- keep the report simple and campaign-safe

## CSV Serialization Rules

Write UTF-8 CSV with a stable header order.

Datetime format:

- ISO-8601 UTC strings

Boolean format:

- `true`
- `false`
- empty string for unknown

Numeric fields:

- integers only

## Optional Derived Segmentation Columns

These are convenience fields for manual batching and do not require new storage.

### `trial_status`

Values:

- `active`
- `expired`
- `unknown`

### `engagement_bucket`

Recommended rules:

- `none` if `completed_synthesis_jobs == 0`
- `light` if `1 <= completed_synthesis_jobs <= 2`
- `engaged` if `3 <= completed_synthesis_jobs <= 9`
- `power` if `completed_synthesis_jobs >= 10`

### `email_batch_candidate`

Recommended Phase 1 rule:

- `true` when:
  - email exists
  - completed synthesis jobs >= 1
  - and at least one of:
    - trial is expired
    - available_credit_balance <= 0
- else `false`

This is only a convenience flag, not a sending decision engine.

## Error Handling

### Auth read failure

- fail the script
- do not produce partial CSV silently

### Firestore `users` read failure

- fail the script

### Firestore `jobs` read failure

- fail the script

### Per-record malformed fields

Do not fail the whole run for a single malformed document.

Instead:

- log a warning with UID or document ID
- continue with safe defaults for that record

Examples:

- invalid timestamp type
- missing nested `credits`
- missing `userId` on a job document

## Logging

The script should print:

- project ID
- number of Auth users scanned
- number of Firestore `users` docs scanned
- number of Firestore `jobs` docs scanned
- number of output rows written
- output file path

Optional:

- counts by trial status
- counts by engagement bucket

## Security and Privacy

This report contains PII and account usage data.

Phase 1 safety rules:

- write output only to a local path explicitly passed by CLI
- do not upload the CSV automatically
- do not generate public links
- do not print full user rows to stdout

Recommended output location:

- a local ignored directory such as `reports/`

Recommendation:

- add `reports/` to `.gitignore` if not already ignored

## Acceptance Criteria

The design is complete when an implementation can:

1. run locally against the production Firebase project
2. export one CSV row per Auth user
3. populate:
   - identity metadata
   - trial/credit metadata
   - completed synthesis counts
4. compute:
   - available credit balance
   - trial expiration status
   - last completed synthesis timestamp
5. do so without modifying production data

## Out of Scope

Not included in this LLD:

- per-user deep-dive job report
- signed download links for scores or audio
- admin web page
- BigQuery
- automated segmentation persistence
- email delivery workflow

Those should be designed separately after the one-off summary report is working.
