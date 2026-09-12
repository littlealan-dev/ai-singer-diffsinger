# Preserve Historical Audio After Score Re-upload

Status: implemented locally on 2026-09-11; not deployed.

## 1. Scope

When a user uploads a replacement score in an existing chat session:

- the new score becomes the active workspace;
- the current player and multitrack workspace reset only after a successful upload;
- previous chat messages and completed job records remain;
- audio attached to an earlier message remains playable and downloadable;
- an expired playback URL can be renewed by any backend instance using durable Firestore and Cloud Storage records.

This change does not provide distributed job locking, worker recovery, leases, heartbeats, automatic retries, or cross-instance execution coordination. Those concerns are deferred to the planned Cloud Run Tasks architecture.

## 2. Durable Identity

Each upload receives a new `scoreId`. Updates to that score continue to use the existing `currentScoreVersion`. `currentJobId` is only the active workspace's progress pointer; it is not a lock.

The authoritative historical playback record is the existing Firestore job document:

```text
jobs/{jobId}
  userId
  sessionId
  status
  audioUrl
  outputPath
  actualDurationSeconds
  completedAt
```

Production output uses an immutable job-scoped Cloud Storage path:

```text
sessions/{userId}/{sessionId}/jobs/{jobId}/output.mp3
```

Score re-upload must not delete job documents or job-scoped storage objects. Artifact expiry and account deletion remain separate lifecycle operations.

## 3. Upload Behavior

The backend validates, parses, and stages the replacement score before publishing it. A successful upload:

1. publishes the new `scoreId` at version 1;
2. replaces active score files, summary, and score-specific settings;
3. sets `currentJobId` and `currentAudio` to null;
4. preserves chat history, job documents, prior score artifacts, and prior audio outputs.

A failed upload leaves the current score, player, and multitrack workspace unchanged.

## 4. Playback Renewal

Each audio chat message retains its `jobId` and job-specific progress URL. If its short-lived audio URL expires:

1. the UI calls authenticated `GET /sessions/{sessionId}/progress?job_id={jobId}`;
2. the backend verifies the session and Firebase user;
3. it reads the exact Firestore job by `jobId`, `userId`, and `sessionId`;
4. it derives the playback resource from that job's stored `outputPath`;
5. it returns a new short-lived URL signed for the exact user, session, filename, and storage resource;
6. the UI updates only the historical message and retries playback once.

The refresh path does not load the active score. The audio route streams the resource recorded in the signed token and does not consult `currentAudio`. Therefore the request can be handled by any instance and does not depend on instance memory or local files.

Production requires `BACKEND_USE_STORAGE=true`. All instances must use the same playback-token secret and secret version.

## 5. UI Isolation

After a successful replacement upload, earlier audio-message IDs are excluded from the new multitrack workspace. Renewing an old message's URL updates that message only. It must not:

- change the active score;
- set the old audio as current audio;
- add the old track to the replacement score's mixer;
- create a new synthesis job or charge credits.

## 6. Component Changes

| Component | Responsibility |
| --- | --- |
| `src/backend/main.py` | Preserve jobs on upload; provide exact-job progress refresh; sign and stream the job's durable output. |
| `src/backend/session.py` | Publish a replacement score without deleting history or artifacts; retain lightweight score/current-job identity. |
| `src/backend/job_store.py` | Retain immutable job ownership, input provenance, and output resource metadata. |
| `src/backend/playback_tokens.py` | Sign and verify user/session/file/resource claims using the shared secret. |
| `src/backend/orchestrator.py` | Upload output to durable storage before marking synthesis complete. |
| `ui/src/MainApp.tsx` | Keep job identity per message, renew expired URLs once, and isolate historical audio from the new mixer. |

The operation-token module, decorators, persisted operation state, worker handoff, and operation recovery tests are intentionally absent.

## 7. Compatibility

No migration or backfill is required. Reports continue reading old and new records using the paths and metadata actually stored. Historical records without new score provenance remain available with legacy uncertainty rather than being rewritten.

## 8. Acceptance Criteria

- Re-upload does not delete previous jobs or job-scoped audio.
- `/progress` is idle immediately after a successful upload.
- `/progress?job_id=J` returns the exact historical job owned by the user/session.
- Expired historical audio renews successfully from its Firestore job and Cloud Storage object.
- Storage-backed renewal succeeds with no corresponding local audio file.
- Another Firestore-backed backend instance can read the retained job after re-upload.
- Unauthorized or mismatched user/session lookups do not expose the job.
- Historical renewal does not repopulate the replacement score's mixer.
- No operation gate, lease, heartbeat, or recovery mechanism remains.

## 9. Verification

- Firestore and in-memory retention suite: `tests/test_score_job_history.py`.
- Direct upload, exact-progress, expired-token, and storage-backed playback cases: `tests/test_backend_api.py`.
- Browser regression is not rerun as part of this revision at the user's request.
