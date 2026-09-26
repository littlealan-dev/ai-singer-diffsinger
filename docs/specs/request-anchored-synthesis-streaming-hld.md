# Request-Anchored Synthesis Streaming

## 1. Status

Approved high-level design implemented by PR #8. The pull request also contains
separately approved usage-correlation changes where noted below.

## 2. Objective

Reduce the chance that Cloud Run scales down the `sightsinger-api` instance that is performing an in-process synthesis job because the HTTP request that started the job has already ended.

The streaming response is an execution-lifetime anchor and the normal delivery channel for synthesis progress and the terminal result. It does not replace the existing Firestore job record, which remains authoritative, or the existing progress endpoint, which remains the fallback after stream disconnection.

## 3. Problem Statement

The current `/sessions/{session_id}/chat` request starts synthesis as an in-process `asyncio` task and immediately returns a `chat_progress` JSON response. The synthesis task then continues after Cloud Run no longer sees the initiating HTTP request as active.

Cloud Run can consequently consider the instance idle even while its GPU subprocess is synthesizing audio. The instance may then be selected for normal autoscaler scale-in or scale-to-zero.

The current deployment settings are retained by this design:

```text
container concurrency: 2
maximum instances:     3
minimum instances:     0
CPU throttling:        disabled
request timeout:       900 seconds
```

## 4. Scope

This change will:

- keep the HTTP request that starts synthesis open until its synthesis task reaches a terminal outcome;
- send the existing `chat_progress` response as the first frame of that response;
- deliver Firestore-backed progress updates and the terminal result through that same response;
- send lightweight heartbeat frames during periods without a state update;
- await the exact in-process synthesis task created by that chat request;
- retain the existing progress endpoint as a fallback after stream disconnection without restarting synthesis; and
- close the streaming response after the synthesis task finishes.

This change will not:

- change confirmation, quoting, credit reservation, settlement, or release behavior;
- change input snapshot capture, job creation, synthesis, audio upload, or error handling order;
- change the current handling of multiple synthesis jobs on one instance;
- add synthesis admission limits, a durable queue, leases, automatic retry, or abandoned-job recovery;
- change Cloud Run concurrency, minimum instances, or maximum instances;
- require a Firestore schema migration for streaming; or
- replace the planned Cloud Tasks or Cloud Run Jobs architecture.

## 5. Current and Proposed Flows

### 5.1 Current Flow

```text
POST /sessions/{session_id}/chat
    |
    +-- run existing chat and synthesis-start orchestration
    |       |
    |       +-- existing confirmation and validation
    |       +-- existing job, input, and billing preparation
    |       +-- create synthesis task T
    |
    +-- return existing chat_progress JSON
    |
HTTP request ends
    |
task T continues in the background
```

### 5.2 Proposed Flow

```text
Browser sends one POST /sessions/{session_id}/chat
    |
    +-- Cloud Run routes that request to instance A
    |
    +-- run the same existing chat and synthesis-start orchestration
    |       |
    |       +-- existing confirmation and validation
    |       +-- existing job, input, and billing preparation
    |       +-- create synthesis task T
    |
    +-- instance A starts the streaming response to that same POST
            |
            +-- emit the existing chat_progress payload
            +-- emit Firestore-backed progress updates
            +-- emit heartbeats while state is unchanged
            +-- await task T
            +-- emit completed, failed, or action-required result
            +-- close the response
```

`POST /chat` and "start the streaming response" are not two backend calls. They are the request and response sides of one HTTP exchange. Cloud Run selects instance A once when the POST arrives, and instance A produces every response frame while it owns and awaits task `T`.

The design explicitly prohibits this two-call sequence:

```text
POST /chat       -> instance A starts synthesis and returns
GET /chat/events -> instance B opens an observer stream
```

That observer stream could be routed to another instance and would not keep instance A active. While the original POST stream is healthy, the browser makes no progress-polling requests. The existing progress endpoint is used only after the stream disconnects.

The behavioral change is limited to the response lifetime:

```diff
- Return chat_progress JSON and finish the HTTP request.
+ Send chat_progress as the first response frame.
+ Keep the same HTTP request open while delivering progress from Firestore.
+ Send the terminal result and close after task T reaches a terminal outcome.
```

## 6. Architectural Design

### 6.1 Request Ownership

The request that creates synthesis task `T` must also retain a stable reference to `T` and await it for the lifetime of the streaming response.

The stream must not rediscover the task later only through the existing per-session task dictionary. The current completion callback removes completed tasks from that dictionary, creating a race between task completion and stream attachment.

The orchestration layer should therefore return an internal started-job result containing:

```text
public chat_progress payload
job ID
stable completion future/task reference
```

Only the public payload is serialized to the browser. The task reference remains internal to the backend process.

### 6.2 Response Transport

Use one authenticated `fetch()` POST with a streaming response. The recommended response content type is `text/event-stream`.

The frontend must not make a second request to establish the anchor. The existing `POST /sessions/{session_id}/chat` request itself remains open, and its response body carries the accepted frame, progress updates, heartbeats, and terminal result. Returning a normal response and then opening `/events`, `/stream`, or another observer endpoint would not satisfy this design.

Native browser `EventSource` is not suitable because the existing operation is a `POST` with a request body and requires Firebase Authentication and App Check headers.

Streaming-capable clients should explicitly send:

```http
Accept: text/event-stream
```

Non-synthesis chat turns may continue returning their existing JSON response. A synthesis-starting turn returns a streaming response.

### 6.3 Stream Frames

The single response carries the complete normal synthesis lifecycle:

```text
accepted -> progress updates -> completed / failed / action-required -> close
```

The first frame contains the existing `chat_progress` result without changing its meaning:

```text
event: accepted
data: {
  "type": "chat_progress",
  "job_id": "...",
  "progress_url": "/sessions/.../progress?job_id=...",
  "message": "Give me a moment to prepare the take..."
}
```

Whenever the authoritative Firestore job state changes, emit a normalized progress frame using the same status, step, message, and progress semantics as the existing progress endpoint:

```text
event: progress
data: {
  "job_id": "...",
  "status": "running",
  "step": "prepare",
  "message": "Warming up the voice...",
  "progress": 0.05
}

event: progress
data: {
  "job_id": "...",
  "status": "running",
  "step": "synthesize",
  "message": "Rendering your take...",
  "progress": 0.55
}
```

During a period without a state change, send a heartbeat every 10 to 20 seconds:

```text
event: heartbeat
data: {"job_id":"..."}
```

When the Firestore job reaches a terminal state, emit the same normalized result that the progress endpoint would return. A successful result includes the signed audio URL and audio metadata:

```text
event: completed
data: {
  "job_id": "...",
  "status": "done",
  "progress": 1.0,
  "audio_url": "/sessions/.../audio?...",
  "audio_track": {...}
}
```

Failure and action-required outcomes are delivered similarly:

```text
event: failed
data: {
  "job_id": "...",
  "status": "error",
  "progress": 1.0,
  "message": "..."
}

event: action-required
data: {
  "job_id": "...",
  "status": "action_required",
  "progress": 1.0,
  "message": "...",
  "action_required": {...}
}
```

Event names describe the transport event, while payload statuses preserve the existing progress contract. In particular, `event: completed` carries `status: "done"`, `event: failed` carries `status: "error"`, and `event: action-required` carries `status: "action_required"`. The frontend must continue branching on the normalized payload status rather than deriving state from the event name.

Cancellation and credit-reconciliation terminal states must also produce a terminal frame so the response cannot remain open indefinitely. After sending one terminal frame, close the response.

### 6.4 Streamed State Delivery and Polling Fallback

After receiving the `accepted` frame, the frontend immediately applies the existing `chat_progress` UI behavior, but it does not start progress polling while the stream remains healthy. It then updates the same progress UI from subsequent stream frames and renders the terminal result from the final frame.

1. add the current progress message to the chat;
2. retain the returned job ID and progress URL;
3. consume progress updates from the original POST response;
4. render the terminal audio, failure, or action-required state from the terminal frame; and
5. close the stream reader after the terminal frame.

Heartbeat frames only maintain liveness and do not change visible progress.

If the stream disconnects before a terminal frame:

1. retain the accepted job ID and progress URL;
2. show a connection-recovery state without declaring synthesis failed;
3. start the existing progress polling flow for that same job;
4. recover progress or the terminal result from Firestore through the progress endpoint; and
5. never submit or restart synthesis as part of recovery.

Do not open a replacement lifecycle-anchor stream. A new request could reach another instance and cannot protect the original in-process task.

With Cloud Run concurrency set to two, the expected request allocation is:

```text
slot 1: synthesis POST carrying accepted, progress, and terminal frames
slot 2: ordinary API requests; fallback polling only after disconnection
```

Normal connected synthesis therefore uses one HTTP request slot on its instance.

### 6.5 Source of Truth

Firestore remains authoritative for all job state:

```text
synthesis task -> writes Firestore job state
anchor stream -> reads and delivers normalized Firestore job state
progress API   -> reads the same state only after stream disconnection
```

The stream must not independently calculate progress, settle credits, store output metadata, or make terminal-state decisions. It emits a new progress frame only after reading a changed authoritative job state. It should reuse the same progress-payload normalization and audio-URL signing behavior as the progress endpoint so streamed and fallback results cannot diverge.

The backend may observe the job document through a server-side listener or bounded internal reads. That observation occurs inside the original POST handler and does not create another browser-to-backend request.

### 6.6 Task Cancellation Boundary

The stream should await the synthesis task without allowing cancellation of the response consumer to propagate automatically into the paid synthesis task. Conceptually, the task is awaited behind a cancellation shield.

If the browser disconnects:

- the synthesis task continues under the current behavior;
- the job is not failed or refunded solely because the stream disconnected;
- no replacement synthesis is started;
- the frontend starts fallback polling for the accepted job ID; and
- the request-lifetime protection is lost because Cloud Run may no longer regard the stream as active.

Opening a new stream after disconnection cannot reliably restore protection because Cloud Run may route it to a different instance.

## 7. Component Responsibilities

| Component | High-level change |
|---|---|
| `src/backend/orchestrator.py` | Preserve the existing synthesis-start sequence while exposing an internal stable completion reference alongside the public `chat_progress` payload. Do not change queueing, billing, or synthesis behavior. |
| `src/backend/main.py` | When a streaming-capable chat turn starts synthesis, return one streaming response that emits accepted, Firestore-backed progress, heartbeat, and terminal frames while awaiting the exact synthesis task. Reuse existing progress-payload normalization and URL signing. Preserve ordinary JSON responses for non-synthesis turns. |
| `ui/src/api.ts` | Add a chat response reader that processes accepted, progress, and terminal frames without waiting for response closure. Start fallback polling only if the stream disconnects before a terminal frame. Keep the existing JSON path for non-streaming responses. |
| `ui/src/MainApp.tsx` | Apply accepted and progress frames through the current progress UI, render the final audio/failure/action-required result from the terminal frame, and invoke existing polling recovery only after premature stream disconnection. |
| Backend tests | Verify same-request task ownership, Firestore-backed frame delivery, heartbeat behavior, terminal payload parity with the progress endpoint, stream closure, disconnect isolation, and unchanged billing/job outcomes. |
| Frontend tests | Verify immediate accepted/progress rendering, terminal audio/failure/action-required rendering, no normal polling while connected, one request slot during normal synthesis, JSON compatibility, and fallback polling after premature disconnection. |

## 8. Lifecycle and Failure Behavior

| Event | Expected behavior |
|---|---|
| Synthesis completes | Existing completion and credit settlement finish; stream emits the normalized completed result with audio data and closes. |
| Synthesis fails | Existing failure and credit release/finalization finish; stream emits the normalized failed result and closes. |
| Synthesis requires user action | Existing action-required state and billing finalization finish; stream emits the normalized action-required result and closes. |
| Browser disconnects | Stream ends without cancelling or duplicating synthesis; frontend starts existing progress polling for the same job. |
| Cloud Run request reaches 900-second timeout | Stream ends and the frontend starts polling the same job. Synthesis is not restarted, cancelled, failed, refunded, or recharged solely because the stream timed out. |
| Fallback polling fails | Existing polling recovery behavior applies; synthesis is not restarted. |
| Cloud Run sends `SIGTERM` | Existing shutdown coordination, task cancellation, and billing cleanup remain responsible. |
| GPU OOM or process crash | Not solved by this design. |
| Forced infrastructure replacement | Not guaranteed to be prevented by this design. |

## 9. Cloud Run Effect

While the anchor stream remains connected, Cloud Run observes one active request on the same instance that created and owns the synthesis task. This materially reduces the chance that normal autoscaling treats that instance as idle and selects it for scale-in or scale-to-zero.

The design protects against normal idle scaling only while the request remains active. It does not guarantee instance survival during:

- deployment or explicit revision shutdown;
- infrastructure maintenance or forced replacement;
- process crashes or failed health checks;
- GPU or container memory exhaustion; or
- browser/network disconnection followed by idle scale-down.

No health-check request, separate progress stream, or session-affinity setting can substitute for awaiting the synthesis task inside the same request that created it. A separate observer request may be routed to another instance and would keep the wrong instance active.

## 10. Timeouts

The Cloud Run request timeout remains 900 seconds. The five-minute synthesis limit applies to estimated output-audio duration, not wall-clock processing time. Voicebank behavior, alignment, score complexity, model startup, output encoding, and storage or billing work can make a permitted song take longer than 900 seconds to finish.

The production frontend sets `VITE_API_BASE` to the `sightsinger-api` Cloud Run
service directly. The synthesis stream therefore does not traverse the Firebase
Hosting `/sessions/**` rewrite or its shorter request timeout. Deployments that
omit that direct API base do not satisfy this design.

A shorter song can therefore outlive the request. For example, a four-minute output may require 16 minutes of processing. At 15 minutes, Cloud Run closes the streaming request while the synthesis task may continue under the current in-process behavior.

This is an accepted limitation of the interim design. When the request deadline ends the stream:

1. the frontend treats the missing terminal frame as a stream disconnection;
2. it starts the existing progress polling flow using the accepted job ID and progress URL;
3. it observes the same Firestore job without submitting synthesis again; and
4. no cancellation, failure transition, reservation release, refund, settlement, or new charge occurs solely because the stream reached its request timeout.

If the original instance survives and synthesis finishes, fallback polling recovers the terminal result from Firestore. Once the stream closes, however, it no longer protects the synthesis instance from normal idle scale-down.

The frontend streaming reader must not apply the ordinary short API-response timeout after response headers and the accepted frame have arrived. It should instead use heartbeat liveness and explicit session/logout cancellation. Missing heartbeats trigger fallback polling, not a new synthesis request.

The backend should close the stream normally when synthesis finishes within the deadline. It must not interpret a request deadline as evidence that synthesis itself failed. This design does not increase the configured request timeout or the supported output-audio duration.

## 11. Security

- Firebase Authentication and App Check are validated when the chat request begins.
- No authentication or App Check token is included in stream frames.
- The stream exposes only the same job ID, progress URL, and message already returned by `chat_progress`.
- Firestore and progress APIs continue enforcing user and session ownership.
- Heartbeats contain no score, lyric, billing, or personal data.

## 12. Observability

Emit structured lifecycle logs without changing job state:

```text
synthesis_stream_opened session_id=<id> job_id=<id>
synthesis_stream_disconnected session_id=<id> job_id=<id> task_done=<bool>
synthesis_stream_draining session_id=<id> job_id=<id> task_done=<bool>
synthesis_stream_progress_read_failed session_id=<id> job_id=<id> task_done=<bool>
synthesis_stream_status_unconfirmed session_id=<id> job_id=<id> firestore_status=<status>
synthesis_stream_closed session_id=<id> job_id=<id> terminal_status=<status> task_done=<bool>
```

Production and standard development startup use structured JSON logging. Local
overrides must retain `LOG_JSON=1` when Gemini usage metadata needs to remain
queryable as structured fields.

Do not log tokens, stream headers containing credentials, score content, lyrics, or user email addresses.

Operational verification should correlate the anchor request's Cloud Run instance ID with the synthesis job logs to confirm that the active request and GPU work remain on the same instance.

## 13. Rollout

Recommended rollout:

1. Verify JSON compatibility and the streaming path locally and in automated tests.
2. Deploy the implementation normally.
3. Confirm request duration and instance correlation in Cloud Logging.
4. Confirm synthesis success, failure, action-required, and billing outcomes are unchanged.
5. Confirm that a healthy stream produces no progress-polling requests.
6. Confirm that forced stream disconnection activates the existing polling path without restarting synthesis.

## 14. Acceptance Tests

1. **Single-request lifecycle:** Start synthesis and verify that accepted, progress, and terminal frames are all received from one `POST /chat` response with no `/progress` request while the stream is healthy.
2. **Request ownership:** Verify that the open POST and GPU synthesis logs carry the same Cloud Run instance identity for the job lifetime.
3. **Progress delivery:** Publish preparing and rendering updates to Firestore and verify that the stream emits each normalized state once and in order.
4. **Heartbeat delivery:** Hold job state unchanged beyond the heartbeat interval and verify heartbeat frames without visible progress changes.
5. **Successful terminal result:** Complete synthesis and verify the stream emits the same signed audio result and metadata that the progress endpoint returns, then closes.
6. **Failed terminal result:** Fail synthesis and verify the existing billing release/finalization completes before the failed frame is emitted and the stream closes.
7. **Action-required result:** Produce an action-required outcome and verify the stream emits the existing normalized payload and closes without charging incorrectly.
8. **Disconnect fallback:** Disconnect after the accepted frame, verify synthesis is not cancelled or restarted, and verify the frontend begins polling the same job ID.
9. **No duplicate billing:** Verify stream delivery and fallback polling cannot create another job, reservation, settlement, refund, or charge.
10. **One-slot normal operation:** Verify normal synthesis uses one active HTTP request slot for accepted, progress, heartbeat, and terminal delivery.
11. **Autoscaler protection:** Verify Cloud Run records the original POST as active for approximately the synthesis duration and does not perform normal idle scale-in of that instance while connected.
12. **Request-timeout fallback:** Simulate the stream reaching its request deadline before synthesis finishes, verify the frontend polls the same job ID, and verify no restart or billing transition occurs solely because of the timeout.
13. **Normalized terminal statuses:** Verify `completed`, `failed`, and `action-required` events carry payload statuses `done`, `error`, and `action_required`, respectively, and drive the existing UI terminal branches.
14. **Compatibility:** Verify non-synthesis chat turns retain existing JSON behavior and multiple-job queueing behavior remains unchanged.
15. **Schema and configuration stability:** Verify streaming requires no Firestore migration or Cloud Run concurrency/minimum/maximum instance change. The optional `originatingTurnId` field belongs to the separately bundled usage-correlation work.

## 15. Residual Risk and Exit Strategy

This is an interim mitigation, not durable execution. It reduces normal idle scale-down risk but cannot recover in-process GPU state after a crash or forced replacement.

The long-term Cloud Tasks or Cloud Run Jobs architecture remains responsible for durable dispatch, execution ownership, retries, and recovery independent of a browser connection or API instance lifecycle.
