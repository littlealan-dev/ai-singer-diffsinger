# AI Singer Deployment Architecture (MVP CPU-first)

This document describes the CPU-first, stateless deployment for DiffSinger using Firebase and Cloud Run.
The design prioritizes low idle cost, simple operations, and clear scaling paths.

## Goals and Principles

- CPU-first inference with scale-to-zero behavior
- Stateless Cloud Run services; local disk is ephemeral
- Firebase Storage is the source of truth for files
- Firestore is the source of truth for job and credit state
- Worker logic is idempotent to tolerate retries
- Enforce cost controls (credits, limits, retries)

## High-Level Architecture

```mermaid
graph TD
    User((User)) --> FH[Firebase Hosting]
    FH --> SPA[React SPA]

    User --> FA[Firebase Auth]

    SPA -->|Upload MusicXML| FS[Firebase Storage]
    SPA -->|Create job| API[Cloud Run API]

    API -->|Verify ID token| FA
    API -->|Write job| DB[Firestore]
    API -->|Publish| PS[Pub/Sub Topic]

    PS -->|Push| WRK[Cloud Run Worker (CPU)]
    WRK -->|Read/Write| FS
    WRK -->|Update status| DB

    SPA <-->|Listen| DB
```

## Component Details

### 1. Frontend: Firebase Hosting
- React/Vite single-page app.
- Handles uploads, job creation, and playback.

### 2. Authentication: Firebase Auth
- Client obtains Firebase ID token.
- API service verifies token per request.

### 3. API Service: Cloud Run (api)
- Validates Firebase ID token.
- Checks and reserves credits before enqueueing jobs.
- Creates Firestore job records.
- Publishes job messages to Pub/Sub.
- Returns job status and signed URLs as needed.

### 4. Job Queue: Pub/Sub
- Topic: `render-jobs`.
- Message includes `jobId`, `userId`, `inputPath`, `renderType`, and optional metadata.
- Push subscription targets the worker service.
- Configure dead-letter topic for repeated failures.

### 5. Worker: Cloud Run (worker)
- Triggered by Pub/Sub push.
- Downloads MusicXML from Storage.
- Runs parsing, phoneme/preprocessing, and CPU DiffSinger inference.
- Writes audio outputs to Storage.
- Updates Firestore job status and error fields.
- Concurrency should be low (often 1) to avoid CPU oversubscription.

### 6. Storage: Firebase Storage (GCS)
- Input and output files are stored in a single bucket.
- Suggested paths:
  - `uploads/{uid}/{scoreId}.musicxml`
  - `jobs/{jobId}/intermediate/...`
  - `jobs/{jobId}/output.wav`

### 7. Metadata: Firestore
- `users/{uid}`: credit balance, plan, metadata.
- `jobs/{jobId}`: status, input/output paths, renderType, timestamps, errors.
- `sessions/{sessionId}` (optional): UI session state.

### 8. Observability
- Cloud Logging for API and worker.
- Error Reporting for uncaught exceptions.
- Alerts on failure rates or backlog growth.

### 9. Optional GPU Tier (Future)
- Separate worker service with GPU and a distinct queue.
- Only used for high-quality or final renders.

## Data Flow (Preview Render)

1. User uploads MusicXML to Firebase Storage.
2. SPA calls the API to create a job.
3. API validates auth, checks credits, writes a job record, and publishes a Pub/Sub message.
4. Worker receives the message, processes the job, and uploads audio output.
5. Worker updates Firestore job status to `completed` or `failed`.
6. SPA listens to Firestore to update UI and playback.

## Statelessness and Idempotency

- Worker verifies job status before processing.
- Use `jobId` as an idempotency key.
- If output already exists and job is completed, skip reprocessing.

## Security and IAM

- API verifies Firebase ID tokens.
- Service accounts use least-privilege roles:
  - `api-sa`: Firestore user, Storage object creator, Pub/Sub publisher.
  - `worker-sa`: Firestore user, Storage object admin, Pub/Sub subscriber.
- Storage rules ensure users can only access their own uploads and outputs.

## Cost Controls

- Require credits before enqueueing jobs.
- Limit max song length per render.
- Cap retry attempts and route failures to dead-letter.
- Clean intermediate artifacts after a TTL.

## Implementation Plan (Firebase + Cloud Run)

1. Firebase project setup
   - Create project, enable Auth, Firestore (native mode), and Storage.
   - Configure Hosting for the SPA.
   - Define Firestore and Storage security rules.

2. Schema and bucket conventions
   - Define `users`, `jobs`, and optional `sessions` collections.
   - Establish Storage paths for inputs and outputs.
   - Add Firestore indexes for job queries (by `userId`, `status`, `createdAt`).

3. Cloud Run services
   - Build container images for `api` and `worker`.
   - Configure env vars: `PROJECT_ID`, `FIRESTORE_DB`, `STORAGE_BUCKET`, `PUBSUB_TOPIC`.
   - Set timeouts (worker up to 60 minutes) and low concurrency.

4. Pub/Sub queue
   - Create `render-jobs` topic and a push subscription to the worker.
   - Set an acknowledgment deadline that fits typical render time.
   - Configure dead-letter topic and max delivery attempts.

5. IAM and service accounts
   - Create `api-sa` and `worker-sa`.
   - Bind least-privilege roles for Firestore, Storage, and Pub/Sub.
   - Assign service accounts to Cloud Run services.

6. Job lifecycle and credit flow
   - API reserves credit, writes a `pending` job, then publishes to Pub/Sub.
   - Worker marks `processing`, writes output, then marks `completed`.
   - On failure, set `failed` and surface an error message.

7. CI/CD and deployments
   - Use Cloud Build to build and deploy Cloud Run images.
   - Use Firebase CLI to deploy Hosting and rules.

8. Monitoring and alerts
   - Set up log-based alerts for failures and retries.
   - Add budgets and spend alerts in GCP.

## Notes

- Do not persist session data on Cloud Run filesystem.
- Treat Storage and Firestore as the authoritative sources of truth.
- Ensure worker behavior is safe to retry.
