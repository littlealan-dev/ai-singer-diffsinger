# SIG-6 AI-Orchestrated Voice Part Workflow - Low-Level Design

## 1. Objective
Define implementable backend design for AI-orchestrated voice-part splitting and lyric propagation with optional section overrides.

This document is the low-level companion to:
- `docs/specs/SIG-6-ai-orchestrated-voice-part-workflow.md`

## 2. Scope
In scope:
1. Full-score analysis model for planning.
2. Plan contract for AI -> backend execution.
3. Deterministic execution engine for split + lyric propagation.
4. Validation and repair loop contract.
5. Non-destructive MusicXML write-back (append transformed part as new part).
6. Persistence of transformed score artifacts.
7. Integration into existing parse/synthesize orchestration.
8. Concurrency-safe append behavior for simultaneous requests.

Out of scope:
1. Frontend UX wording/prompt design.
2. Advanced automatic choral label inference beyond current naming baseline.
3. Full MusicXML rewrite in AI.

## 3. Existing Baseline
Current implementation already supports:
1. Voice-part analysis by part (`src/api/voice_parts.py`).
2. Target selection by `part_index + voice_part_id`.
3. Confirm flow for missing lyric propagation.
4. Source selection with `{source_part_index, source_voice_part_id}`.
5. Simple propagation by exact `offset_beats` match.
6. Persisted transform metadata (`voice_part_transforms`).
7. `synthesize()` currently calls `prepare_score_for_voice_part`.

Known limitations:
1. Part-level propagation behavior is coarse for sectionally mixed lyric ownership.
2. Propagation matching is strict onset-only.
3. No first-class section override execution contract.
4. Caller-score in-place mutation in current synth integration should be eliminated in final design.

## 4. Target Architecture
Components:
1. `FullScoreAnalyzer` (deterministic)
2. `PlanExecutor` (deterministic)
3. `PropagationEngine` (deterministic strategies)
4. `ScoreAppender` (deterministic MusicXML part append/write-back)
5. `TransformValidator` (deterministic metrics + thresholds)
6. `RepairCoordinator` (AI-triggered, bounded retries)
7. `TransformStore` (persist transformed score + metadata)

Module placement:
1. `src/api/voice_parts_analysis.py`
2. `src/api/voice_parts_plan.py`
3. `src/api/voice_parts_execute.py`
4. `src/api/voice_parts_musicxml_append.py`
5. `src/api/voice_parts_validate.py`
6. Reuse `src/api/voice_parts.py` helpers where possible.

## 5. Data Contracts

### 5.1 Core References
```json
{
  "target_ref": { "part_index": 1, "voice_part_id": "voice part 1" },
  "source_ref": { "part_index": 0, "voice_part_id": "voice part 1" }
}
```

Rules:
1. All references must include both `part_index` and `voice_part_id`.
2. `voice_id` can be accepted as compatibility input but normalized internally to `voice_part_id`.

### 5.2 Section Range
```json
{
  "start_measure": 36,
  "end_measure": 41
}
```

Rules:
1. Inclusive measure bounds.
2. `start_measure <= end_measure`.
3. If omitted, operation applies to whole target stream.

### 5.3 Section Override
```json
{
  "start_measure": 42,
  "end_measure": 57,
  "source": { "part_index": 0, "voice_part_id": "voice part 1" },
  "strategy": "overlap_best_match",
  "policy": "fill_missing_only"
}
```

Supported values:
1. `strategy`: `strict_onset | overlap_best_match | syllable_flow`
2. `policy`: `fill_missing_only | replace_all | preserve_existing`

### 5.4 Transform Plan (AI Output)
```json
{
  "targets": [
    {
      "target": { "part_index": 1, "voice_part_id": "voice part 1" },
      "actions": [
        {
          "type": "split_voice_part",
          "split_shared_note_policy": "duplicate_to_all"
        },
        {
          "type": "propagate_lyrics",
          "verse_number": "1",
          "copy_all_verses": false,
          "source_priority": [
            { "part_index": 1, "voice_part_id": "voice part 1" },
            { "part_index": 0, "voice_part_id": "voice part 1" }
          ],
          "strategy": "overlap_best_match",
          "policy": "fill_missing_only",
          "section_overrides": []
        }
      ],
      "confidence": 0.9,
      "notes": "..."
    }
  ]
}
```

Executor requirements:
1. Validate schema and allowed enums.
2. Reject unknown action types.
3. Normalize overlapping section overrides by precedence.
4. Normalize deprecated `voice_id` input into `voice_part_id`.

### 5.5 Execution Result
```json
{
  "status": "ready",
  "score": { "...": "transformed" },
  "part_index": 1,
  "transform_id": "vp:part1:voice part 1:sha256...",
  "appended_part_ref": {
    "part_index": 3,
    "part_id": "P_DERIVED_001",
    "part_name": "Tenor (Derived)"
  },
  "modified_musicxml_path": "sessions/.../score.modified.musicxml"
}
```

### 5.6 Verse and Exempt-Note Policy
1. `verse_number` default is `"1"` unless user specifies another verse.
2. Propagation copies only the requested verse by default.
3. Multi-verse copy is opt-in via `copy_all_verses: true`.
4. Lyric-exempt notes are tagged when any of these hold:
   - explicit vocalise marker from source metadata
   - configured hum/syllable placeholders
   - section-level override policy marks note range exempt

Warning-accept variant:
```json
{
  "status": "ready_with_warnings",
  "warnings": [
    {
      "code": "partial_lyric_coverage",
      "start_measure": 54,
      "end_measure": 55,
      "missing_note_count": 2
    }
  ]
}
```

Action-required variant:
```json
{
  "status": "action_required",
  "action": "select_source_voice_part",
  "message": "...",
  "target_voice_part": "voice part 1",
  "source_voice_part_options": [
    { "source_part_index": 0, "source_voice_part_id": "voice part 1" }
  ]
}
```

## 6. APIs

### 6.1 Parse API
Current:
1. `parse_score(...)` returns `voice_part_signals`.

Extend:
1. Add optional `full_score_analysis` payload:
   - per-part voice streams
   - per-measure lyric coverage
   - source-candidate ranking hints.
2. Carry requested `verse_number` through analysis payload.

### 6.2 Preprocess API (new)
Add deterministic pre-step API before synth:
1. `preprocess_voice_parts(score, request)`:
   - accepts plan and/or direct operation payload
   - returns transformed score or `action_required`.
2. Relationship to existing API:
   - `prepare_score_for_voice_part` becomes a compatibility wrapper.
   - `preprocess_voice_parts` is the new orchestration entrypoint.
3. `voice_id` accepted temporarily for backward compatibility, but deprecated in favor of `voice_part_id`.

### 6.3 Materialize API (new)
Add score write-back API:
1. `materialize_transformed_part(source_musicxml_path, transformed_part, append_metadata)`:
   - appends new `score-part` to `<part-list>`
   - appends new `<part id="...">` with transformed measures/notes/lyrics
   - returns `modified_musicxml_path` and `appended_part_ref`.

### 6.4 Synthesize integration
Synthesize flow:
1. Resolve requested target ref or appended derived part ref.
2. If transformed score artifact exists, reuse.
3. Else execute preprocess + materialize append.
4. Continue existing phonemize/inference pipeline unchanged by targeting appended part.
5. Avoid mutating caller-owned input score in-place; return updated score snapshot separately.

## 7. Execution Pipeline

### 7.1 Step A: Split
1. Identify target notes by normalized source voice key.
2. Keep rests belonging to target voice for timing alignment.
3. Output target-only part notes.
4. Shared/unison note policy:
   - `duplicate_to_all` (default): copy shared notes into each relevant derived voice.
   - `assign_primary_only`: assign shared note to primary target voice only.

### 7.2 Step B: Propagation Defaults
1. Apply global propagation policy/strategy across full target.
2. Source chosen by:
   - explicit source
   - highest-ranked source candidate.

### 7.3 Step C: Section Overrides
1. For each override range, subset target notes by measure.
2. Re-run propagation with override source/policy/strategy.
3. Merge back into transformed target stream.

Precedence:
1. Later override entries win on overlap.
2. Override result wins over global default in overridden bars.

### 7.4 Step D: Append Transformed Part to Source MusicXML
1. Generate stable appended part metadata:
   - `part_id`: `P_DERIVED_<short_transform_hash>`
   - `part_name`: `<Voice Part Name> (Derived)`
2. Add corresponding entry under `<part-list>/<score-part>`.
3. Append `<part id="P_DERIVED_<short_transform_hash>">` with transformed musical content.
4. Preserve original parts unmodified.
5. Persist modified MusicXML as new artifact (do not overwrite original upload).
6. Idempotency:
   - same score fingerprint + same transform hash -> reuse existing appended part.
   - same target with new transform hash -> append new derived part revision.

## 8. Propagation Strategies

### 8.1 `strict_onset`
Match by exact `offset_beats` (rounded tolerance).
Pros: deterministic, safe.
Cons: low recall in contrapuntal sections.

### 8.2 `overlap_best_match`
For each target note:
1. Find source notes with time overlap.
2. Score candidate source notes:
   - `score = 0.7 * overlap_ratio + 0.3 * onset_proximity`
   - `overlap_ratio = overlap_duration / target_duration`
   - `onset_proximity = max(0, 1 - abs(delta_onset_beats) / onset_window_beats)`
3. Select best scored source lyric note.
4. Tie-breakers:
   - higher score
   - higher lyric confidence (non-empty, non-extension-only)
   - smaller onset delta
   - lower source note index (stable deterministic order)

Recommended default strategy.

### 8.3 `syllable_flow`
Phrase-aware fallback:
1. Build ordered source lyric token sequence in section.
2. Walk target sung notes and advance token index by phrase boundaries.
3. Preserve extension behavior (`lyric_is_extended`, `syllabic`) where possible.

Phrase boundary detection order:
1. explicit breath mark / phrase mark metadata
2. rest boundary above duration threshold
3. section boundary edge
4. long-duration hold crossing configurable threshold

Use only when overlap-based matching underperforms.

## 9. Validation Engine
Metrics:
1. `lyric_coverage_ratio`
2. `source_alignment_ratio`
3. `missing_lyric_sung_note_count`
4. `extension_continuity_issues`
5. `section_override_conflicts`
6. `lyric_exempt_note_count`

Default thresholds (tunable):
1. `lyric_coverage_ratio >= 0.98`
2. `missing_lyric_sung_note_count == 0` for requested output
3. `source_alignment_ratio >= 0.70` (or mark warning)
4. Exempt notes are excluded from denominator of lyric coverage ratio.

Partial success policy:
1. Return `ready_with_warnings` when:
   - `lyric_coverage_ratio >= 0.90`
   - unresolved notes are localized below configured cap
2. Return `action_required` when below partial success threshold.

Validator output:
```json
{
  "passed": false,
  "issues": [
    {
      "code": "low_alignment_in_section",
      "severity": "warning",
      "start_measure": 42,
      "end_measure": 49,
      "hint": "add section override with alternate source"
    }
  ]
}
```

## 10. Repair Loop
Bounded retries:
1. Max attempts: 2 (configurable).
2. On failure, AI receives:
   - previous plan
   - validator issues
   - candidate sources by section.
3. AI returns revised plan with targeted section overrides.
4. Context budget control:
   - send summarized section diagnostics, not full note-level dumps
   - cap section issues count; include top-K highest impact segments.

Escalate to `action_required` when:
1. Still failing after retries.
2. Multiple sources remain ambiguous with low confidence.

## 11. Persistence
Storage location:
1. Session snapshot field: `voice_part_transforms`.
2. Key format:
   - `part:{part_index}|target:{voice_part_id}|score:{score_fingerprint}|hash:{plan_hash}`

Stored payload:
1. Transformed part.
2. Plan metadata.
3. Validation summary.
4. Timestamp and version.
5. Appended part reference.
6. Modified MusicXML artifact path.

Reuse:
1. If same target + equivalent plan hash exists, skip recompute.
2. If appended part already exists for same transform hash, reuse appended part ref.

## 12. Error/Status Taxonomy
Standardized statuses:
1. `ready`
2. `ready_with_warnings`
3. `action_required`
4. `error`

Action codes:
1. `target_voice_part_not_found`
2. `missing_lyrics_no_source`
3. `select_source_voice_part`
4. `invalid_section_override`
5. `validation_failed_needs_review`
6. `musicxml_append_failed`
7. `deprecated_voice_id_input`

## 13. Security and Guardrails
1. AI plan is data-only JSON, never executable code.
2. Executor validates all refs and bar ranges before mutation.
3. Reject section override ranges outside target measure span.
4. Preserve original score as immutable input copy.
5. Append-only write-back: never mutate or delete original parts.

## 14. Performance
Optimizations:
1. Precompute per-part/per-measure index maps.
2. Cache source candidate ranking by target section.
3. Reuse transformed artifacts across synth calls.
4. Reuse modified MusicXML artifact when transform hash is unchanged.

Expected overhead:
1. Analyzer + execution + validation target sub-second for typical choir scores.
2. Repair loop only invoked on edge cases.
3. XML append/serialize may dominate runtime on large scores; treat as known bottleneck.

## 15. Example: `my-tribute` Tenor
Target:
1. `{part_index: 1, voice_part_id: "voice part 1"}`

Plan:
1. Split Men upper voice.
2. Global `fill_missing_only` with `overlap_best_match`.
3. Override m36-41 with `preserve_existing` using Men local source.
4. Override m42-57 with Women source for gap fill.
5. Append result as `Tenor (Derived)` part to source MusicXML.

Validation expectation:
1. Tenor note stream complete.
2. End-to-end lyric coverage for output.
3. No overwrite of already-correct local Men lyric bars.
4. Appended part renders in preview as a separate new part.

## 16. Rollout Plan
1. Phase A: Introduce section override schema + executor support.
2. Phase B: Add overlap-based strategy and validator thresholds.
3. Phase C: Enable repair loop behind feature flag.
4. Phase D: Enable default AI-planned section overrides for low-confidence pieces.

Feature flags:
1. `VOICE_PART_SECTION_OVERRIDES_ENABLED`
2. `VOICE_PART_REPAIR_LOOP_ENABLED`
3. `VOICE_PART_SYLLABLE_FLOW_ENABLED`
4. `VOICE_PART_PARTIAL_SUCCESS_ENABLED`
5. `VOICE_PART_DEPRECATE_VOICE_ID`

## 17. Test Plan
Unit tests:
1. Section range validation and overlap precedence.
2. Strategy behaviors (`strict_onset`, `overlap_best_match`, `syllable_flow`).
3. Policy behaviors (`fill_missing_only`, `preserve_existing`, `replace_all`).

Integration tests:
1. `assets/test_data/amazing-grace-satb-verse1.xml`
2. `assets/test_data/my-tribute.mxl/score.xml`
3. MusicXML append tests:
   - new part appears in `part-list`
   - new part is selectable for synth
   - modified score preview matches appended content

Golden checks:
1. Measure-level lyric coverage maps before/after transform.
2. Deterministic transform hash for identical plan inputs.
3. Deterministic appended part ID for identical transform hash.

## 18. Concurrency and Locking
1. Use per-session transform lock keyed by `session_id`.
2. Serialize append operations for same source score artifact.
3. On concurrent identical requests:
   - first writer materializes transform
   - followers reuse persisted result by score fingerprint + plan hash.
4. On concurrent different transform plans:
   - each produces separate derived part revision
   - latest selected derived part reference is tracked in session state.
