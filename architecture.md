# SightSinger.ai Architecture

## Purpose

SightSinger.ai is a full-stack MusicXML → singing workflow that combines:
- A React/Vite frontend (landing, demo, and studio)
- A FastAPI backend orchestration layer
- A DiffSinger pipeline for synthesis

The system supports both a real studio (`/app`) and a scripted demo (`/demo`) that requires no backend calls.

---

## System Overview

```
User
 ├─ Landing (/)
 ├─ Demo (/demo) ──> local MusicXML + pre-recorded audio
 └─ Studio (/app) ──> Cloud Run API ──> DiffSinger pipeline
                           │
                           ├─ Firebase Auth / App Check
                           ├─ Firestore (jobs + sessions)
                           └─ Firebase Storage (inputs + outputs)
```

---

## Key Components

### Frontend (`ui/`)
- **Landing**: marketing + CTA to demo.
- **Demo**: scripted UI with preset MusicXML and audio.
- **Studio**: full app, uploads MusicXML, renders score with OSMD, and runs synthesis.
- Score rendering uses **OpenSheetMusicDisplay**.

### Backend (`src/backend/`)
- **FastAPI** service for sessions, chat, uploads, and synthesis.
- Orchestrates synthesis requests, streams progress, writes job status to Firestore.
- Uses Firebase Auth and App Check when enabled.
- Uses **Gemini 3 Flash** for chat intent parsing and synthesis directives.

### MCP Server (`src/mcp_server.py`)
- Exposes pipeline APIs over stdio JSON-RPC for tool orchestration.
- Used by the backend to run synthesis as an MCP tool.

### DiffSinger Pipeline (`src/api/`)
- Parses MusicXML → phonemes → durations → pitch → variance → audio.
- Voicebank model discovery from local or cached GCS tarballs.
- Supports CPU and GPU (ONNX Runtime).

---

## Pipeline (MusicXML → Audio)

```
MusicXML
  └─ parse_score
      └─ phonemize + align
          └─ predict_durations
              └─ predict_pitch
                  └─ predict_variance
                      └─ synthesize_audio (vocoder)
```

Optional pitch/variance models fall back gracefully when absent.

---

## Voicebank Loading

Dev:
- Voicebanks live in `assets/voicebanks/<id>`.

---

## Storage + State

- **Firebase Storage**: MusicXML uploads + audio outputs.
- **Firestore**: job progress, session metadata, and audio references.
- Session state is **stateless** across Cloud Run instances.

---

## Technology Stack

| Layer | Tech |
|------|------|
| Frontend | React, Vite, OpenSheetMusicDisplay |
| Backend | Python, FastAPI, Firebase Admin SDK |
| Synthesis | DiffSinger ONNX models |
| Runtime | ONNX Runtime (CPU/GPU) |
| Hosting | Firebase Hosting |
| Storage | Firebase Storage (GCS) |
| DB | Firestore |
