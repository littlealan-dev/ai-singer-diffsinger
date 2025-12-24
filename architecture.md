# DiffSinger SVS Backend – Architecture Context

## Purpose

This project implements a backend-only Singing Voice Synthesis (SVS) pipeline based on:
- DiffSinger acoustic models (ONNX)
- An external neural vocoder

Immediate objective:
- Validate the end-to-end pipeline using the DiffSinger community **PC-HiFi-GAN** vocoder (non-commercial)

Future objective:
- Swap to a commercial-use vocoder later with minimal refactor
- Expose backend capabilities via an **MCP server** so an LLM-driven chat UI can call tools/APIs to achieve user goals

This document defines authoritative architectural decisions and constraints. Treat it as the source of truth.

---

## Target User Experience (Future)

We are aiming for an “AI image generator”-style workflow:
- User uploads a MusicXML file
- User provides a natural language prompt in a chat window (e.g., “sing it softly, sad, airy, with gentle vibrato”)
- An LLM interprets the prompt and orchestrates backend calls to synthesize the vocal audio

We will NOT build the UI in the current phase, but the backend architecture MUST be compatible with this future LLM-driven integration.

---

## High-Level Architecture

[Future Chat UI]
  - User uploads MusicXML
  - User enters natural-language prompt
        ↓
[LLM Orchestrator]
  - Interprets intent
  - Calls tools via MCP
        ↓
[MCP Server (Tool Wrapper)]
  - Stable tool/API surface for LLM
  - Validates inputs
  - Orchestrates underlying services
        ↓
[SVS Backend Services]
  - Preprocessing (MusicXML → phonemes/pitch/duration/etc.)
  - DiffSinger Acoustic Inference (ONNX Runtime → mel)
  - Vocoder (mel → waveform)
        ↓
[Outputs]
  - WAV audio
  - Optional intermediate artifacts (mel, alignment, logs)

  ---

## Core Decisions (Locked)

### Acoustic Model
- Engine: DiffSinger
- Model format: ONNX (often distributed as OpenUtau voicebanks)
- Runtime: onnxruntime (CPU first)
- OpenUtau is NOT used as a dependency/runtime (it is only a packaging format for some voicebanks)

### Phonemizer (Frontend)
- **Constraint**: Must use a G2P (Grapheme-to-Phoneme) module compatible with the specific DiffSinger/OpenUtau voicebank (usually the "DiffSinger Phonemizer" logic).
- **Reason**: Acoustic models are trained on specific phoneme sets (e.g., ARPABET, Pinyin). Generic phonemizers (like espeak) will fail unless the model was explicitly trained on them.
- **Implementation**: We must replicate the OpenUtau dictionary lookup and phoneme mapping logic (Text → Phoneme IDs).

### Vocoder (Phase 1 – Validation Only)
- Use DiffSinger community **PC-HiFi-GAN** vocoder to ensure compatibility and validate the pipeline
- Licensing is acknowledged as non-commercial; this phase is for technical validation only

### Vocoder (Phase 2 – Commercial-Ready)
- Design must allow easy switching to a commercial-use vocoder later (e.g., MIT HiFi-GAN, BigVGAN)
- **Critical Constraint**: The replacement vocoder MUST be trained/adapted to accept the exact **Mel Spectrogram Contract** (sample rate, hop size, n_mels, fmin, fmax) produced by the Acoustic Model.
- Switching vocoder should not require changes to:
  - DiffSinger ONNX inference code
  - Preprocessing pipeline
  - MCP tool interface (only configuration / implementation swaps)

### MCP Integration (Future)
- The SVS backend must be wrapped by an MCP server so an LLM can:
  - Discover available tools
  - Decide which tools to call and in what order
  - Pass user assets (MusicXML) and parameters (prompt-derived controls)
- The MCP server is the stable “contract boundary” between LLM and backend services

---

## Critical Design Principle: Isolation Boundaries

Two strict modular boundaries must be preserved:

1) Vocoder Isolation
- Vocoder is a pluggable module; no vocoder-specific assumptions inside the acoustic model code

2) MCP Tool Contract Isolation
- The MCP server provides stable tools that do not leak internal implementation details
- The LLM should not call internal services directly; it calls MCP tools only

---

## Vocoder Abstraction Contract

All vocoders must implement a stable interface:
- Input: mel spectrogram (numpy array)
- Output: waveform samples (numpy array)

Example interface (conceptual):

- infer(mel) -> waveform

Notes:
- Mel tensor layout and normalization MUST be consistent across vocoders via a single “Mel Contract”
- **Constraint**: The Mel Contract is dictated by the **Acoustic Model**. The Vocoder must be compatible with *these* specific physics parameters, or use a high-quality signal adapter (though matching training params is preferred).
- Any vocoder-specific mel scaling must be handled inside the vocoder adapter, not upstream

---

## MCP Server Responsibilities (Future)

The MCP server is a wrapper/orchestrator that exposes tools to the LLM. It should:
- Provide tool discovery and schemas (inputs/outputs)
- Validate parameters and file types
- Orchestrate calls to internal services
- Return structured results to the LLM

The MCP server is NOT responsible for:
- Training models
- UI rendering
- Long-running job management beyond basic request/response (future enhancement)

---

## Proposed MCP Tools (Draft)

These are draft tool concepts for future integration (exact naming may evolve):

1) register_asset
- Input: MusicXML file content or file reference
- Output: asset_id

2) parse_musicxml
- Input: asset_id
- Output: structured score representation (notes, lyrics, tempo, measures)

3) synthesize_mel
- Input:
  - asset_id OR structured score
  - optional performance controls (derived from user prompt)
- Output:
  - mel_id OR mel array (depending on storage strategy)
  - metadata (sample_rate, hop_length, n_mels, etc.)

4) vocode_audio
- Input: mel_id or mel array
- Output: wav_id or wav bytes + metadata

5) synthesize_audio (convenience)
- Input:
  - asset_id
  - performance controls
  - vocoder choice (config-driven)
- Output: wav_id / wav bytes

Important:
- Even if synthesize_audio exists, the lower-level tools must remain available to allow the LLM to debug/iterate (e.g., change only vocoder, or inspect mel).

---

## Current Implementation Plan

### Phase 1 (Now): End-to-End Validation
- Implement:
  - ONNX DiffSinger acoustic inference wrapper (onnxruntime)
  - Vocoder adapter for PC-HiFi-GAN
  - End-to-end pipeline runner (MusicXML preprocessing can be minimal/mock initially if needed, but aim to integrate properly)
- Goal:
  - Generate audible WAV from a known test MusicXML + voicebank

### Phase 2 (Later): Commercial Vocoder Swap
- Replace vocoder adapter with a commercial-use model
- Ensure:
  - Same Mel Contract
  - Minimal configuration-only changes
  - No changes to DiffSinger inference module

### Phase 3 (Later): MCP Server Wrapper + LLM Chat UI Integration
- Implement MCP server exposing the tool surface described above
- UI remains out-of-scope until MCP tools are stable
- LLM prompt interpretation will map natural language instructions to structured “performance controls” (future work)

---

## Technology Stack

- Language: Python 3.10+
- ONNX Runtime: onnxruntime (CPU first)
- Acoustic model: DiffSinger ONNX voicebanks
- Vocoder:
  - Phase 1: PC-HiFi-GAN (community, non-commercial)
  - Phase 2: commercial-use vocoder (MIT HiFi-GAN / BigVGAN)
- Interface layer (future): MCP server
- No UI code in current scope

---

## Non-Goals (Explicit)

- No model training
- No OpenUtau UI/editor dependency
- No real-time streaming (batch rendering only)
- No full-featured prompt-to-technique control yet (only placeholder schema is fine)
- No full UI build in this phase

---

## Future Extensions (Out of Scope for Now)

- Prompt → performance control mapping (LLM interpreter)
- Technique/emotion embeddings
- Multi-singer routing and voice selection
- Queueing, job persistence, and caching
- SaaS concerns (auth, billing, quotas)
- Voicebank marketplace ingestion/verification

---

## Summary

- Phase 1 uses PC-HiFi-GAN for compatibility validation only (non-commercial acknowledged)
- Architecture must keep vocoder swappable via a strict adapter boundary
- Future integration will be via an MCP server so an LLM can discover and call backend tools from a chat UI
- The MCP tool contract must be stable and implementation-agnostic
