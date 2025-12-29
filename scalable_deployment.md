# Scalable Deployment (Workload-Split)

This document describes a scalable deployment that separates light CPU steps from heavy GPU steps, allowing independent scaling and cost control.

---

## Services by Workload

**CPU Services (lightweight)**
- parse_score
- modify_score
- phonemize
- align_phonemes_to_notes
- save_audio (encode/base64 + persistence)

**GPU Services (heavyweight)**
- predict_durations
- predict_pitch
- predict_variance
- synthesize_audio (mel)
- vocoder stage (vocode)

---

## High-Level Architecture

```
User
  │
  ▼
Web/Mobile UI (React / React Native)
  │  upload MusicXML + prompt
  ▼
API Gateway / Backend Orchestrator
  │  ├─ Auth/Billing
  │  ├─ LLM tool planning
  │  └─ MCP client calls (router)
  ▼
MCP Tool Router (internal)
  │
  ├─ Score/Prep Service (CPU)
  │    - parse_score
  │    - modify_score
  │    - phonemize
  │    - align_phonemes_to_notes
  │
  ├─ Inference Service (GPU)
  │    - predict_durations
  │    - predict_pitch
  │    - predict_variance
  │    - synthesize_audio (mel)
  │
  ├─ Vocoder Service (GPU)
  │    - vocode
  │
  └─ Media Service (CPU)
       - save_audio (encode/base64)
       - file persistence + CDN URL
```

---

## Execution Flow (Hybrid Sync/Async)

1) UI uploads MusicXML + prompt.
2) Backend stores the file and requests tool planning from the LLM.
3) Backend executes tool calls via the MCP Tool Router.
4) CPU steps run synchronously.
5) GPU steps run as queued jobs (async), returning a job ID if needed.
6) Media Service writes audio, returns base64 and/or a URL.
7) UI renders a play button.

---

## Scaling Strategy

- **CPU services**: scale horizontally; fast startup; lower cost.
- **GPU services**: scale on queue depth; keep warm instances to avoid cold starts.
- **Vocoder service**: isolate from acoustic inference if model footprint differs.
- **Caching**: per-worker model cache to avoid repeated loading.
- **Storage**: object store for uploads and outputs (S3/GCS) + CDN for playback.

---

## Notes

- Voicebank IDs map to `assets/voicebanks/<id>`.
- Device selection stays internal to GPU services.
- The MCP Tool Router can be implemented as:
  - A single router service dispatching by tool name, or
  - Multiple MCP servers (CPU/GPU) with a proxy that merges tool lists.
