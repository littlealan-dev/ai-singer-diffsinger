# Deployment Architecture (Current MVP)

This document reflects the **current** deployment for SightSinger.ai.
The MVP is **API-driven**, CPU-first, and stateless.

---

## Goals

- Keep infra simple and low cost
- Scale to zero when idle
- Preserve state in Firebase (Storage + Firestore)
- Allow future worker/queue expansion without refactor

---

## Current Architecture

```
User
 ├─ Firebase Hosting (SPA)
 ├─ Firebase Auth + App Check
 └─ Cloud Run API (FastAPI)
       ├─ Firestore (jobs + sessions)
       ├─ Firebase Storage (inputs + outputs)
       └─ GCS voicebank tarballs (lazy cached to /tmp)
```

### Frontend
- Hosted on Firebase Hosting.
- `/demo` is fully client-side (no backend calls).
- `/app` uses the backend API.

### Backend (Cloud Run)
- Single API service (no worker queue in MVP).
- Handles upload, chat, synthesis, job progress, audio streaming.
- Stateless; all state is in Firestore/Storage.

**Current Cloud Run config**
- CPU: 4 vCPU
- RAM: 16 GB
- GPU: NVIDIA L4 x1
- Min instances: 0

### Storage
- Firebase Storage is the source of truth for inputs/outputs.
- Voicebanks are stored separately as tarballs in GCS and cached on first use.

Suggested paths:
```
sessions/{uid}/{sessionId}/input.xml
sessions/{uid}/{sessionId}/jobs/{jobId}/output.mp3
assets/voicebanks/{voicebankId}.tar.gz
```

### Firestore
- `sessions/{sessionId}`: session metadata
- `jobs/{jobId}`: job status, progress, output path
- Users and credits can be added later

---

## Statelessness

- Cloud Run filesystem is ephemeral.
- Voicebanks are cached to `/tmp`, but not a source of truth.
- Jobs are idempotent and retriable.

---

## Security

- Firebase Auth ID tokens validated on API.
- App Check enforced in prod.
- Secrets from Secret Manager (Gemini API key).

---

## Future Expansion (Optional)

When needed:
- Add Pub/Sub + worker service
- Separate CPU and GPU queues
- Add Redis cache for parsed scores

---

## Key Environment Variables

```
APP_ENV=prod
PROJECT_ID=...
STORAGE_BUCKET=...
VOICEBANK_BUCKET=...
VOICEBANK_PREFIX=assets/voicebanks
LOG_JSON=1
```
