# UI Audio API Contract

## Scope

This document describes the current backend contract for UI features that:

- send chat requests
- poll render progress
- play or download generated audio
- distinguish singing audio from backing-track audio

This is the current implementation contract, not a proposed redesign.

## Base Flow

For both singing renders and backing-track renders, the UI flow is:

1. call chat
2. receive a `chat_progress` response
3. poll the returned `progress_url`
4. when progress reaches `done`, use the returned `audio_url`

Important:

- always use the exact `progress_url` returned by chat
- do not reconstruct progress URLs manually
- do not reconstruct audio URLs manually

## Auth

### Chat and Progress

These endpoints require Firebase auth:

- `Authorization: Bearer <firebase_id_token>`

### Audio

The audio endpoint uses a short-lived playback token embedded in the returned `audio_url`.

The UI should not add auth headers to audio playback requests unless needed by the browser fetch path.

## Endpoints

### 1. Chat

`POST /sessions/{session_id}/chat`

Source:

- [main.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L290)

Request body:

```json
{
  "message": "generate a backing track in simple pop rock style",
  "selection": {
    "verse_number": 1
  }
}
```

Fields:

- `message: string`
- `selection?: object`

`selection` is optional and is used for structured UI choices such as verse selection.

Schema source:

- [ChatRequest](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L57)

#### Typical Response When A Render Starts

Singing example:

```json
{
  "type": "chat_progress",
  "message": "Give me a moment to prepare the take...",
  "progress_url": "/sessions/SESSION_ID/progress?job_id=JOB_ID",
  "job_id": "JOB_ID"
}
```

Backing-track example:

```json
{
  "type": "chat_progress",
  "message": "Let me put together the backing track...",
  "progress_url": "/sessions/SESSION_ID/progress?job_id=JOB_ID",
  "job_id": "JOB_ID"
}
```

Combined melody + backing example:

```json
{
  "type": "chat_progress",
  "message": "Let me create the combined melody and backing track...",
  "progress_url": "/sessions/SESSION_ID/progress?job_id=JOB_ID",
  "job_id": "JOB_ID"
}
```

Notes:

- the UI should persist `progress_url` on the corresponding chat bubble/message
- the `job_id` in that URL is important and should not be dropped

Progress URL creation:

- singing: [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L462)
- backing track: [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L697)

## 2. Progress Polling

`GET /sessions/{session_id}/progress?job_id={job_id}`

Source:

- [main.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L369)

Behavior:

- if `job_id` is provided, backend returns that exact job
- if `job_id` is omitted, backend returns the latest job for the session

UI requirement:

- always poll with `job_id`
- this prevents old singing messages from being overwritten by later backing-track jobs

### Normalized Progress Payload

Payload builder:

- [job_store.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py#L100)

Current payload fields:

```json
{
  "status": "done",
  "step": "done",
  "message": "Backing track ready.",
  "progress": 1.0,
  "audio_url": "/sessions/SESSION_ID/audio?file=backing_track.mp3&playback_token=...",
  "job_id": "JOB_ID",
  "job_kind": "backing_track",
  "details": {
    "metadata": {},
    "backing_track_prompt": "..."
  },
  "updated_at": "..."
}

Possible fields:

- `status`
- `step`
- `message`
- `progress`
- `audio_url`
- `error`
- `warning`
- `job_id`
- `job_kind`
- `review_required`
- `action_required`
- `details`
- `updated_at`

### Status Values

Current normalized values:

- `idle`
- `running`
- `done`
- `error`

Normalization rules are in:

- [job_store.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/job_store.py#L102)

## 3. Audio

`GET /sessions/{session_id}/audio?file={file_name}&playback_token={token}`

Source:

- [main.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L314)

The UI should use the exact `audio_url` returned by the progress payload.

The backend signs this URL automatically before returning it:

- [main.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L632)

Possible media types:

- `audio/wav`
- `audio/mpeg`

Backend file serving:

- [main.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/main.py#L353)

## How To Tell Singing vs Backing Track

Use `job_kind` from the progress payload.

### Backing Track

Backing-track jobs explicitly set:

```json
{
  "job_kind": "backing_track"
}
```

References:

- [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L569)
- [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L614)

### Combined Melody + Backing

Combined melody + backing jobs explicitly set:

```json
{
  "job_kind": "combined_backing_track"
}
```

### Singing Audio

Current singing jobs do not set a specific `job_kind` on the normal completion path.

So for the UI today:

- if `job_kind === "backing_track"`, treat as backing track
- if `job_kind === "combined_backing_track"`, treat as combined melody + backing output
- otherwise, if there is an `audio_url` for a completed render job, treat it as singing audio

There is currently no separate type flag on the `/audio` endpoint itself.

The distinction is made from `/progress`, not from the binary audio route.

## Recommended UI Logic

### Start Render

1. send chat request
2. if response `type === "chat_progress"`:
   - create/update the chat bubble
   - store `progress_url`
   - store `job_id`

### Poll Render

1. poll `progress_url`
2. render status/message/progress in UI
3. when `status === "done"`:
   - read `audio_url`
   - read `job_kind`
   - decide UI label:
     - `backing_track` -> backing track
     - `combined_backing_track` -> combined melody + backing
     - otherwise -> singing audio

### Play Audio

1. use `audio_url` directly as the player source
2. if playback later fails with expired token behavior:
   - re-fetch the same `progress_url`
   - get the refreshed `audio_url`
   - retry playback/download

## Example Payloads

### Singing Progress Complete

```json
{
  "status": "done",
  "step": "done",
  "message": "Your take is ready.",
  "progress": 1.0,
  "audio_url": "/sessions/SESSION_ID/audio?file=audio-123.wav&playback_token=TOKEN",
  "job_id": "JOB_ID"
}
```

### Backing Track Progress Complete

```json
{
  "status": "done",
  "step": "done",
  "message": "Backing track ready.",
  "progress": 1.0,
  "audio_url": "/sessions/SESSION_ID/audio?file=backing_track.mp3&playback_token=TOKEN",
  "job_id": "JOB_ID",
  "job_kind": "backing_track",
  "details": {
    "metadata": {
      "title": "Happy Birthday to You"
    },
    "backing_track_prompt": "Original instrumental backing track..."
  }
}
```

### Combined Melody + Backing Progress Complete

```json
{
  "status": "done",
  "step": "done",
  "message": "Combined backing track ready.",
  "progress": 1.0,
  "audio_url": "/sessions/SESSION_ID/audio?file=combined-backing-track-123.mp3&playback_token=TOKEN",
  "job_id": "JOB_ID",
  "job_kind": "combined_backing_track",
  "details": {
    "metadata": {
      "title": "Happy Birthday to You"
    },
    "backing_track_prompt": "Original instrumental backing track...",
    "render_variant": "combined",
    "reused_existing_backing_track": true
  }
}
```

## Important Implementation Notes

### 1. Keep The Original Progress URL Per Bubble

Do not replace an earlier singing message's `progress_url` with a later backing-track `progress_url`.

The backend now supports job-specific progress lookup specifically to preserve this.

Relevant behavior is tested in:

- [test_backend_api.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_backend_api.py#L2434)

### 2. Audio URL Is Short-Lived

The audio URL contains a short-lived playback token.

If it expires:

- refresh via the original `progress_url`
- do not cache the original `audio_url` forever

### 3. Use Progress Metadata For UI Labels

Do not infer backing track vs singing from filename alone.

Preferred rule:

- use `job_kind`

Fallback rule:

- if `job_kind` absent and a completed audio render exists, treat as singing

## Open Gap

For UI clarity, a future improvement may be to add an explicit value for singing jobs, for example:

- `job_kind: "singing"`

That is not in the current implementation.
