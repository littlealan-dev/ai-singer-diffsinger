# Refinement: SIG-6 Voice Part Preprocessing Workflow & Enforcement

This document refines the technical requirements for [SIG-6-preprocess-tool-and-modify-score-lockdown.md](file:///Users/alanchan/antigravity/ai-singer-diffsinger/docs/tasks/SIG-6-preprocess-tool-and-modify-score-lockdown.md) based on architectural review.

## 1. Complete Decoupling of Preprocessing from Synthesis

The current `synthesize` API includes an internal call to `preprocess_voice_parts` as a convenience. To ensure clear role separation and avoid "black box" behavior, this will be refactored.

- **Change**: Remove the call to `preprocess_voice_parts` inside `src/api/synthesize.py`.
- **Result**: `synthesize` becomes a pure "Render/Inference" function. It assumes the provided `score` and `part_index` are already valid and "ready to sing."
- **Responsibility**: The AI Planner/Orchestrator or the UI must now explicitly call the `preprocess_voice_parts` MCP tool if the score analysis indicates splitting or lyric propagation is required.

## 2. Stateless Input Validation & Enforcement

To prevent invalid synthesis attempts (e.g., singing a raw SATB part without splitting), `synthesize` will implement strict validation based on score metadata.

### 2.1 Detection Logic
The synthesizer will determine if a part is "Ready to Sing" by comparing the request against the score's internal signals:

1. **Complexity Signal**: Check `voice_part_signals`. If the part has `multi_voice_part: true` or `missing_lyrics: true`, it is marked as "Preprocessing Required."
2. **Derived Part Detection**:
    - **Index Delta**: Compare the requested `part_index` against the original part count in `score_summary`. If `requested_index >= original_count`, it is a **Derived Part**.
    - **Metadata Key**: Check for the existence of the part ID in `voice_part_transforms`.
3. **Validation Rule**:
    - If **Preprocessing Required** is TRUE AND the requested part is **NOT Derived**, the API must return `status: "action_required"` with an error code like `preprocessing_required_for_complex_score`.

### 2.2 Statelessness
This enforcement is entirely stateless. The synthesizer does not need to know if an API was called previously; it simply looks at the `score` JSON payload provided in the current request. If the payload contains the necessary derived parts and signals, it proceeds. If it contains a raw, complex part, it refuses.

## 3. Workflow Sequence

The standardized MCP workflow becomes:
1. `parse_score` -> LLM sees complexity signals.
2. `preprocess_voice_parts` (if needed) -> LLM receives enriched score with `DERIVED` parts.
3. `synthesize` -> Pointed to the `DERIVED` part index.

---
*Reference: Review comments on SIG-6 Architecture Refactoring (2026-02-22).*
