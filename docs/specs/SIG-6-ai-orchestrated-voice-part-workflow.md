# SIG-6 AI-Orchestrated Voice Part Workflow

## Goal
Build an intelligent workflow that combines:
- AI-level full-score understanding (global musical context)
- Deterministic tool execution (split voices, replicate lyrics, validate result)

So the system can split all voice parts and replicate lyrics robustly across complex, real-world scores.

## Why This Approach
Purely rule-based logic will always miss edge cases across large score diversity.  
Purely AI-edited score mutation is hard to trust and hard to debug.

The target model is:
- AI plans and decides
- Tools execute exact operations
- Validators check quality
- AI repairs when needed

## Core Principles
1. AI never edits raw MusicXML directly.
2. Every operation is explicit and typed (`split`, `propagate_lyrics`, `validate`).
3. IDs are always stable: `part_index + voice_part_id`.
4. All transforms are persisted as reusable transformed scores.
5. Flow is demand-driven: only process requested singing targets.
6. Section overrides are optional: global defaults first, bar-range overrides only when needed.
7. Transform is non-destructive: final output is appended back into source MusicXML as a new part.
8. Transform is verse-aware: operate on requested lyric verse by default.
9. Append is idempotent for the same score + plan fingerprint.

## System Architecture
1. **Full Score Analyzer (deterministic)**
- Builds a canonical score map from entire MusicXML:
  - parts, measures, staves, voices, note onsets/durations, lyric tokens, ties/slurs.
- Produces machine-readable features for planning.

2. **AI Planner (LLM)**
- Reads canonical map and user intent.
- Produces a structured transform plan:
  - which voice parts to split
  - lyric source selection per target voice part
  - optional section overrides (`start_measure`, `end_measure`)
  - confidence and rationale
  - fallback options

3. **Transform Executor (deterministic tools)**
- Applies plan using typed APIs only.
- No free-form score rewrite.

4. **Score Materializer (deterministic)**
- Appends transformed target as a new `score-part` + `part` in MusicXML.
- Produces modified score artifact for preview refresh and optional download.

5. **Validator (deterministic + heuristic)**
- Checks coverage and musical consistency:
  - split completeness
  - lyric coverage by measure
  - onset alignment quality
  - melisma extension continuity

6. **Repair Loop (AI + tools)**
- If validation fails, AI receives validator report and generates a revised plan.
- Repeat until pass or action-required escalation.

## Proposed Tool/API Surface
1. `analyze_full_score(score)`  
Returns global voice/lyric map and candidate lyric sources.

2. `plan_voice_part_transforms(analysis, intent)`  
LLM-produced JSON plan (no mutation).

3. `split_voice_part(score, part_index, voice_part_id)`  
Deterministically isolates one voice part.
Shared-note policy is explicit:
- default: duplicate unison/shared notes into each implicated derived voice part
- optional policy override: `assign_primary_only`

4. `propagate_lyrics(score, target_ref, source_ref, strategy, section_overrides?, verse_number?)`  
Copies lyrics with strategy options:
- `strict_onset`
- `overlap_best_match`
- `syllable_flow`

`section_overrides` is optional and supports:
- section-specific source references
- section-specific strategy/policy
- example use case: preserve local lyrics in m36-41, use external source for m42-57

`syllable_flow` phrase boundaries are derived from:
- rests
- explicit breath marks / phrase marks when available
- long-duration boundaries over threshold
- section boundary edges

5. `validate_transformed_score(score, target_refs)`  
Returns pass/fail plus structured issues.

6. `persist_transformed_score(score, metadata)`  
Stores reusable transformed output for later synth calls.

7. `append_transformed_part_to_musicxml(source_musicxml, transformed_part, metadata)`  
Returns modified MusicXML with appended part and stable identifiers for UI preview/synth.

## End-to-End Flow
1. User uploads MusicXML.
2. Backend parses full score and returns signals + options.
3. User asks to sing target voice part(s).
4. AI planner builds transform plan for only requested targets.
5. Executor runs split + lyric propagation steps with global defaults.
6. If section overrides exist, executor applies override operations by bar range.
7. Validator checks quality.
8. If validation is partial but above accept threshold, return `ready_with_warnings` for user acceptance.
9. If pass, materializer appends transformed output as a new MusicXML part.
10. Backend returns modified score payload; UI refreshes notation preview from modified MusicXML.
11. Persist transformed score + modified MusicXML artifact for reuse/download.
12. Continue synth flow by targeting the newly appended part.
13. If fail and fixable, run repair loop (often by adding/refining section overrides).
14. If ambiguous, return `action_required` with concrete choices.

## AI Planning Output Contract (High-Level)
```json
{
  "targets": [
    {
      "target": { "part_index": 1, "voice_part_id": "voice part 2" },
      "actions": [
        { "type": "split_voice_part" },
        {
          "type": "propagate_lyrics",
          "source": { "part_index": 0, "voice_part_id": "voice part 1" },
          "strategy": "overlap_best_match",
          "section_overrides": [
            {
              "start_measure": 36,
              "end_measure": 41,
              "source": { "part_index": 1, "voice_part_id": "voice part 1" },
              "strategy": "strict_onset",
              "policy": "preserve_existing"
            }
          ]
        }
      ],
      "confidence": 0.88,
      "notes": "Source has strongest bar-level onset overlap."
    }
  ]
}
```

## Validation Metrics
1. Lyric coverage ratio by target voice part and section.
2. Matched-onset ratio between target and chosen lyric source.
3. Unresolved syllable extension/tie continuity count.
4. Empty-lyric sung-note count after propagation.
5. Lyric-exempt sung-note count (vocalise/hum/intentional no-lyric).
6. Confidence threshold for auto-accept vs action-required.

## Escalation Rules
Return `action_required` when:
1. No viable lyric source exists.
2. Multiple sources are similarly plausible and confidence is low.
3. Validation still fails after repair attempts.

Return `ready_with_warnings` when:
1. Validation passes minimum acceptance threshold but not strict pass.
2. Remaining issues are localized and user-reviewable.
3. Default threshold: `lyric_coverage_ratio >= 0.90`.

Response should include:
- candidate sources
- why each candidate is plausible
- recommended option
- unresolved bars/notes summary when partial success is returned.

## Worked Example: `my-tribute` Tenor Request
Input score:
- `assets/test_data/my-tribute.mxl/score.xml`

User request:
- "Give me the full end-to-end lyric and notes for Tenor part."

### 1. Analyzer output (high-level)
The full-score analyzer detects:
- `part_index=0` Women
- `part_index=1` Men
- `part_index=2` Pipe Organ (non-vocal target for this request)

For Men (`part_index=1`):
- Singing notes present from m19 onward.
- Split voices in many bars (`voice1` + `voice2`), especially m25-35 and m45+.
- Lyrics are only partially present (notably around m36-41 on one Men voice).

For Women (`part_index=0`):
- Strong lyric coverage across the same song span.
- Good candidate source for missing Men lyrics.

### 2. AI planner decision
Planner resolves "Tenor" as the upper Men voice:
- Target ref: `{ "part_index": 1, "voice_part_id": "voice part 1" }`

Plan intent:
1. Split Tenor note lane from Men part.
2. Preserve any existing Tenor lyrics.
3. Fill missing lyric segments using Women source with best alignment.
4. Use section overrides where local Men lyrics are more reliable than cross-part replication.
5. Operate on requested verse (default Verse 1 unless user asks otherwise).
6. Validate lyric continuity and note-lyric coverage.

Example plan (conceptual):
```json
{
  "targets": [
    {
      "target": { "part_index": 1, "voice_part_id": "voice part 1" },
      "actions": [
        { "type": "split_voice_part" },
        {
          "type": "propagate_lyrics",
          "policy": "fill_missing_only",
          "source_priority": [
            { "part_index": 1, "voice_part_id": "voice part 1" },
            { "part_index": 0, "voice_part_id": "voice part 1" }
          ],
          "strategy": "overlap_best_match",
          "section_overrides": [
            {
              "start_measure": 36,
              "end_measure": 41,
              "source": { "part_index": 1, "voice_part_id": "voice part 1" },
              "strategy": "strict_onset",
              "policy": "preserve_existing"
            },
            {
              "start_measure": 42,
              "end_measure": 57,
              "source": { "part_index": 0, "voice_part_id": "voice part 1" },
              "strategy": "overlap_best_match",
              "policy": "fill_missing_only"
            }
          ]
        }
      ],
      "confidence": 0.9
    }
  ]
}
```

### 3. Executor behavior
1. `split_voice_part(part_index=1, voice_part_id="voice part 1")`
- Produces Tenor-only note stream for requested output.

2. `propagate_lyrics(..., policy="fill_missing_only")`
- Keeps existing Men Tenor lyrics (for bars where present, e.g. m36-41).
- Fills missing lyric bars (e.g. earlier/later Tenor sections) from Women source based on overlap/alignment.
3. `apply section_overrides`
- m36-41 uses local Men Tenor as source of truth (`preserve_existing`).
- m42-57 uses Women source to fill missing Tenor lyric content (`fill_missing_only`).

4. Persist transform
- Save transformed Tenor score as reusable artifact for synth retries or alternate voices.

### 4. Validator checks
Validator confirms:
1. Tenor note stream is isolated (no bass contamination).
2. Lyric coverage for sung Tenor notes is complete or above threshold.
3. No regression in bars that already had valid Men Tenor lyrics.
4. Melisma/tie extension continuity is acceptable.

If failed:
- AI repair loop adjusts source choice by section (bar-range-specific source remap) and reruns.

### 5. Final output to synth flow
Once validation passes:
1. Materializer appends a new part (example: `TENOR_DERIVED`) into source MusicXML.
2. UI reloads preview from modified MusicXML so user can visually verify notes/lyrics.
3. Optional download is enabled for modified MusicXML.
4. Existing synth pipeline proceeds unchanged by instructing synth to sing the new appended part.
5. Re-running the same request reuses the same derived part artifact (idempotent behavior).

## Rollout Plan
1. Phase 1: AI planner + existing strict propagation as baseline.
2. Phase 2: Add overlap-based propagation strategy and validator.
3. Phase 3: Add repair loop and confidence-driven auto-selection.
4. Phase 4: Optimize with cached transformed-score reuse and telemetry.

## Success Criteria
1. Complex choir scores with mixed lyric ownership can be transformed without manual XML edits.
2. Most requested voice parts become synth-ready in one pass.
3. Failures become explainable action-required choices, not silent bad output.
4. Users can visually verify transformed output in refreshed score preview.
5. Modified MusicXML can be downloaded for external editing/reuse.
6. Overall pipeline remains deterministic, auditable, and cost-controlled.
