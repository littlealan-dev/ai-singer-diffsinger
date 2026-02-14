# SIG-6 Timeline-Section Plan (Low-Level Design)

## 1. Goal
Move from `base source + section_overrides` to a linear timeline plan (`measure 1 -> end`) so LLM output is explicit, deterministic, and easier to verify.

Constraints:
- Reuse existing backend executors in `src/api/voice_parts.py`.
- Keep API backward-compatible.
- Do not require new score parsing passes.

## 2. Why Change
Current plan shape is global-first, then override-by-exception. In complex scores this causes:
- hidden precedence conflicts (`fill_missing_only` with prior `+` markers),
- accidental carry-over from a wrong base source,
- hard-to-audit plans with many overrides.

Timeline sections remove global precedence: each measure range has one explicit intent.

## 3. New Plan Contract

### 3.1 Target-level schema
```json
{
  "targets": [
    {
      "target": { "part_index": 1, "voice_part_id": "voice part 1" },
      "sections": [
        { "start_measure": 1, "end_measure": 4, "mode": "rest" },
        {
          "start_measure": 19,
          "end_measure": 24,
          "mode": "derive",
          "melody_source": { "part_index": 1, "voice_part_id": "voice part 2" },
          "lyric_source": { "part_index": 0, "voice_part_id": "voice part 3" },
          "lyric_strategy": "strict_onset",
          "lyric_policy": "replace_all"
        }
      ]
    }
  ]
}
```

### 3.2 Section fields
- Required: `start_measure`, `end_measure`, `mode`.
- `mode`:
  - `rest`: target must be silent in this range.
  - `derive`: derive target content from explicit sources in this range.
- Optional for `derive`:
  - `melody_source`: `{part_index, voice_part_id}`
  - `lyric_source`: `{part_index, voice_part_id}`
  - `lyric_strategy`: `strict_onset | overlap_best_match | syllable_flow`
  - `lyric_policy`: `fill_missing_only | replace_all | preserve_existing`

### 3.3 Hard rules
- Sections per target must be sorted, non-overlapping, and contiguous from `1..max_measure`.
- `rest` section must not contain source fields.
- `derive` section must contain at least one of `melody_source` or `lyric_source`.

## 4. Parser Facts Used By LLM
No opinionated fields are added. Use facts already returned by parser/analyzer:
- `voice_part_measure_spans`
- `voice_part_id_to_source_voice_id`
- `measure_staff_voice_map`
- `measure_annotations` (includes `<direction><words>`, e.g. unison cues)
- `measure_lyric_coverage`

## 5. Execution Design (Section Unit-of-Work)

### 5.1 Reuse-first approach
Do not add a compile/aggregation layer from `sections` into one global action.
Execute each section as an independent unit-of-work in measure order.

### 5.2 Mapping table
- Melody extraction source notes:
  - `_select_part_notes_for_voice(...)`
- Melody duplication by range:
  - reuse existing duplicate/copy helpers, but invoked per section
- Lyric source resolution:
  - `_resolve_source_notes(...)`
- Lyric propagation:
  - `_propagate_lyrics(...)` with section measure bounds

### 5.3 Section execution behavior
For each target:
1. Resolve/create target derived part context once.
2. Iterate sections in ascending measure order.
3. Execute each section immediately:
  - `rest`: ensure target has no sung notes in range (alignment preserved with rests).
  - `derive`: run melody operation for range if `melody_source` exists.
  - `derive`: run lyric operation for range if `lyric_source` exists.
4. Persist section result diagnostics.
5. Stitch final target part by simple range concatenation into full-length measure grid.

## 6. API and Backward Compatibility
- `parse_voice_part_plan(...)` accepts both:
  - legacy `actions`,
  - new `sections`.
- Dispatch:
  - if `sections` exists, execute via timeline section runner.
  - else run existing legacy path unchanged.
- Result schema unchanged (`ready`, `ready_with_warnings`, `action_required`, `error`).

## 7. Validation and Diagnostics

### 7.1 Pre-execution validation
- `invalid_section_range`
- `overlapping_sections`
- `non_contiguous_sections`
- `invalid_section_mode`
- `invalid_section_source`

### 7.2 Post-execution validation
Reuse existing validation metrics:
- `lyric_coverage_ratio`
- `missing_lyric_sung_note_count`
- `unresolved_measures`

Add timeline diagnostics:
- `section_results[]` with:
  - section range,
  - copied note count,
  - copied lyric count,
  - missing lyric sung-note count.

## 8. Edge Cases
- `+` extension tokens:
  - treated as non-authoritative text for planning decisions.
  - for sections with explicit `lyric_source`, prefer `replace_all` when correcting stale `+`.
- Unison sections:
  - use `measure_annotations` and staff/voice facts to emit explicit section duplication action.
- Tie/slur/melisma at section boundaries:
  - accepted as-is in V1.
  - no boundary repair and no `action_required` for boundary continuity.
- Sparse target sections:
  - if target has sung notes but no lyrics after derive, return `ready_with_warnings` or `action_required` per threshold policy.

## 9. Testing Plan
- Unit:
  - section schema validation and contiguity checks.
  - per-section unit-of-work execution and stitch correctness.
  - section-level lyric propagation policy behavior (`replace_all` vs `fill_missing_only`).
- Integration:
  - `tests/test_voice_parts_e2e.py::test_my_tribute_all_voice_parts` with `VOICE_PART_E2E_SKIP_SYNTHESIS=1`.
  - assert derived score has expected notes/lyrics per section.
- Regression:
  - keep legacy-action test coverage unchanged.

## 10. Rollout
1. Land parser/plan support and section-runner behind feature flag.
2. Run timeline plan in tests; keep legacy as fallback.
3. Switch LLM system prompt contract to timeline-section format.
4. Remove legacy prompt guidance only after stability window.
