# Backend Design Spec

This document expands on `backend_architecture.md` with concrete backend behaviors and data shapes.

## 1. Goals / Non-goals
- Goals: simple MVP backend, predictable session state, safe file handling, deterministic worker routing.
- Non-goals: multi-tenant auth, persistent DB, distributed job queue, autoscaling orchestration.

## 2. Session Model
`SessionState` (in-memory, guarded by an async lock):
```json
{
  "id": "uuid",
  "created_at": "iso8601",
  "last_active_at": "iso8601",
  "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "files": {
    "musicxml_path": "data/sessions/<uuid>/score.xml"
  },
  "current_score": { "score": "json", "version": 3 },
  "current_audio": {
    "path": "data/sessions/<uuid>/audio.wav",
    "duration_s": 12.3
  }
}
```

Session handling:
- TTL eviction (e.g., 24h since `last_active_at`), plus max session count guard.
- Per-session directory for uploads/outputs; never trust client file names.
- A single async lock per `SessionStore` to avoid races when updating history and outputs.

## 3. File Handling
- Upload size limit (e.g., 20MB configurable).
- Accept only MusicXML extensions (`.xml`, `.mxl` if supported).
- Normalize filenames to a fixed server-generated basename, store original name only as metadata.

## 4. Worker Lifecycle
- Spawn CPU/GPU workers at app startup.
- Health check with a lightweight RPC (e.g., `list_voicebanks`) on startup and on failure.
- For each request: apply timeout, retry once on transient failure, then surface error.

## 5. API Responses

### 5.1 `POST /sessions/{id}/chat`
Two high-level variants based on whether synthesis was invoked.

**Text response (no audio):**
```json
{
  "type": "chat_text",
  "message": "Sure, I softened the dynamics.",
  "current_score": { "score": "json", "version": 4 }
}
```

**Audio response (with audio):**
```json
{
  "type": "chat_audio",
  "message": "Here is the rendered audio.",
  "audio_url": "/sessions/<id>/audio",
  "current_score": { "score": "json", "version": 5 }
}
```

Notes:
- `current_score` is optional; include only when modified or requested.
- If the client needs to cache-bust audio, add `audio_id` or `audio_etag`.

### 5.2 `POST /sessions/{id}/upload`
Return parse summary to confirm success:
```json
{
  "session_id": "uuid",
  "parsed": true,
  "current_score": { "score": "json", "version": 1 }
}
```

## 6. Audio Serving
- Support HTTP `Range` requests for seekable playback.
- Send `Content-Type: audio/wav` (or appropriate codec).
- Consider `ETag` or `Last-Modified` for client caching.

## 7. Logging
- Structured logs for request id, session id, tool routing, duration, and errors.
- Keep tool payloads out of logs unless explicitly in debug mode.
