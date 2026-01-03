# Firebase Deployment Gap Analysis

## Goal
Deploy the current app to Firebase Hosting for a public demo using Gemini, with the FastAPI backend on a single GCE GPU VM. Use CORS on FastAPI (no Cloud Run proxy or load balancer).

## Current State (Assumed)
- Backend: Python FastAPI app with MCP orchestration, local file storage under `BACKEND_DATA_DIR`, running on a single GCE GPU VM.
- Frontend: Vite-based UI in `ui/`, deployed to Firebase Hosting.
- LLM: Gemini (API key via env).
- Audio: Generated server-side and stored as local files, returned via `/sessions/{id}/audio`.

## Key Gaps

### 1) Hosting Model
- **Gap**: Firebase Hosting serves static sites only. Backend lives on GCE.
- **Impact**: Cross-origin requests between `*.web.app` and the GCE API domain.
- **Work Needed**:
  - Expose FastAPI over HTTPS on the VM (Caddy/NGINX + TLS).
  - Configure FastAPI CORS to allow only Firebase Hosting origins.

### 2) Persistent Storage
- **Gap**: Backend writes session data + audio to local disk.
- **Impact**: Disk can fill; data is tied to a single VM.
- **Work Needed**:
  - Add TTL cleanup for old sessions/audio (cron/systemd timer).
  - (Optional) Move audio to Firebase Storage later for durability and sharing.

### 3) Auth + Rate Limiting
- **Gap**: No user auth/rate limits; public demo can burn Gemini credits.
- **Impact**: Abuse risk and cost spikes.
- **Work Needed**:
  - Add Firebase Auth (anonymous or email) and enforce user quota.
  - Add basic rate limiting (per user/session).
  - Optionally require a soft gating (invite code/feature flag).

### 4) Secrets Management
- **Gap**: API keys are loaded via env locally.
- **Impact**: Need secure secret storage on GCP.
- **Work Needed**:
  - Store Gemini API key in GCP Secret Manager or locked-down VM env vars.

### 5) GPU Split (Longer Term)
- **Gap**: Current backend does both orchestration + synthesis.
- **Impact**: Scaling GPU separately is not possible yet.
- **Work Needed**:
  - Split synth into a separate GPU service (GCE/Vertex). 
  - Queue or async job model (Pub/Sub or Cloud Tasks).
  - Backend returns “processing” state and polls for result.

### 6) Large Asset Management
- **Gap**: Voicebanks are local assets under `assets/voicebanks`.
- **Impact**: Shipping large assets in container increases size and cold start time.
- **Work Needed**:
  - Store voicebanks in GCS bucket.
  - Load on-demand or mount at startup (cache locally).
  - Add versioning for voicebanks.

### 7) CORS / Domain Routing
- **Gap**: Backend expects localhost-style calls.
- **Impact**: Browser calls will be cross-origin.
- **Work Needed**:
  - Configure CORS in FastAPI for Firebase Hosting domains.
  - Update UI `VITE_API_BASE` to point at the GCE API URL.

### 8) Observability
- **Gap**: Logs are local files.
- **Impact**: Need centralized logging for production troubleshooting.
- **Work Needed**:
  - Use Cloud Logging via stdout/stderr.
  - Add request tracing IDs in logs.

### 9) Build/Deploy Pipeline
- **Gap**: No CI/CD setup for deploy.
- **Impact**: Manual deploys are error-prone.
- **Work Needed**:
  - GitHub Actions or Firebase CLI deploy script.
  - Separate staging/prod configs.

## Minimal Path to Demo (Suggested)
1. Deploy backend to a single GCE GPU VM with HTTPS (Caddy/NGINX).
2. Enable FastAPI CORS for Firebase Hosting origins.
3. Set `VITE_API_BASE` to the GCE API URL and deploy frontend to Firebase Hosting.
4. Store Gemini key in Secret Manager or locked-down VM env vars.
5. Add basic auth/rate limiting and a TTL cleanup job for audio.

## Optional Enhancements
- Add async job queue for GPU synth service.
- Split inference into GPU Cloud Run or GCE with autoscaling.
- Add dashboard or usage tracking.

## Decisions Needed
- Which HTTPS termination to use on the VM (Caddy vs NGINX + certbot).
- Whether to keep audio on disk for demo or move to Firebase Storage.
- How strict to rate-limit demo users.
