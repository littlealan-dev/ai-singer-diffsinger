# Audio Generation Feedback LLD (Improved)

## Overview

Add a lightweight user feedback prompt after successful audio generation. This improved version eliminates UI-blocking eligibility roundtrips by evaluating prompt candidacy server-side during job completion and piggybacking the result onto the completed audio response.

## Goals

- Collect job-level feedback for generated audio quality.
- Link each feedback record to the exact completed job that produced the audio.
- Avoid over-prompting users.
- Support creator-facing and product analytics later.
- **Provide a low-latency UX** by removing synchronous eligibility calls before displaying the prompt.

## Non-Goals

- No public feedback browsing UI.
- No user-to-creator messaging workflow.
- No moderation workflow in the first version.
- No email notification on feedback submission.
- No explicit dismiss tracking in V1. Closing or minimizing the feedback bubble is local UI state only.

## Feedback Fields

Structured rating aspects:

| Field | Scale |
| --- | --- |
| Voice quality | 1-5 |
| Pronunciation | 1-5 |
| Timing/rhythm | 1-5 |
| Lyrics alignment | 1-5 |
| Part splitting accuracy | 1-5 |

Free-form field:

- Optional text feedback.
- Recommended limit: 4000 characters.

Free-form text security:

- Treat `comment` as untrusted user input at every layer.
- Store the original plain text only after validation and normalization. Do not execute, interpolate, or evaluate it.
- Trim leading/trailing whitespace, normalize line endings, and reject control characters except normal whitespace such as newline and tab.
- Enforce the server-side length limit before writing to Firestore.
- Render comments as text content only. Do not render with `dangerouslySetInnerHTML`, markdown-to-HTML without sanitization, or any HTML interpretation.
- Escape or parameterize comments when exporting to CSV, querying analytics tools, logging, or sending to any downstream system.
- Do not concatenate comments into SQL, shell commands, log-query strings, BigQuery SQL, or LLM/tool prompts without escaping and clear data boundaries.
- For CSV exports, prefix values that begin with `=`, `+`, `-`, or `@` to reduce spreadsheet formula injection risk.

## Trigger Requirements

Show the prompt only when all of these are true:

1. The user has completed a successful audio generation job.
2. The user played or downloaded the generated audio.
3. The job was marked as `promptCandidate` by the backend during completion.
4. The user has not already submitted feedback for the just-completed job.
5. The user did not just submit feedback in the current UI session.

Backend Eligibility Rules (evaluated at Job Completion):
1. If the user has never been prompted, the first successful generation can become a prompt candidate.
2. After the first prompt, the user has not actually been prompted within `FEEDBACK_PROMPT_COOLDOWN_DAYS`.
3. After the first prompt, the user has at least `FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS` successful generations since the last actual prompt.

## Config Values

These values should be configurable per environment:

| Config | Default | Meaning |
| --- | --- | --- |
| `FEEDBACK_PROMPT_COOLDOWN_DAYS` | `5` | Minimum number of days between actually displaying feedback prompts to the same user. |
| `FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS` | `5` | Minimum successful generation count since the last actual prompt before a later completed job can become a prompt candidate. The first-ever prompt can be shown after the user's first successful generation. |

## High-Level Flow (Improved)

1. Synthesis job completes successfully.
2. Backend marks the job as `completed` and settles credits in a Firestore transaction.
3. **[NEW]** Within this same transaction, backend increments the user's successful-generation counter and checks feedback candidacy using `FEEDBACK_PROMPT_COOLDOWN_DAYS` and `FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS`.
4. **[NEW]** If candidate rules pass, backend sets `feedback.promptCandidate = true` on the job document. It does not reset prompt counters yet, because the user has not actually seen the prompt.
5. Frontend receives the completed audio payload through the existing job progress/audio response, with the feedback state included.
6. User plays or downloads the audio.
7. **[NEW]** Frontend instantly checks `job.feedback.promptCandidate`. If `true`, it renders the feedback chat bubble immediately.
8. **[NEW]** Frontend asynchronously sends `POST /feedback/prompted` to consume the prompt. This updates `lastPromptAt` and resets the generation counter.
9. User either submits or closes/minimizes.
10. Submit sends `POST /feedback` asynchronously. Close/minimize does not call the backend in V1.

## API Design

*Note: The previous `POST /feedback/eligibility` endpoint has been removed to eliminate eligibility roundtrips.*

### Mark Prompted

`POST /feedback/prompted`

Called once the frontend actually renders the feedback bubble after the user plays or downloads generated audio.

Request:

```json
{
  "jobId": "job_123",
  "trigger": "audio_played"
}
```

Response:

```json
{
  "status": "prompted"
}
```

Backend behavior:

- Verify `jobs/{jobId}.userId == auth.uid`.
- Verify `jobs/{jobId}.feedback.promptCandidate == true`.
- Idempotently return success if the job was already marked `prompted`.
- Set `jobs/{jobId}.feedback.prompted = true`, `promptedAt`, and `promptTrigger`.
- Set `users/{uid}.feedback.lastPromptAt = now`, `lastPromptJobId = jobId`, and `successfulGenerationsSinceLastPrompt = 0`.

### Submit Feedback

`POST /feedback`

Request:

```json
{
  "jobId": "job_123",
  "ratings": {
    "voiceQuality": 4,
    "pronunciation": 5,
    "timingRhythm": 3,
    "lyricsAlignment": 4,
    "partSplittingAccuracy": 5
  },
  "comment": "Timing was good, but the consonants were a little soft."
}
```

Response:

```json
{
  "status": "submitted",
  "feedbackId": "job_123"
}
```

Submit is idempotent by `jobId`. V1 can use `audio_feedback/{jobId}` as the document ID to prevent duplicate feedback for the same job.

Backend validation for `comment`:

- Accept only strings; reject objects, arrays, numbers, or other JSON types.
- Normalize to a bounded plain-text value before storage.
- Store `commentLength` after normalization.
- Do not strip harmless punctuation or non-English characters; the goal is safe handling, not content loss.

### Dismiss Prompt

Not included in V1. Close/minimize is local UI state only. Because `POST /feedback/prompted` already consumes the prompt when the bubble is shown, an explicit dismiss call is not required to prevent immediate re-prompting.

## Firestore Schema

### `audio_feedback/{feedbackId}`

One document per submitted feedback form.

```json
{
  "feedbackId": "job_123",
  "userId": "firebase_uid",
  "jobId": "job_123",
  "sessionId": "session_123",
  "ratings": {
    "voiceQuality": 4,
    "pronunciation": 5,
    "timingRhythm": 3,
    "lyricsAlignment": 4,
    "partSplittingAccuracy": 5
  },
  "comment": "Timing was good, but the consonants were a little soft.",
  "commentLength": 58,
  "createdAt": "2026-05-16T12:00:00Z",
  "client": {
    "appVersion": "optional",
    "userAgent": "optional"
  }
}
```

Notes:

- `feedbackId` should be equal to `jobId` in V1 for idempotency.
- `jobId` is required and should reference `jobs/{jobId}`.
- V1 stores only the job pointer. Reports can join from `audio_feedback.jobId` to `jobs/{jobId}`.
- A denormalized `jobSnapshot` can be added later if reports need immutable historical fields even when job/session/user metadata changes.

### `users/{uid}.feedback`

Small aggregate state used for fast eligibility checks during job completion.

```json
{
  "feedback": {
    "lastPromptAt": "2026-05-16T11:58:00Z",
    "lastSubmittedAt": "2026-05-16T12:00:00Z",
    "successfulGenerationsSinceLastPrompt": 0,
    "lastPromptJobId": "job_123",
    "lastSubmittedJobId": "job_123"
  }
}
```

Notes:

- Increment `successfulGenerationsSinceLastPrompt` when a synthesis job completes successfully.
- Do not reset it at job completion when a job becomes a prompt candidate.
- Reset it to `0` and update `lastPromptAt` only when the frontend actually displays the feedback bubble and calls `POST /feedback/prompted`.
- Submitting feedback updates `lastSubmittedAt` and `lastSubmittedJobId`, but does not need to reset prompt counters again because the prompt was already consumed.

### `jobs/{jobId}` Additions

Add enough metadata to join from feedback to the completed job, plus the new `promptCandidate` flag.

```json
{
  "jobKind": "synthesis",
  "completedAt": "2026-05-16T11:56:20Z",
  "analytics": {
    "songTitle": "Amazing Grace"
  },
  "feedback": {
    "promptCandidate": true,
    "prompted": true,
    "promptedAt": "2026-05-16T11:58:00Z",
    "promptTrigger": "audio_played",
    "submitted": true,
    "feedbackId": "job_123",
    "submittedAt": "2026-05-16T12:00:00Z"
  }
}
```

## Eligibility Logic (Backend Job Completion)

Pseudocode for `settle_credits_and_complete_job`:

```python
def check_and_update_feedback_candidate(transaction, user_ref, job_ref, user_data, job_id):
    now = utcnow()
    user_feedback = user_data.get("feedback", {})
    cooldown_days = config.FEEDBACK_PROMPT_COOLDOWN_DAYS
    min_successful_generations = config.FEEDBACK_PROMPT_MIN_SUCCESSFUL_GENERATIONS
    
    gens = user_feedback.get("successfulGenerationsSinceLastPrompt", 0) + 1
    last_prompt_at = user_feedback.get("lastPromptAt")
    last_submitted_at = user_feedback.get("lastSubmittedAt")
    has_prior_prompt_cycle = last_prompt_at is not None or last_submitted_at is not None
    required_generations = min_successful_generations if has_prior_prompt_cycle else 1

    is_candidate = False

    # Check cooldowns
    if gens >= required_generations:
        days_since_prompt = (now - last_prompt_at).days if last_prompt_at else 999
        days_since_submit = (now - last_submitted_at).days if last_submitted_at else 999
        
        if days_since_prompt >= cooldown_days and days_since_submit >= cooldown_days:
            is_candidate = True

    if is_candidate:
        # Mark this completed job as a prompt candidate. Do not reset user counters yet.
        transaction.update(job_ref, {
            "feedback.promptCandidate": True
        })

    transaction.update(user_ref, {
        "feedback.successfulGenerationsSinceLastPrompt": gens
    })
```

Pseudocode for `POST /feedback/prompted`:

```python
def mark_feedback_prompted(transaction, user_ref, job_ref, user_id, job_id, trigger):
    now = utcnow()
    job = job_ref.get(transaction=transaction).to_dict()

    if job.get("userId") != user_id:
        raise PermissionError()

    feedback = job.get("feedback", {})
    if not feedback.get("promptCandidate"):
        raise ValueError("job is not a feedback prompt candidate")

    if feedback.get("prompted"):
        return

    transaction.update(job_ref, {
        "feedback.prompted": True,
        "feedback.promptedAt": now,
        "feedback.promptTrigger": trigger,
    })
    transaction.update(user_ref, {
        "feedback.lastPromptAt": now,
        "feedback.lastPromptJobId": job_id,
        "feedback.successfulGenerationsSinceLastPrompt": 0,
    })
```

## Frontend Behavior

1. Each audio message knows its `job_id`.
2. The completed audio/progress payload includes feedback metadata:

```json
{
  "feedback": {
    "promptCandidate": true,
    "prompted": false,
    "submitted": false
  }
}
```

3. On first `play` or `download`, check `feedback.promptCandidate === true`, `feedback.prompted !== true`, and `feedback.submitted !== true`.
4. If true, render the feedback chat bubble immediately after that audio bubble.
5. Fire `POST /feedback/prompted` asynchronously.
6. If the user closes it, collapse to a minimized feedback bubble locally.
7. If the user submits it, fire `POST /feedback` asynchronously and collapse to a submitted state.
8. The frontend can update its local UI state immediately without waiting for the `POST` requests to complete.

## Security Rules

- Clients should not directly write `audio_feedback` or `users/{uid}.feedback`.
- All writes should go through backend endpoints (`POST /feedback/prompted`, `POST /feedback`) using Firebase-authenticated user context.
- Backend must verify `jobs/{jobId}.userId == auth.uid`.
- Backend must validate ratings are integers from 1 to 5.
- Backend must trim, normalize, validate type, reject unsafe control characters, and enforce max length on comment text.
- Any admin/reporting UI must render comments with text nodes or equivalent escaping.
- Any export/report script must protect against CSV/spreadsheet formula injection and use parameterized queries for external databases.

## Indexes

Recommended Firestore composite indexes:

| Collection | Query | Index |
| --- | --- | --- |
| `audio_feedback` | user feedback history | `userId ASC, createdAt DESC` |
| `audio_feedback` | job lookup | `jobId ASC` single-field is enough |
| `jobs` | completed synthesis exports | `jobKind ASC, status ASC, completedAt DESC` |

If a future version denormalizes `jobSnapshot` into `audio_feedback`, add report indexes such as `jobSnapshot.voicebankId ASC, createdAt DESC`.

## Privacy Notes

- Feedback may include user-written text. Treat it as user content.
- Do not expose feedback publicly without review.
- For creator sharing, prefer aggregated data or remove user identifiers unless explicit user-level reporting is required.

## Rollout Plan

1. Keep the current UI prototype behind local/dev-only behavior.
2. Add job analytics snapshot fields needed for feedback joins.
3. Update `settle_credits_and_complete_job` transaction to evaluate candidacy and set `feedback.promptCandidate`.
4. Add backend endpoints (`POST /feedback/prompted`, `POST /feedback`) and Firestore writes.
5. Switch frontend trigger from prototype-on-load to check `promptCandidate` on play/download.
6. Add export script/report after sufficient feedback data is collected.
