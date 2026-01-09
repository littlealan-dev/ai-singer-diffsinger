# Performance + Statelessness Change List

This is the proposed change set for performance profiling, MCP lifecycle visibility,
and making progress tracking stateless.

## 1) Voicebank Lazy-Download Cache
- Decisions:
  - Lazy download on first use (no eager preload).
  - Cache on local disk (Cloud Run `/tmp`).
  - Store raw voicebank folders in GCS (no tar.gz).
- Backend config:
  - Add env vars:
    - `VOICEBANK_BUCKET` (GCS bucket name, use existing Firebase Storage bucket)
    - `VOICEBANK_PREFIX` (optional, default `assets/voicebanks`)
    - `VOICEBANK_CACHE_DIR` (default `/tmp/voicebanks`)
- Environment behavior:
  - `APP_ENV=prod`: only load from GCS; no local fallback if missing.
  - `APP_ENV=dev|local|test`: load from local `assets/voicebanks` (no GCS).
- Backend storage logic:
  - Add helper to resolve a voicebank path:
    - If local cache exists, use it.
    - Otherwise download from GCS into cache, then use it.
  - Download per voicebank (Raine_Rena_2.01, Raine_Reizo_2.01).
- Docker/build:
  - Stop copying `assets/voicebanks` into the image.
  - Update `.gcloudignore` to ignore large model assets now externalized.
- Deployment:
  - Add env vars to Cloud Run (prod env file).
  - Ensure Cloud Run service account has read access to the GCS bucket.

## 2) Timing Logs for Pipeline Steps
- Add per-step duration logs in `synthesize`:
  - align → durations → pitch → variance → synthesize_audio → postprocess
- Emit a summary log with total time and audio length.
- Use consistent log fields so Cloud Logging can filter by step name.

## 3) MCP Worker Lifecycle Logs
- Log MCP process startup time:
  - `mcp_start_begin`, `mcp_start_ready`, `mcp_start_ms`
- Log tool list latency at startup (`tools/list` duration).
- Log per-tool call duration (synthesize, parse, etc.).

## 4) Fix "Session Not Found" (Stateless /progress)
- Replace `/sessions/{id}/progress` local file read with Firestore-backed state.
- Use job document fields for:
  - `status`, `step`, `message`, `progress`, `audio_url`, `error`, `job_id`, `updated_at`
- Remove local progress file reads/writes; use Firestore only (dev uses emulator).

## Open Questions
- Final bucket name/path for voicebanks in GCS.
- Confirm GCS path layout under `assets/voicebanks` in the existing bucket.
- Whether to keep local progress file as a fallback for dev.
