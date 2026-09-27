# MIDI instrumental credit billing — low-level design

Status: Approved product decisions recorded; implementation is not yet authorized.

Date: 2026-09-19

Workspace: `ai-singer-diffsinger-integration`

## 1. Objective

Add one-time credit charging for browser-playable non-vocal MIDI instrumentals created for an active parsed score.

Pricing:

- One synthesized vocal part costs 1 credit per started 30 seconds.
- All eligible non-vocal MIDI tracks together cost 1 credit per started 120 seconds.
- The instrumental fee is independent of the number of MIDI tracks.
- A score with no eligible non-vocal MIDI tracks has an instrumental cost of 0.
- The selected **With Repeats** state determines the duration used by both components.
- The displayed quote and reservation use one score-based estimated breakdown.
- After successful generation, settlement recalculates both components from
  the actual generated audio duration and charges that actual total for a
  combined job. A vocal-only job recalculates and charges vocal only.
- A combined job reserves, settles, or rolls back vocal and instrumental
  components together. A vocal-only job reserves, settles, or rolls back vocal
  only and performs no instrumental payment-state write. A previously paid
  instrumental scope is never changed by a later vocal-only job.

This design interprets “charge once” using the earlier deferred-generation
decision: the instrumental component is charged once per uploaded score, on the
first successful synthesis that prepares its MIDI artifacts. The billing scope
is `user_id / session_id / score_id`; it deliberately excludes the parsed score
version. Its pricing basis is the repeat mode of the first successfully
generated vocal take for that uploaded score, because a MIDI failure stops the
job before an actual vocal-audio duration exists. Further vocal takes and every
revision of the same uploaded score pay only their vocal component. Each new
upload receives a new `score_id` and therefore a new unpaid instrumental scope,
even when the user uploads identical file contents again.

## 2. Current behaviour and gaps

### 2.1 Current vocal pricing

`src/backend/credits.py` owns the current vocal rate:

```text
CREDIT_DURATION_SECONDS = 30
vocal credits = ceil(normalized duration / 30)
```

The duration is normalized to 1 ms before rounding up. That boundary behaviour must remain shared by every new calculation.

Today the same numeric estimate is independently produced in several places:

- `src/backend/llm_prompt.py` constructs `Current synthesis estimate` for the LLM.
- `src/backend/orchestrator.py` recalculates an estimate before reservation.
- `src/backend/credits.py` recalculates from actual output duration during settlement.
- `ui/src/MainApp.tsx` duplicates `ceil(duration / 30)` in TypeScript.

Those paths can disagree and they do not carry component-level pricing.

The existing settlement behaviour of deriving cost from actual output duration
is intentional. The change is to make it use the same combined pricing
calculator and return an actual vocal/instrumental breakdown, not to replace it
with fixed-price settlement.

### 2.2 Current instrumental lifecycle

Parsing already exposes deterministic instrumental eligibility in:

```text
score_summary.instrument_program_resolution.instrumental_score_instrument_ids
```

This list comes from the same MusicXML eligibility rules used by the exporter. It exists before MIDI files are generated, including when an LLM program assignment is still required.

`Orchestrator._ensure_instrumental_midi_artifacts()` currently runs after credit reservation on the first synthesis for a score version. It creates both written-order and expanded-order MIDI files. A score-version marker prevents regeneration until another upload/reparse version is created. Regeneration remains revision-specific, but its billing state is upload-scoped and is not reset by regeneration.

There is currently no instrumental charge state. MIDI failure is deliberately non-fatal and vocal synthesis continues. That behaviour needs an explicit product decision once MIDI generation becomes billable; see section 16.

## 3. Canonical pricing rules

Introduce these named constants in the backend pricing module:

```python
VOCAL_CREDIT_DURATION_SECONDS = 30
INSTRUMENTAL_CREDIT_DURATION_SECONDS = 120
CREDIT_DURATION_PRECISION_SECONDS = 0.001
```

For the pre-synthesis estimate, let `Dv` be the current vocal job's selected
written/expanded score duration. The instrumental estimate uses the same
selected score duration while the uploaded-score billing scope is still unpaid:

```text
estimated_vocal_part_credits = ceil(normalize_ms(Dv) / 30)

estimated_instrumental_credits =
    0                                      if no eligible instrumental route
    0                                      if this uploaded score is already paid
    ceil(normalize_ms(Dv) / 120)           otherwise

total_estimated_credits =
    estimated_vocal_part_credits + estimated_instrumental_credits
```

After successful generation, let `A` be the actual generated vocal-audio
duration reported by the synthesis/save pipeline:

```text
actual_vocal_credits = ceil(normalize_ms(A) / 30)

actual_instrumental_credits =
    0                                      if no instrumental charge was reserved
    ceil(normalize_ms(A) / 120)            otherwise

total_actual_credits = actual_vocal_credits + actual_instrumental_credits
```

The actual audio duration is used for both components because the generated
MIDI playback and vocal take share the same selected score timeline. MIDI track
count still never multiplies the instrumental component.

There is no minimum instrumental charge when no eligible instrumental exists. When at least one eligible instrumental exists and the charge is required, the positive-duration formula naturally has a minimum of 1.

Track count must never multiply `instrumental_credits`.

Estimated-price examples:

| Selected duration | Eligible MIDI tracks | Instrumental already paid | Vocal | Instrumentals | Total |
|---:|---:|---:|---:|---:|---:|
| 60 s | 0 | no | 2 | 0 | 2 |
| 60 s | 1 | no | 2 | 1 | 3 |
| 60 s | 12 | no | 2 | 1 | 3 |
| 120 s | 4 | no | 4 | 1 | 5 |
| 120.001 s | 4 | no | 5 | 2 | 7 |
| 121 s | 4 | yes | 5 | 0 | 5 |

Actual settlement may differ from these examples when the generated audio is
shorter or longer than the score-derived duration. For example, a 121-second
score quote reserves 7 credits, but a successful 119.8-second output settles at
4 vocal credits plus 1 instrumental credit, for 5 actual credits, and releases
the unused 2-credit reservation.

## 4. Authoritative instrumental presence

Before generation, pricing must use:

```python
bool(
    score_summary
      ["instrument_program_resolution"]
      ["instrumental_score_instrument_ids"]
)
```

This is preferable to:

- counting all score parts;
- treating every part without lyrics as playable;
- checking whether a browser MIDI URL already exists;
- checking UI track state; or
- duplicating eligibility rules in billing code.

After generation, `performance_midi.has_instrumental_parts` is a useful consistency check, but it is too late to be the only pre-synthesis estimate input.

If program selection remains unresolved, the routes still count as instrumentals for pricing. The existing deterministic/LLM program-resolution preflight must complete before the binding billable quote is issued.

## 5. One shared pricing calculator

Create a pure backend pricing primitive, preferably in a new
`src/backend/synthesis_pricing.py` module, so quote construction and successful
settlement use exactly the same duration normalization, rounding, and component
arithmetic:

```python
@dataclass(frozen=True)
class SynthesisCreditBreakdown:
    duration_seconds: float
    vocal_part_credits: int
    instrumental_credits: int
    total_credits: int

def calculate_synthesis_credit_breakdown(
    *,
    duration_seconds: float,
    include_instrumentals: bool,
) -> SynthesisCreditBreakdown:
    ...
```

The estimate wrapper adds score, repeat, part, and one-time charge context:

```python
@dataclass(frozen=True)
class SynthesisCreditEstimate:
    pricing_version: int
    vocal_part_id: str | None
    expand_repeats: bool
    vocal_duration_seconds: float
    instrumental_pricing_duration_seconds: float | None
    vocal_part_credits: int
    instrumental_credits: int
    total_estimated_credits: int
    has_instrumental_parts: bool
    instrumental_charge_required: bool
    instrumental_charge_scope: str | None
    instrumental_pricing_expand_repeats: bool | None
    vocal_pricing_unit_seconds: int
    instrumental_pricing_unit_seconds: int

def estimate_synthesis_credits(
    *,
    vocal_duration_seconds: float,
    instrumental_pricing_duration_seconds: float | None,
    vocal_part_id: str | None,
    expand_repeats: bool,
    has_instrumental_parts: bool,
    instrumental_charge_required: bool,
    instrumental_charge_scope: str | None,
    instrumental_pricing_expand_repeats: bool | None,
) -> SynthesisCreditEstimate:
    ...
```

The pricing primitive must:

- validate a finite positive duration;
- use one shared millisecond normalization helper for both components;
- calculate the vocal component once;
- calculate the instrumental component once, never once per route/track;
- return a structured breakdown and total; and
- have no Firestore, session, LLM, or UI side effects.

The estimate wrapper resolves score-derived duration, eligibility, and one-time
charge state, then calls the primitive. Successful settlement calls the same
primitive with the actual generated vocal-audio duration and whether the
reservation included the one-time instrumental component. Neither function
inspects MusicXML or infers instruments.

Keep `estimate_credits()` as a compatibility wrapper for vocal-only callers during migration, implemented through the same normalization primitive. Export-mix pricing remains separate and unchanged.

## 6. Estimate response contract

Expose a snake_case backend contract and corresponding TypeScript type:

```json
{
  "pricing_version": 1,
  "vocal_part_id": "P1",
  "expand_repeats": true,
  "vocal_duration_seconds": 121.0,
  "vocal_part": {
    "pricing_unit_seconds": 30,
    "estimated_credits": 5
  },
  "instrumentals": {
    "has_instrumental_parts": true,
    "charge_required": true,
    "charge_scope": "user/session/score-id",
    "pricing_expand_repeats": true,
    "pricing_duration_seconds": 121.0,
    "pricing_unit_seconds": 120,
    "estimated_credits": 2,
    "charged_once_for_all_tracks": true
  },
  "total_estimated_credits": 7
}
```

`vocal_part_id` identifies the requested vocal target when known. The price does not vary by vocal part today, but carrying the ID prevents a quote for one selected part from being presented as a quote for another.

## 7. UI estimate flow

The browser must stop calculating credits locally.

Add a session-scoped read endpoint such as:

```text
GET /sessions/{session_id}/synthesis-estimate
    ?expand_repeats=true
    &part_id=P1
```

The endpoint:

1. Loads the active score ID/version and score summary.
2. Chooses `duration_seconds` or `expanded_duration_seconds` from `expand_repeats`.
3. Resolves parse-time instrumental presence.
4. Reads the instrumental charge state for that user/session/score ID.
5. If the uploaded score is unpaid, uses the current repeat mode for the
   instrumental estimate. A failed attempt does not permanently lock an
   estimate because it produces no actual vocal-audio duration.
6. Calls `estimate_synthesis_credits()`.
7. Returns the structured non-binding estimate.

Refresh this endpoint:

- after upload/reupload/reparse completes;
- when **With Repeats** changes;
- when the selected vocal part changes; and
- after a synthesis reaches any terminal billing state, because a successful first synthesis changes the future instrumental component to 0.

While the request is pending, retain the previous duration text but show both credit labels as calculating/disabled rather than briefly displaying locally computed values.

Replace:

```text
Estimated cost per part: X credits
```

with:

```text
Estimated cost per vocal part: X credits
Estimated cost for instrumentals: Y credits
```

Recommended post-payment wording:

```text
Estimated cost for instrumentals: 0 credits · already generated
```

The total does not need another permanent line in the compact score header unless space permits; it must be present in the LLM confirmation quote.

## 8. Binding LLM quote flow

The UI estimate is informational. The quote presented by the LLM must be
immutable and bound to the later synthesis choices and reservation. Its credit
values are explicitly estimates; successful settlement uses actual generated
duration. Confirmation is required by the prompt flow, not represented as a
backend-verified quote state.

### 8.1 Recommended quote tool

Add an internal MCP tool, `prepare_synthesis_quote`, used after the LLM has resolved the full requested render choices but before it asks for billable confirmation.

The request mirrors the billable synthesis choices needed to bind the quote, including:

- exact parser-visible `part_id`;
- exact `lyric_selection`;
- language and voicebank/style controls;
- authoritative repeat state (injected/validated by the backend);
- validated instrument program assignments when required; and
- any other option declared by the existing billable-confirmation rule.

The handler performs existing non-billable preflights first, calls the same `estimate_synthesis_credits()` function, persists an immutable quote, and returns:

```json
{
  "status": "quote_ready",
  "quote_id": "...",
  "part_id": "P1",
  "vocal_duration_seconds": 121.0,
  "vocal_part_credits": 5,
  "instrumental_credits": 2,
  "instrumental_pricing_expand_repeats": true,
  "instrumental_pricing_duration_seconds": 121.0,
  "total_estimated_credits": 7,
  "available_credits": 20,
  "balance_after": 13
}
```

The follow-up LLM response presents those exact values and asks for confirmation. It never calculates, sums, rounds, or substitutes a value itself.

On confirmation, `synthesize` includes `quote_id`. The backend verifies that all price-affecting and render-choice fields match the stored quote. A stale or mismatched quote returns `synthesis_quote_refresh_required`; the LLM must prepare and present a new quote instead of synthesizing.

This extra quote step is preferable to trusting prose plus an unbound
calculation because it guarantees that the reservation refers to the exact
estimate the LLM presented before requesting confirmation. The prompt must state that final consumption is
recalculated from actual generated audio length.

### 8.2 Prompt changes

Update `system_prompt.txt` to require a confirmation message with this structure:

```text
Estimated duration: …
Estimated vocal part (Soprano): X credits
Estimated all instrumentals: Y credits
Estimated total: Z credits
Available: N credits
Final credits are based on the generated audio length and may differ from this estimate.
```

Rules:

- Say “All instrumentals” so the user understands the charge is not per track.
- If no eligible instrumentals exist, show `All instrumentals: 0 credits`.
- If the uploaded score already paid the instrumental fee, show `All instrumentals: 0 credits (already generated)`.
- Describe all credit figures as estimates based on score tempo/measures and
  state that the successful charge is recalculated from actual generated audio
  duration. Recommended copy: `Final credits are based on the generated audio
  length and may differ from this estimate.`
- Use only the `prepare_synthesis_quote` result.
- The prompt requires the LLM to obtain explicit user confirmation before it
  calls `synthesize` with the quote.
- Any score version, repeat state, target part, lyric selection, voice, or price-state change invalidates the quote.

### 8.3 Confirmation trust boundary

Explicit confirmation remains prompt-enforced. The backend does not infer or
independently prove that the user consented, and the quote does not contain a
backend-maintained `confirmed` state.

When `synthesize` receives a `quote_id`, backend validation is limited to:

- quote ownership by the authenticated user/session;
- exact render-choice and active score-version matching;
- quote expiry/status and reservation validity; and
- prevention of cross-job quote reuse.

Passing those checks proves that synthesis is using a valid bound price and
render request; it does not prove user consent. The system prompt and
orchestration sequence are responsible for asking for and waiting for explicit
confirmation before the tool call.

Replace the current dynamic `Current synthesis estimate.estimated_credits` scalar with the shared structured estimate during migration. Once the quote tool is mandatory, the dynamic value remains useful for planning but is not sufficient authorization for synthesis.

## 9. Immutable quote persistence

Persist binding quotes in Firestore because credit reservation and the one-time instrumental claim also require transactional coordination.

Suggested document:

```text
synthesis_quotes/{quote_id}
```

Required fields:

```text
userId
sessionId
scoreId
scoreVersionNo
pricingVersion
renderChoicesHash
partId
expandRepeats
vocalDurationSeconds
instrumentalPricingDurationSeconds
instrumentalPricingExpandRepeats
vocalPartCredits
instrumentalCredits
totalEstimatedCredits
instrumentalChargeScope
instrumentalChargeRequired
status = quoted | reserved | consumed | expired
createdAt
reservedByJobId
```

The stored record is the source of truth for pricing and render-choice binding.
It is not a consent record. Do not accept a client/LLM-supplied numeric price.

`renderChoicesHash` uses a canonical JSON representation of the exact billable choices. The synthesis request is canonicalized and compared before reservation.

## 10. One-time instrumental charge state

Use a deterministic billing scope per user and uploaded score:

```text
instrumental_charge_scope = user_id / session_id / score_id
```

Persist one document per scope, for example:

```text
instrumental_generation_charges/{sha256(scope)}
```

State:

```text
unpaid (or document absent before the first vocal synthesis job)
pending { jobId, quoteId, estimatedCredits, estimatedExpandRepeats, estimatedDurationSeconds }
paid    { jobId, quoteId, actualCredits, pricingExpandRepeats, actualDurationSeconds, paidAt }
```

### 10.1 Job billing-component set

Every quote fixes one immutable set of billable components:

```text
vocal-only job:  billing_components = { vocal }
combined job:    billing_components = { vocal, instrumental }
```

A job is combined only when the uploaded score has eligible instrumentals and
its upload-scoped instrumental fee is still unpaid when the quote is prepared
and reserved. If there are no eligible instrumentals, or the uploaded-score
instrumental scope is already paid, the new job is vocal-only. MIDI work may
still run for a paid revision, but it is not a billable component of that job.

The job reservation stores subrecords only for components in its fixed set:

```text
components.vocal = {
  status: pending | settled | released,
  estimatedCredits,
  actualCredits,
}

components.instrumental = {             # combined jobs only
  status: pending | settled | released,
  chargeScope,
  estimatedCredits,
  actualCredits,
}
```

Vocal-only jobs do not create an instrumental payment component, claim, or
status snapshot. They may read the durable upload-scoped instrumental record to
determine that no fee is due, but reservation, settlement, rollback,
cancellation, and shutdown must not write that record.

For combined jobs, vocal and instrumental transitions occur together in the
same atomic transaction. There must be no successful partial state in which one
component is reserved, settled, or released while the other is not.

| Event | Billing set | Job vocal status | Job instrumental status | Durable instrumental scope |
|---|---|---|---|---|
| Reserve first job for unpaid upload | vocal + instrumental | `pending` | `pending` | `pending`, owned by this job |
| Combined job succeeds | vocal + instrumental | `settled` | `settled` | `paid` |
| Combined job fails/cancels | vocal + instrumental | `released` | `released` | pending claim removed; unpaid |
| Reserve later job after instrumentals paid | vocal only | `pending` | absent | remains `paid`, untouched |
| Vocal-only job succeeds | vocal only | `settled` | absent | remains `paid`, untouched |
| Vocal-only job fails/cancels | vocal only | `released` | absent | remains `paid`, untouched |

Reservation must atomically:

1. Read the immutable quote.
2. Reject expired, mismatched, or consumed quotes. A `reserved` quote continues
   to the same-job idempotency check below.
3. Read the instrumental charge document when the quote contains an instrumental fee.
4. Reject a stale quote if the scope is already `paid` but the quote includes an
   instrumental fee.
5. If the quote and credit reservation are already pending for the same user,
   `job_id`, and `quote_id`, verify that their stored amount, component
   breakdown, render-choice hash, and, for a combined job, instrumental claim all
   match, then return `already_reserved` (“already reserved successfully”)
   without reserving credits or creating a second claim.
6. Reject reuse of a reserved quote or pending claim by a different job.
7. Reject or serialize when a different job owns the instrumental `pending`
   claim.
8. Reserve exactly `quote.totalEstimatedCredits`.
9. Mark the quote `reserved` by this job for every job type.
10. For a combined job, mark the instrumental scope `pending` for the same job,
    retaining the estimated repeat mode/duration for audit purposes.

Steps 3, 4, 7, and 10 apply only to combined jobs. Step 9 is unconditional: a
vocal-only job must still consume its quote by marking it reserved. For a
vocal-only job, reservation creates and reserves only `components.vocal`; it
performs no write to the instrumental charge document.

This prevents two simultaneous first vocal takes from both charging the one-time instrumental fee.

On successful settlement of a combined job, calculate both actual component
costs from actual generated audio duration and mark the scope `paid` in the same
Firestore transaction that settles both components and publishes the completed
job. The successful job's repeat mode and actual duration become the permanent
pricing record for that uploaded score. On combined-job failure, release both
components and remove the pending instrumental claim; a retry estimates using
its own selected repeat mode. A vocal-only job never performs either scope
transition.

## 11. Reservation, settlement, and release

### 11.1 Reservation

Replace the orchestration-time numeric recalculation with:

```text
reserve_synthesis_quote(user_id, job_id, quote_id, ...)
```

The reservation stores the full breakdown:

```text
quoteId
pricingVersion
estimatedCredits = totalEstimatedCredits
vocalPartCredits
instrumentalCredits
instrumentalChargeScope
vocalDurationSeconds
expandRepeats
instrumentalPricingDurationSeconds
instrumentalPricingExpandRepeats
components.vocal.status
components.vocal.estimatedCredits
components.instrumental.status             # combined jobs only
components.instrumental.estimatedCredits   # combined jobs only
```

Insufficient-credit responses return the same breakdown and total, not only one scalar.

Reservation is idempotent for transport retries. If the transaction committed
but its response was lost, retrying `reserve_synthesis_quote()` with the same
authenticated user, `job_id`, and `quote_id` returns an explicit
`already_reserved` success result after verifying all stored fields. It performs
no balance mutation and creates no additional instrumental claim. The same
quote presented by another job is rejected.

### 11.2 Successful charge

For synthesis jobs, `settle_credits_and_complete_job()` (or a
synthesis-specific replacement) must call
`calculate_synthesis_credit_breakdown()` using the actual generated vocal-audio
duration:

```text
actual breakdown = calculate_synthesis_credit_breakdown(
    duration_seconds = actual generated vocal-audio duration,
    include_instrumentals = reservation included instrumental component,
)
actualCredits = actual vocal credits + actual instrumental credits
```

The quote and reservation remain immutable estimate records, but they are not a
fixed final price. Settlement stores both estimated and actual breakdowns.

Settlement follows the job's fixed billing-component set:

- Combined job: calculate both actual component costs, settle both component
  statuses together, change the job-owned upload scope from `pending` to `paid`,
  and apply the combined balance/ledger mutation in one atomic transaction.
- Vocal-only job: calculate and settle only the vocal component and vocal
  balance/ledger amount. Do not update the instrumental charge document.

The settlement function must not infer an instrumental transition merely
because the score has MIDI tracks or a durable instrumental scope exists. Only
membership in this reservation's `billing_components` authorizes that write.

If actual cost is lower than reserved, settlement releases the unused reserved
credits. If actual cost is higher, preserve the existing overdraft/extra-charge
semantics and record the delta. The confirmation message must clearly call the
quoted amount an estimate.

Export-mix settlement continues using its own pricing path.

### 11.3 Failure release

All synthesis/startup/cancellation failure paths continue to call the existing
`release_credits()` by job ID. Extend that function's existing transaction to
follow the job's fixed billing-component set:

- Combined job: release vocal and instrumental reserved amounts together, mark
  both component statuses `released`, and remove the job-owned pending
  instrumental claim in one atomic transaction.
- Vocal-only job: release and mark only the vocal component. Do not read or
  write instrumental payment status as part of rollback.
- Return the sum of amounts released for the job's component set.

Repeated release calls remain idempotent. Because MIDI failure occurs before
vocal synthesis, no actual-duration recalculation is attempted.

Examples:

- A later vocal-only take fails after instrumentals were paid by an earlier job:
  release the later job's vocal reservation only; there is no instrumental
  component on that job, and the instrumental scope remains untouched.
- A first, unpaid synthesis fails during MIDI generation: release that job's
  vocal reservation and its pending instrumental reservation/claim; the
  instrumental scope returns to unpaid.
- A first synthesis stages MIDI successfully but vocal generation later fails:
  release both components owned by that job and clean up unpublished artifacts.
- A paid score revision needs MIDI regeneration and that regeneration fails:
  release only the current vocal reservation; do not change the previously paid
  instrumental scope.

MIDI generation must be registered in the existing tracked-job lifecycle so
normal cancellation and process shutdown run the same billing-finalization and
rollback path. No new lease, heartbeat, watchdog, or recovery worker is added.
Existing settlement/finalization retries read the stored quote/reservation and
must never rerun the estimator against possibly changed score/session state.

## 12. MIDI artifact publication and billing boundary

Billable MIDI artifacts must not become an unpaid durable success.

Recommended flow:

1. Reserve the immutable quote for every job. If `instrumental` is in the job's
   `billing_components`, also claim the instrumental scope for that job.
2. Generate written and expanded MIDI into job-scoped staging paths.
3. Generate the vocal audio.
4. Move/publish the MIDI artifacts to their final paths and prepare the complete
   MIDI metadata payload while the job remains non-terminal and invisible to
   the UI as a success.
5. Atomically settle every component in the job's fixed `billing_components`,
   persist the prepared MIDI metadata, and mark the job successfully completed.
   Mark the instrumental scope `paid` only when `instrumental` belongs to that
   component set; a vocal-only job performs no instrumental payment-state write.
6. Expose successful completion through the existing progress poll. Its terminal
   payload includes the compact existing `PerformanceMidi` object as
   `performance_midi`; the UI creates/updates instrumental tracks directly from
   that payload. No additional API request is made.

If step 4 fails, fail the job and release its complete reservation: both
components for a combined job, or vocal only for a vocal-only job. If the
transaction or response in step 5/6 is interrupted, use the existing idempotent
billing-finalization retry path; do not charge a second time. The UI must never
observe successful completion before the published MIDI URLs and metadata are
ready and readable from the authoritative session/job state. Unexposed
staged/final files from a job that rolls back are cleaned up by the existing job
cleanup path; their presence alone never marks the upload paid or MIDI-ready.

Do not set the score-version “MIDI generated” marker, expose the MIDI metadata,
or return a successful terminal state before the billing outcome is known.
Otherwise a failed job can release the instrumental credits while leaving
future synthesis believing the artifact was already generated and paid.

## 13. Behaviour across score and repeat changes

- Every upload/reupload creates a new `score_id` and a new unpaid instrumental
  scope, including upload of byte-identical content.
- Reparse/revision preserves the existing `score_id` and instrumental payment
  state. Lyric changes, solfege changes, vocal-line preparation, preprocessing,
  and MIDI regeneration do not create a new fee.
- Preprocessed/derived score: price the active derived score and its deterministic instrumental eligibility.
- Before instrumentals are successfully charged, the current **With Repeats**
  value supplies the instrumental estimate.
- MIDI failure releases the job's fixed billing-component set: both components
  for a combined job, or vocal only for a vocal-only job. It does not lock a new
  instrumental pricing basis because no actual vocal audio was generated, and
  it never changes an instrumental scope that was already paid.
- The repeat mode of the first successfully generated vocal take determines
  the actual one-time instrumental charge for the uploaded score.
- Toggle after instrumentals are paid: instrumental component remains 0; vocal estimate changes with duration.
- A revision may change the number of instrumental tracks and regenerate MIDI;
  an already-paid uploaded score remains paid and is not charged again.
- Existing paid uploaded score: never charge the instrumental component again,
  even when another vocal part/verse/voice is synthesized.

## 14. API and schema changes

### Backend responses

Add `synthesis_credit_estimate` to the estimate endpoint response and optionally to upload/reparse/chat-progress payloads when already available.

Extend completed progress/job metadata with:

```json
{
  "credit_breakdown": {
    "estimated": {
      "vocal_part_credits": 5,
      "instrumental_credits": 2,
      "total_credits": 7
    },
    "actual": {
      "duration_seconds": 119.8,
      "vocal_part_credits": 4,
      "instrumental_credits": 1,
      "total_credits": 5
    },
    "quote_id": "...",
    "pricing_version": 1
  },
  "performance_midi": {
    "version": 1,
    "instrumental_parts": [
      {
        "part_index": 1,
        "part_id": "P2",
        "raw_part_id": "P2",
        "label": "Piano",
        "eligible": true,
        "has_lyrics": false,
        "midi_program": 1,
        "percussion": false
      }
    ],
    "has_instrumental_parts": true,
    "original_midi_available": true,
    "expanded_midi_available": true,
    "diagnostic": null
  }
}
```

Retain `consumed_credits` as the total for backward compatibility.

The MIDI readiness and delivery contract is:

- Persist the existing `score_summary.performance_midi` / `PerformanceMidi`
  object before setting the job status to `completed`; do not create a parallel
  metadata model.
- Store that same compact object on the completed job as `performanceMidi`; the
  progress serializer exposes it as `performance_midi`.
- Set availability flags only after their corresponding session MIDI asset can
  be fetched.
- `ProgressResponse` adds `performance_midi?: PerformanceMidi`.
- When the terminal progress payload is `done`, the UI calls
  `setPerformanceMidi(payload.performance_midi)` before ending the active
  progress state. Existing player state/effects then construct the instrumental
  tracks and fetch the selected written/expanded MIDI asset.
- No new metadata endpoint or completion-triggered refresh request is required.

### MCP schemas

- Add `prepare_synthesis_quote` input/output schemas.
- Add required `quote_id` to orchestrated `synthesize` calls.
- The public handler must reject missing/stale/mismatched quote IDs in billable session flows.
- Internal/local direct synthesis without account billing may use an explicitly separate non-billable execution path; it must not silently bypass quote validation in production app flows.

### Frontend types

Add types for the estimate breakdown and request function. Remove frontend `Math.ceil(duration / 30)` billing logic.

## 15. Component and module change list

| Component/module | Required change |
|---|---|
| `src/backend/synthesis_pricing.py` (new) | Define pricing constants, normalization, `SynthesisCreditBreakdown`, `SynthesisCreditEstimate`, serialization, the shared arithmetic primitive, and its estimate wrapper. |
| `src/backend/credits.py` | Keep vocal compatibility wrapper; add idempotent quote-based reservation for the same user/job/quote; reject cross-job reuse; persist the immutable job billing-component set; make combined jobs reserve/settle/release vocal and instrumental together atomically; make vocal-only jobs reserve/settle/release vocal only with no instrumental payment-state write; write prepared `performanceMidi` into the completed job transaction; preserve lower-actual release and higher-actual overdraft semantics; leave export-mix billing unchanged. |
| `src/backend/llm_prompt.py` | Replace scalar estimate with structured shared-estimator output; include one-time instrumental state. |
| `src/backend/config/system_prompt.txt` | Require quote-tool use, component breakdown, total, and explicit confirmation before synthesis; forbid arithmetic by the LLM. Confirmation remains prompt-enforced. |
| `src/backend/orchestrator.py` | Add quote-tool execution/follow-up; validate quote binding before synthesis without claiming to verify consent; replace local estimate calculation; include MIDI generation in the existing tracked-job cancellation/finalization flow; prepare published MIDI metadata before atomic successful completion; reuse stored quote in existing retries. |
| `src/backend/job_store.py` and session storage implementation | Persist quote/breakdown references and prepared MIDI publication state needed for idempotent completion; serialize completed-job `performanceMidi` as progress `performance_midi`. No new recovery worker. |
| `src/backend/main.py` / API routes | Add authenticated synthesis-estimate endpoint and return structured estimates. Keep the existing progress endpoint; do not add a metadata-refresh endpoint. |
| `src/backend/message_catalog.py` | Add breakdown-aware insufficient-credit, stale-quote, quote-in-progress, and MIDI-generation failure messages. |
| `src/mcp/tools.py` | Add `prepare_synthesis_quote`; add `quote_id` to synthesis contract for billable orchestration. |
| `src/mcp/handlers.py` | Route quote preparation to the shared pricing service; validate quote binding on synthesis. |
| `src/api/score.py` | Ensure both original/expanded duration and parse-time instrumental eligibility remain available on every upload/reparse/derived score. Do not calculate credits here. |
| `src/musicxml/instrument_programs.py` | No pricing formula; preserve the canonical eligible instrumental route list used by pricing. |
| `src/musicxml/performance_midi.py` | Support staged paths/idempotent publication; return generated eligibility/availability for consistency checks. |
| `ui/src/api.ts` | Add estimate response types and authenticated fetch function; add `performance_midi?: PerformanceMidi` to `ProgressResponse`. |
| `ui/src/MainApp.tsx` | Fetch estimates after parse/toggle/part/terminal changes; render vocal and instrumental lines; remove local credit arithmetic; handle loading/stale state; consume terminal `payload.performance_midi` and hydrate/update MIDI tracks without another request or manual reload. |
| `ui/src/styles.css` | Fit two estimate lines without wrapping header controls; add calculating/already-generated treatment if needed. |
| Analytics/reporting | Store each component's lifecycle state, estimated/actual credits, total, quote/pricing version, instrumental scope, ownership job, and actual duration for auditability. |

## 16. MIDI failure policy — accepted decision

MIDI generation is part of the synthesis job whenever the active revision needs
MIDI artifacts. This remains true when the upload-scoped instrumental fee was
already paid and the current revision is regenerating MIDI. If MIDI generation
fails:

- MIDI generation failure makes the whole synthesis job fail before vocal work begins.
- Release every component pending for this job. For an unpaid first job this is
  normally both vocal and instrumental; for a later job whose instrumental
  scope was already paid, release vocal only and preserve instrumental `paid`.
- Do not publish MIDI or vocal output.
- The user may retry. The next quote includes the instrumental component only
  when this uploaded-score scope is still unpaid; an already-paid scope remains
  free across the retry/revision.
- Remove the failed job's pending instrumental claim when one exists and is
  owned by that job. A retry for an unpaid scope estimates using its own
  selected repeat mode.

Vocal synthesis must not continue after a billable MIDI-generation failure.
This preserves the invariant that every failed job releases all of its own
pending amounts without modifying payment state owned by another component or
earlier job. A successful job charges the actual-duration result, which may
differ from the confirmed estimate.

## 17. Repeat-mode pricing — accepted decision

`_ensure_instrumental_midi_artifacts()` currently generates both written and expanded MIDI variants together. The earlier UI rule says the repeat toggle controls estimated duration and credits.

- The repeat mode selected by the first successfully generated vocal take for
  the uploaded score determines the one-time instrumental fee.
- Its score-derived duration supplies the estimate; its actual generated audio
  duration supplies the settled instrumental charge.
- If an attempt fails before vocal output exists, release its complete
  reservation and let the retry's repeat mode supply the next estimate.
- Both MIDI variants are generated and the uploaded score becomes paid.
- Later switching repeat mode does not add another instrumental fee.

This permits a lower first fee when the first successful vocal job uses written
order even though the expanded artifact is also generated. Conversely, a first
successful job with repeats uses the expanded take's actual audio duration.
Later vocal jobs use their own current repeat mode for the vocal component only.

## 18. Deployment and legacy-session policy — accepted decision

The MIDI generation feature has not been deployed to production, so there are
no production MIDI artifacts or uploaded-score charge scopes to migrate.

- Do not add a legacy waiver migration.
- Do not create `waived` instrumental charge state solely for this rollout.
- Development/test sessions created by pre-release builds may be discarded.
- Every production upload after deployment creates a new unpaid charge scope;
  reparses preserve that upload's payment state.

## 19. Test plan

### Pure pricing tests

- zero/negative/non-finite duration validation;
- exact 30 s and 120 s boundaries;
- millisecond normalization around boundaries;
- no instrumental routes gives 0 instrumental credits;
- 1 and many instrumental routes produce the same aggregate fee;
- paid scope produces 0 instrumental credits;
- total equals component sum.

### Quote and prompt tests

- quote result contains requested part and exact breakdown;
- LLM prompt instructs no arithmetic;
- confirmation message names vocal, instrumentals, and total;
- prompt/orchestration tests require an explicit user confirmation turn before
  the LLM calls `synthesize`;
- stale repeat/score/part/voice/lyric choices reject the quote;
- backend tests verify ownership, matching render choices, active score version,
  expiry/status, and reservation validity;
- backend tests do not assert that quote validation independently proves user
  consent, and no backend confirmation state is created.

### Reservation/settlement tests

- reservation amount equals quote total;
- reservation stores the immutable `billing_components` set and subrecords only
  for members of that set;
- combined reservation atomically creates both vocal and instrumental `pending`
  components plus the job-owned instrumental claim;
- vocal-only reservation creates only vocal `pending`; no instrumental component
  or claim is created and the instrumental payment document is not written;
- vocal-only reservation still changes the quote from `quoted` to `reserved`
  with `reservedByJobId` set to that job;
- successful combined settlement recalculates and settles vocal and
  instrumental together from actual generated audio duration;
- successful vocal-only settlement recalculates and settles vocal only, with no
  instrumental payment-state write;
- successful combined finalization marks the job-owned instrumental scope paid,
  while successful vocal-only finalization does not write that scope;
- settling a later vocal-only job leaves the earlier instrumental paid record,
  owner job, actual credits, repeat mode, duration, and paid timestamp unchanged;
- lower actual duration releases the unused reservation;
- higher actual duration preserves overdraft/delta accounting;
- combined-job failure atomically releases its pending vocal and instrumental
  amounts together and removes its own pending instrumental claim;
- later vocal-only failure releases only vocal and preserves the previously paid
  instrumental scope byte-for-byte;
- two concurrent first jobs cannot both claim/charge instrumentals;
- when reservation commits but its response is lost, retrying with the same
  user/job/quote returns `already_reserved` without another balance mutation or
  instrumental claim;
- a different job cannot reuse the reserved quote or pending claim;
- existing settlement/finalization retries do not recalculate pricing;
- second vocal take on the same paid uploaded score charges vocal only;
- lyric changes, solfege changes, vocal-line preparation, preprocessing,
  reparsing, and MIDI regeneration preserve the paid scope;
- a new upload creates a new unpaid scope even when its bytes match a previously
  paid upload;
- first successful written and expanded jobs settle their one-time
  instrumental component from their actual audio duration.

### Cancellation and shutdown tests

- `release_credits()` atomically releases both components for a combined job and
  only vocal for a vocal-only job;
- repeated release is idempotent;
- release never removes a claim owned by another job;
- shutdown of a vocal-only job cannot change an already-paid instrumental scope;
- cancellation during MIDI generation uses the existing tracked-job rollback;
- process shutdown during a combined job releases both component reservations
  and its pending instrumental claim together through the existing shutdown
  path;
- process shutdown during a vocal-only job releases vocal only and does not
  write instrumental payment state;
- no lease, heartbeat, watchdog, or recovery-worker behavior is introduced.

### MIDI failure tests

- MIDI failure prevents vocal synthesis;
- for a combined job, MIDI failure releases the complete vocal-plus-instrumental
  reservation together and removes that job's pending instrumental claim;
- for a vocal-only job regenerating already-paid MIDI, MIDI failure releases the
  vocal reservation only and leaves instrumental payment state untouched;
- no artifacts/URLs are published;
- retry of a failed combined job can claim the unpaid instrumental scope and
  succeed;
- publication failure before settlement fails the job and releases the
  reservation;
- an interrupted atomic finalization/response is idempotently retryable through
  the existing path without a second charge.

### API/UI tests

- acapella displays vocal and 0 instrumental credits;
- multi-instrument score displays one aggregate instrumental fee;
- before the first vocal job, the repeat toggle refreshes both components;
- after instrumentals are paid, the repeat toggle refreshes only the vocal
  component and instrumentals remain 0;
- completion refreshes instrumental estimate to 0/already generated;
- before job status becomes `completed`, the existing
  `score_summary.performance_midi` metadata is persisted and every declared
  available MIDI asset is fetchable;
- the first terminal `done` progress payload contains the same authoritative
  `performance_midi` object;
- the UI consumes that object and adds/updates instrumental tracks without a
  second metadata request or manual reload;
- stale estimate loading never shows locally calculated credits;
- insufficient-credit UI uses total, not vocal-only cost.

Do not rely only on E2E coverage. The pricing calculator, quote binding, Firestore transaction state machine, and UI response mapping all require focused unit/integration tests.

## 20. Implementation order

1. Add the pure calculator and boundary tests.
2. Add parse-time instrumental-presence resolver and estimate endpoint.
3. Replace frontend local arithmetic and update the two estimate labels.
4. Add quote persistence and `prepare_synthesis_quote`.
5. Update prompts and confirmation tests.
6. Add idempotent quote-based reservation and the uploaded-score one-time
   instrumental claim transaction.
7. Stage MIDI generation, extend `release_credits()`, and connect MIDI work to
   the existing cancellation/shutdown rollback.
8. Change synthesis settlement to recalculate the actual breakdown from actual
   generated audio duration using the shared calculator.
9. Prepare and persist published MIDI artifacts/metadata before atomic
   settlement exposes successful completion; add `performance_midi` to the
   existing terminal progress payload and consume it in the UI; add finalization
   idempotency without a new endpoint or recovery worker.
10. Add billing observability; no legacy migration is required.
11. Run focused billing/MIDI/prompt/UI tests, then an explicitly authorized E2E pass.

## 21. Out of scope

- Charging separately per MIDI track.
- Charging again for mixer instrument selection, mute/solo/volume changes, or browser playback.
- Changing browser real-time bounce/export-mix pricing.
- Using LLM inference to decide whether a part is billable.
- Deriving credit prices in TypeScript or prompt prose.
- Retroactively billing pre-feature MIDI artifacts.
