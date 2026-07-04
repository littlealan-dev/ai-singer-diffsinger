# Export Mix Credit Billing LLD

## Purpose

Define the low-level design for charging credits every time a user exports a multitrack mix.

The feature extends the existing async `export_mix` job flow and must use the same transactional reservation, release, settlement, and ledger guarantees as synthesis jobs.

## Product Rules

- Every Export Mix action is billable.
- Pricing: `1 credit = 1 minute` of exported mix audio.
- Credit amount is always an integer.
- Any partial minute rounds up to the next whole credit.
- Minimum charge is `1 credit`.
- Examples:
  - `0.1s` to `60.0s` => `1 credit`
  - `60.1s` to `120.0s` => `2 credits`
  - `600.0s` => `10 credits`
- The user is charged on every successful export, even if the same track set was exported before.
- If the export fails, the reserved credits are released.
- If the export succeeds, the reserved credits are consumed.

## Non-Goals

- Charging for multitrack playback.
- Charging for individual track downloads.
- Changing synthesis pricing. Synthesis remains on its existing rate.
- Batch synthesis.
- Reusing a previous exported mix without charging.

## Credit Calculation

Export mix must use a separate estimator from synthesis.

```python
EXPORT_MIX_CREDIT_DURATION_SECONDS = 60

def estimate_export_mix_credits(duration_seconds: float) -> int:
    if duration_seconds <= 0:
        raise ValueError("Export mix duration must be positive.")
    return max(1, math.ceil(duration_seconds / EXPORT_MIX_CREDIT_DURATION_SECONDS))
```

Important: do not call the existing synthesis `estimate_credits(...)` helper for export-mix settlement unless it is refactored to accept a pricing unit. The current synthesis helper is based on 30-second units.

## UI Requirement

The multitrack player displays the required export credits before the user clicks Export Mix.

Display source:
- Use the duration of the first audio track added to the multitrack player.
- Store this as `firstAddedTrackDurationSeconds` or derive it from `multiTrackAudioTracks[0].durationSeconds`.
- If the first lane is later replaced by regenerating the same part, update the duration for that lane and recompute credits.
- If all tracks are cleared on new score upload, clear the displayed export credit amount.

Display formula:

```ts
requiredCredits = Math.max(1, Math.ceil(firstAddedTrackDurationSeconds / 60));
```

Suggested UI copy:
- `Export: 3 credits`
- If duration is not loaded yet: `Export: --`
- If no tracks exist, hide or disable the estimate.

The displayed amount is an estimate from client-observed track duration. Backend must compute the authoritative amount before reservation.

## Backend Authoritative Duration

The backend must not trust a frontend-supplied duration or audio URL.

For the export-mix request, the frontend should pass:

```json
{
  "format": "wav",
  "billing_reference_job_id": "first-added-track-synthesis-job-id",
  "tracks": [
    {
      "job_id": "source-synthesis-job-id",
      "part_id": "Soprano",
      "key": "id:Soprano",
      "label": "Soprano",
      "verse_number": "1",
      "muted": false,
      "solo": false,
      "volume": 1.0
    }
  ]
}
```

Backend verification:
- `billing_reference_job_id` must belong to the same `userId` and `sessionId`.
- It must be a completed synthesis job.
- It must not be a preprocess job or export-mix job.
- It must have an audio output path owned by the same session.
- Prefer `jobs/{billing_reference_job_id}.actualDurationSeconds`.
- If missing, probe the verified source audio file duration server-side.

Authoritative billable duration:
- Use the verified duration of `billing_reference_job_id`.
- Compute `requiredCredits = estimate_export_mix_credits(billableDurationSeconds)`.

Guardrail:
- Resolve durations for all audible source tracks before reserving.
- If any audible source duration is longer than the billing reference duration by more than a small tolerance, reject the export before reserving and return a retryable validation error.
- Suggested tolerance: `1.0s`.
- This prevents undercharging if the first-added track is unexpectedly shorter than another exported track.

## API Changes

### POST `/sessions/{session_id}/export-mix`

Request additions:

```ts
type ExportMixRequest = {
  format: "wav";
  billing_reference_job_id: string;
  tracks: ExportMixTrackRequest[];
};
```

Response on accepted job:

```json
{
  "status": "queued",
  "progress_url": "/sessions/{session_id}/progress?job_id={job_id}",
  "job_id": "{export_mix_job_id}",
  "required_credits": 3,
  "billable_duration_seconds": 172.4
}
```

Insufficient credits:
- Do not start export rendering.
- Return the same billing error shape used by synthesis insufficient-credit handling where possible.
- Include:
  - `required_credits`
  - `available_credits`
  - `job_kind: "export_mix"`

Suggested HTTP status:
- Use the existing synthesis insufficient-credit status for consistency.

### GET `/sessions/{session_id}/progress`

For export-mix jobs, include billing metadata when available:

```json
{
  "status": "running",
  "job_kind": "export_mix",
  "progress": 0.54,
  "required_credits": 3,
  "billable_duration_seconds": 172.4
}
```

On completion:

```json
{
  "status": "done",
  "job_kind": "export_mix",
  "progress": 1.0,
  "audio_url": "/sessions/{session_id}/audio?file=mix-{job_id}.wav",
  "consumed_credits": 3,
  "billable_duration_seconds": 172.4
}
```

## Job Document

Collection: `jobs/{jobId}`

Queued/running:

```json
{
  "userId": "{uid}",
  "sessionId": "{session_id}",
  "status": "queued",
  "jobKind": "export_mix",
  "renderType": "export_mix",
  "progress": 0.0,
  "billing": {
    "pricing": "export_mix_v1",
    "pricingUnitSeconds": 60,
    "billingReferenceJobId": "{source_job_id}",
    "billableDurationSeconds": 172.4,
    "requiredCredits": 3,
    "reservationStatus": "pending"
  },
  "mix": {
    "format": "wav",
    "trackCount": 4,
    "tracks": []
  }
}
```

Completed:

```json
{
  "status": "completed",
  "progress": 1.0,
  "audioUrl": "/sessions/{session_id}/audio?file=mix-{job_id}.wav",
  "outputPath": "sessions/{uid}/{session_id}/jobs/{job_id}/mix.wav",
  "actualDurationSeconds": 172.4,
  "consumedCredits": 3,
  "billing": {
    "pricing": "export_mix_v1",
    "pricingUnitSeconds": 60,
    "billingReferenceJobId": "{source_job_id}",
    "billableDurationSeconds": 172.4,
    "requiredCredits": 3,
    "consumedCredits": 3,
    "reservationStatus": "settled"
  }
}
```

Failed after reservation release:

```json
{
  "status": "failed",
  "jobKind": "export_mix",
  "progress": 1.0,
  "errorMessage": "Export mix failed.",
  "billing": {
    "pricing": "export_mix_v1",
    "requiredCredits": 3,
    "reservationStatus": "released"
  }
}
```

## Reservation Flow

The reservation must be transaction-backed.

1. Authenticate user.
2. Verify session ownership.
3. Select audible tracks using current M/S rules:
   - if any track has `solo=true`, export only solo tracks
   - otherwise export tracks where `muted=false`
4. Verify every audible track references a completed synthesis `job_id` in the same user/session.
5. Verify `billing_reference_job_id`.
6. Compute `billableDurationSeconds`.
7. Compute `requiredCredits`.
8. Generate export-mix `job_id`.
9. Call a transactional reserve operation:
   - increment `users/{uid}.credits.reserved`
   - create `credit_reservations/{job_id}`
   - write a reserve ledger entry
10. If reservation is rejected, return a billing error and do not render.
11. Create or update the export-mix job as `queued`.
12. Start background export task.

Preferred implementation:
- Add `reserve_credits_for_job(...)` or extend `reserve_credits(...)` to accept metadata:
  - `session_id`
  - `job_kind`
  - `pricing`
  - `pricing_unit_seconds`
  - `billable_duration_seconds`
- The existing `reserve_credits(...)` transaction can be reused if metadata is added without breaking synthesis callers.

Reservation document:

```json
{
  "jobId": "{export_mix_job_id}",
  "sessionId": "{session_id}",
  "userId": "{uid}",
  "jobKind": "export_mix",
  "pricing": "export_mix_v1",
  "pricingUnitSeconds": 60,
  "billableDurationSeconds": 172.4,
  "estimatedCredits": 3,
  "createdAt": "...",
  "expiresAt": "...",
  "status": "pending"
}
```

## Successful Settlement Flow

Settlement must be transaction-backed.

Because export mix uses a different credit rate, do not call `settle_credits_and_complete_job(...)` unchanged if it still computes credits using the synthesis 30-second estimator.

Implementation options:

1. Add a dedicated helper:

```python
def settle_export_mix_credits_and_complete_job(
    uid: str,
    job_id: str,
    session_id: str,
    billable_duration_seconds: float,
    *,
    actual_duration_seconds: float | None = None,
    output_path: str,
    audio_url: str,
) -> CompleteJobAndSettleCreditsResult:
    actual_credits = estimate_export_mix_credits(billable_duration_seconds)
    ...
```

2. Or refactor the shared helper:

```python
def settle_credits_and_complete_job(
    ...,
    actual_credits: int | None = None,
    pricing: str = "synthesis_v1",
    pricing_unit_seconds: int = 30,
)
```

Transaction writes:
- decrement `users/{uid}.credits.reserved` by reservation `estimatedCredits`
- decrement `users/{uid}.credits.balance` by `actualCredits`
- mark `credit_reservations/{job_id}.status = "settled"`
- set `actualCredits`
- set `settledAt`
- write deterministic ledger entry `credit_ledger/settle_{job_id}`
- mark `jobs/{job_id}` completed with `audioUrl`, `outputPath`, `consumedCredits`, and billing metadata

The user-visible completion boundary is this transaction. The UI must not receive a completed `audio_url` until the settlement transaction succeeds.

## Failure Release Flow

Release must be transaction-backed.

If any step after successful reservation fails:
- rendering failure
- storage upload failure
- audio metadata/probe failure
- job cancellation
- worker exception

Then call `release_credits(uid, job_id)` or a metadata-preserving equivalent.

Transaction writes:
- decrement `users/{uid}.credits.reserved` by reservation `estimatedCredits`
- mark `credit_reservations/{job_id}.status = "released"`
- set `releasedAt`
- write a release ledger entry

After release succeeds:
- mark job `failed`
- set `billing.reservationStatus = "released"`

If release fails:
- mark reservation `reconciliation_required`
- mark job `credit_reconciliation_required` or `failed` with explicit reconciliation metadata, following the existing SIG-16 pattern
- do not expose an audio URL as completed

## Ledger Entries

Keep ledger logging for reserve, settle, and release.

Reserve ledger:

```json
{
  "userId": "{uid}",
  "sessionId": "{session_id}",
  "type": "reserve",
  "jobKind": "export_mix",
  "pricing": "export_mix_v1",
  "pricingUnitSeconds": 60,
  "jobId": "{job_id}",
  "amount": 0,
  "reservedDelta": 3,
  "reservedAfter": 7,
  "balanceAfter": 20,
  "billableDurationSeconds": 172.4,
  "createdAt": "..."
}
```

Settle ledger:

```json
{
  "userId": "{uid}",
  "sessionId": "{session_id}",
  "type": "settle",
  "jobKind": "export_mix",
  "pricing": "export_mix_v1",
  "pricingUnitSeconds": 60,
  "jobId": "{job_id}",
  "amount": -3,
  "reservedDelta": -3,
  "reservedAfter": 4,
  "balanceAfter": 17,
  "billableDurationSeconds": 172.4,
  "createdAt": "..."
}
```

Release ledger:

```json
{
  "userId": "{uid}",
  "sessionId": "{session_id}",
  "type": "release",
  "jobKind": "export_mix",
  "pricing": "export_mix_v1",
  "pricingUnitSeconds": 60,
  "jobId": "{job_id}",
  "amount": 0,
  "reservedDelta": -3,
  "reservedAfter": 4,
  "balanceAfter": 20,
  "billableDurationSeconds": 172.4,
  "createdAt": "..."
}
```

Ledger invariants:
- reserve increases only `reserved`
- release decreases only `reserved`
- settle decreases both `reserved` and `balance`
- all credit mutation ledger entries are created inside the same transaction as the user credit mutation

## UI Flow

### Track Added

When generated audio is added or replaced in the multitrack player:

1. Store `durationSeconds` on the track.
2. Prefer duration from backend progress payload if available.
3. Fallback to WaveSurfer `ready` duration.
4. Recompute `exportMixRequiredCredits` from the first track in `multiTrackAudioTracks`.

### Export Button

The export button should show:
- share/export icon
- adjacent or inline credit estimate: `Export: N credits`

Disabled states:
- no tracks
- first track duration unknown
- user credits locked or unavailable
- export job already running

On click:
1. Build request with all tracks and current M/S/volume settings.
2. Include `billing_reference_job_id` from the first track in `multiTrackAudioTracks`.
3. Do not send `audio_url`.
4. Backend returns insufficient-credit error if reserve fails.
5. UI opens the same paywall/insufficient-credit flow used for synthesis.
6. If accepted, show percentage-only progress.
7. On completion, download or expose the mix using signed audio URL.
8. Refresh credits after terminal state.

### Credit Display Refresh

After export terminal state:
- successful export: refresh credit balance
- failed export after release: refresh credit balance/reserved amount
- insufficient-credit rejection: refresh credit balance if the response indicates stale local state

## Backend Progress

Progress remains percentage-only for UI text.

Backend can internally set steps, but UI should display only:
- `0%` to `100%`

Do not expose wording like `encoding`, `uploading`, or `settling` in the multitrack player progress display unless a later UI design explicitly asks for it.

## Error Handling

### Insufficient Credits

Reservation result `insufficient_balance`:
- no render starts
- no audio URL
- response includes required and available credits
- UI opens billing/paywall route

### Reservation Succeeds, Job Startup Fails

- call release transaction
- if release succeeds, return failure
- if release fails, mark reconciliation required

### Render Fails

- call release transaction
- mark job failed after release

### Storage Upload Fails

- call release transaction
- mark job failed after release
- do not expose local-only output as completed

### Settlement Fails

- do not mark job completed
- do not expose audio URL
- retry settlement using existing retry wrapper
- if retry exhausted, attempt release only if reservation is still pending and no user-visible output was published
- otherwise mark reconciliation required

## Concurrency and Idempotency

- Every export click creates a new export-mix job and a new reservation.
- The same track set exported twice must charge twice.
- `credit_reservations/{job_id}` remains one reservation per export job.
- Retrying the same backend job after transient failure must be idempotent:
  - reserve can return `reservation_exists`
  - settle can return `already_completed_and_settled`
  - release can return `already_released`
- The UI should disable the export button while one export job is running to avoid accidental duplicate jobs.
- If the user opens two tabs and exports twice, both accepted jobs are billable if each reserves credits.

## Security

- Frontend must not send `audio_url` for billing or source resolution.
- Backend resolves all source audio from verified completed synthesis jobs.
- Backend verifies:
  - source job belongs to same user
  - source job belongs to same session
  - source job is completed
  - source job has audio output path
  - output path is under the expected session/job storage prefix or local session directory
- Backend never uses frontend duration for charging.

## Tests

### Unit Tests

Credit estimator:
- `0` or negative duration raises
- `0.1s` => `1`
- `60.0s` => `1`
- `60.1s` => `2`
- `600.0s` => `10`

Reservation metadata:
- export-mix reservation stores `jobKind`, `pricing`, `pricingUnitSeconds`, and `billableDurationSeconds`
- reserve ledger includes export-mix metadata

Settlement:
- successful export decrements `reserved` and `balance`
- consumed credits use 60-second units, not synthesis 30-second units
- deterministic settle ledger is written
- completed job includes `consumedCredits`

Release:
- failed export releases reserved credits
- release ledger is written
- failed job does not expose audio URL

### API Tests

- insufficient credits returns billing error and does not create/render export output
- successful export reserves before rendering
- successful export settles before progress reports `done`
- failed renderer releases reservation
- `billing_reference_job_id` from another user/session is rejected
- request with frontend `audio_url` is ignored or rejected

### UI Tests

- credit estimate appears after first track duration is known
- credit estimate rounds up by minute with minimum 1
- export button disabled while export is running
- insufficient-credit response opens the existing billing/paywall path
- credits refresh after success and failure

## Rollout Plan

1. Add export-mix credit estimator and tests.
2. Add reservation metadata support without changing synthesis behavior.
3. Add export-mix reserve flow before render.
4. Add export-mix settlement helper using 60-second billing.
5. Add release-on-failure path and reconciliation handling.
6. Add progress payload billing metadata.
7. Add UI credit estimate from first-added track duration.
8. Add UI insufficient-credit handling and credit refresh.
9. Test locally with successful export, insufficient credits, and forced render failure.
10. Deploy backend before enabling the UI button in production.

## Decisions

- Export-mix jobs do not trigger the audio feedback prompt, because the user is exporting a mix, not evaluating a new synthesis take.
- Exported mixes do not appear in the individual chat message list. They stay scoped to the multitrack player.
- Identical repeated exports are still charged. Every successful Export Mix click consumes credits.
