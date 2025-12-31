# Backend Architecture Design

This document outlines the design for the API Backend, separate from the SVS pipeline logic.

## 1. System Overview

```
[Web/Mobile UI] <--> [FastAPI Backend] <--> [MCP Client Router]
                                                  |
                                      +-----------+-----------+
                                      |                       |
                                [CPU Worker]            [GPU Worker]
                                (Stdio/HTTP)            (Stdio/HTTP)
                                      |                       |
                                [Parse/Phonemize]       [Synthesize]
```

## 2. Component Design

### 2.1 FastAPI Backend (`src/backend/main.py`)
Exposes REST endpoints for the UI.
- **Framework**: FastAPI
- **Concurrency**: `async def` endpoints

### 2.2 Session Management (`src/backend/session.py`)
In-memory storage for the MVP.
- **Structure**:
  ```python
  sessions = {
      "session_id": {
          "history": [{"role": "user", "content": "..."}, ...],
          "files": {"song.xml": "path/to/file"},
          "current_score": {...},  # Last known JSON state
          "current_audio": "path/to/output.wav"
      }
  }
  ```
- **Notes**:
  - Implement as a small `SessionStore` with an async lock for concurrency safety.
  - Add TTL and eviction to avoid unbounded memory growth.
  - Store uploads and outputs in per-session subdirectories (UUID-based).
  - Enforce file size limits and sanitize user-provided filenames.

### 2.3 LLM Orchestrator (`src/backend/orchestrator.py`)
- **Role**:
  1. Receives user prompt.
  2. Injects system prompt with tool definitions.
  3. Calls LLM (Mock or Real).
  4. Parses tool calls.
  5. **Routes** tool calls to the appropriate MCP connection.
  6. Returns final response to UI.
- **Observability**:
  - Emit structured logs for tool routing, durations, and errors.

### 2.4 MCP Client & Router (`src/backend/mcp_client.py`)
Manages connections to the "Workers".
- **Implementation**: Spawns two subprocesses (simulating serverless services).
  - `worker_cpu`: `python -m src.mcp_server --mode cpu`
  - `worker_gpu`: `python -m src.mcp_server --mode gpu`
- **Routing Table**:
  - `cpu`: `parse_score`, `modify_score`, `phonemize`, `align_phonemes_to_notes`, `list_voicebanks`
  - `gpu`: `predict_durations`, `predict_pitch`, `predict_variance`, `synthesize_audio`, `synthesize` (convenience), `save_audio`
- **Lifecycle**:
  - Start workers on backend startup and restart on crash.
  - Enforce timeouts and retries on long GPU calls.
  - Provide a lightweight health check to verify worker readiness.

## 3. API Endpoints

### `POST /sessions`
Create a new session.
- **Response**: `{ "session_id": "uuid" }`

### `POST /sessions/{id}/upload`
Upload a MusicXML file.
- **Body**: Multipart file.
- **Logic**: Save to disk, parse immediately, update session state.

### `POST /sessions/{id}/chat`
Iterative chat interface.
- **Body**: `{ "message": "Make it softer" }`
- **Logic**:
  - Append msg to history.
  - Run Orchestrator loop.
  - Response shape depends on whether synthesis was invoked:
    - **Without audio**: text response and optional `current_score`.
    - **With audio**: text response plus `audio_url` (or `audio_id`) and optional `current_score`.

### `GET /sessions/{id}/audio`
Get the latest generated audio.
- Support HTTP `Range` requests for streaming/seek.

## 4. Refactoring `src/mcp_server.py`
Add a `--mode` flag to filter exposed tools.
- `--mode cpu`: Expose only parsing/text tools.
- `--mode gpu`: Expose only synthesis tools.
- `--mode all`: Default (expose everything).

## 5. Security
- LLM API keys stored in environment variables.
- Backend runs on localhost (for now).
- Enforce upload size limits and sanitize filenames to prevent path traversal.

## 6. Scalability
This design satisfies the requirement by decoupling the monolithic `mcp_server` into specialized workers. In production, these subprocesses would be replaced by HTTP URLs to Cloud Run instances.
Health checks, timeouts, and restart policies make the local worker model closer to a production deployment.
