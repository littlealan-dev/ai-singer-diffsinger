# Full-Stack Architecture

This document describes the full-stack flow for a chat UI that accepts a MusicXML upload + prompt, invokes the MCP server through an LLM orchestrator, and returns an audio player in the chat UI.

For backend-only details, see `architecture.md`.

---

## Components (Recommended Tech)

- **Web/Mobile UI**  
  - Role: Chat UI, file upload, audio playback  
  - Recommended: React (web) + React Native (mobile)

- **Backend API + MCP Client + LLM Orchestrator**  
  - Role: Session management, file storage, LLM calls, MCP tool execution  
  - Recommended: Python (FastAPI) or Node.js (Express); Python pairs best with the MCP server runtime

- **LLM Provider**  
  - Role: Interprets user intent, emits tool calls  
  - Recommended: OpenAI or Gemini SDK (server-side)

- **MCP Server (stdio JSON-RPC)**  
  - Role: Runs the SVS pipeline tools (parse/phonemize/infer/synthesize/save)  
  - Recommended: Python (this repo’s `src/mcp_server.py`)

- **Storage**  
  - Role: MusicXML uploads, generated audio, logs/metadata  
  - Recommended: Local disk in dev; S3/GCS + DB (Postgres) in prod

---

## High-Level Flow

1) **User** uploads MusicXML + prompt in the chat UI.  
2) **Backend** stores the file (e.g., `uploads/<id>.xml`) and sends prompt + tool list to the LLM.  
3) **LLM** proposes tool calls (e.g., `parse_score → modify_score → synthesize → save_audio`).  
4) **Backend (MCP client)** sends JSON-RPC requests over stdio to the MCP server.  
5) **MCP server** runs the pipeline and returns results (waveform/base64).  
6) **Backend** persists the audio and returns a chat response with an audio URL or base64.  
7) **UI** shows a message with a play button.

---

## MCP Client Responsibility

The MCP client lives in the **backend**. The frontend never talks directly to the MCP server.

Reasons:
- Keeps the MCP server off the public internet.
- Centralizes auth, rate limits, and file access.
- Simplifies secure access to local assets/voicebanks.

---

## Data Flow Diagram

```
User
  │
  ▼
Web/Mobile UI (React)
  │  (upload + prompt)
  ▼
Backend API
  │  (prompt + tool list)
  ▼
LLM Orchestrator
  │  (tool calls)
  ▼
MCP Client (Backend)
  │  (JSON-RPC over stdio)
  ▼
MCP Server (Python)
  │
  ▼
SVS Backend Pipeline (see architecture.md)
  │
  ▼
Audio Bytes (base64)
  │
  ▼
Backend API → UI → Play Button
```

---

## API Responsibilities

### Web/Mobile UI
- Upload MusicXML
- Provide prompt and display streaming responses
- Render audio player from URL or base64

### Backend API
- Store uploads and outputs
- Run LLM orchestration loop
- Own the MCP client
- Enforce permissions and rate limits
- Convert base64 audio to file or stream

### MCP Server
- Expose SVS tools via JSON-RPC
- Resolve voicebank IDs
- Keep device selection internal

---

## Example Orchestration Sequence

1) `tools/list` (discover MCP tools)
2) `parse_score(file_path="uploads/song.xml")`
3) `modify_score(score, code=...)` (optional)
4) `synthesize(score, voicebank="Raine_Rena_2.01", voice_id="soprano")`
5) `save_audio(waveform, output_path="outputs/song.wav")`

---

## Storage Layout (Example)

```
uploads/
  <session-id>/
    song.xml
outputs/
  <session-id>/
    song.wav
```

---

## Notes

- Voicebank IDs are directory names under `assets/voicebanks`.
- Device selection is a server startup option (not exposed to MCP).
- `save_audio` returns `audio_base64` for easy transport to the UI.
