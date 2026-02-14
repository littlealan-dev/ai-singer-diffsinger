# SIG-6 Timeline-Section Plan - Implementation Blueprint

## 1. Scope
Implement support for timeline-first plans (`sections`) as direct section-by-section execution in `src/api/voice_parts.py`.

Goals:
- Keep API backward compatible.
- Reuse existing split/duplicate/propagate executors.
- Keep each section as an independent unit-of-work.

## 2. Files To Change
- `src/api/voice_parts.py`
- `tests/test_voice_parts.py`
- `tests/test_voice_parts_e2e.py`

## 3. Contract To Support
Per target:
- `target`: `{part_index, voice_part_id}`
- `sections`: ordered measure ranges with mode:
  - `rest`
  - `derive` with optional:
    - `melody_source`
    - `lyric_source`
    - `lyric_strategy`
    - `lyric_policy`

## 4. Parser Changes
Inside `parse_voice_part_plan(...)`:

1. Detect timeline mode:
- If `targets[i].sections` exists, parse as timeline target.

2. Validate timeline sections:
- non-empty list
- integer `start_measure` and `end_measure`
- `start_measure <= end_measure`
- sorted, non-overlapping
- contiguous from `1..max_measure` (where `max_measure` is derived from target part span)
- `rest` section disallows source fields
- `derive` section requires at least one of `melody_source` or `lyric_source`
- validate all source refs via existing part/voice-part resolvers

3. Normalize timeline target for direct execution:
- keep `sections` as-is (validated and normalized), no compile-to-legacy conversion
- normalize each source ref to canonical `{part_index, voice_part_id}`
- normalize strategy/policy defaults per section

4. Store metadata:
- `plan_mode = "timeline_sections"`
- normalized section count

## 5. Executor Changes
Inside `_execute_preprocess_plan(...)`:

1. Add a timeline runner path:
- if target has `sections`, call `_execute_timeline_sections(...)`.
2. In `_execute_timeline_sections(...)`:
- initialize target part context once
- iterate sections in order
- execute section immediately:
  - `rest`: clear/keep-rest in range
  - `derive` melody: call existing note copy helpers with range
  - `derive` lyric: call existing propagation helper with range
- append one section result record for each section
3. After all sections:
- stitch all section outputs into one full-length derived part (simple concatenation by measure range)
- run existing final validation path

## 6. Validation Behavior
Before execution:
- return `action_required` with code:
  - `invalid_section_range`
  - `overlapping_sections`
  - `non_contiguous_sections`
  - `invalid_section_mode`
  - `invalid_section_source`
  - no boundary-continuity error for tie/slur/melisma in V1

After execution:
- reuse existing coverage metrics and status handling:
  - `ready`
  - `ready_with_warnings`
  - `action_required`
  - `error`

## 7. Tests

### 7.1 Unit (`tests/test_voice_parts.py`)
- add parser tests for timeline sections:
  - valid normalize
  - overlap reject
  - non-contiguous reject
  - invalid derive with no sources reject
  - invalid rest with source reject
- add section-runner assertions:
  - section-by-section output is deterministic
  - stitch output measure order is correct
  - boundary tie/slur/melisma does not block execution

### 7.2 Integration/E2E (`tests/test_voice_parts_e2e.py`)
- add one timeline-plan test path for My Tribute:
  - run with `VOICE_PART_E2E_SKIP_SYNTHESIS=1`
  - assert derived score exists and parseable
  - assert key measure windows have expected lyric presence
  - assert section patch run can update only one measure range without recomputing full target

## 8. Rollout
1. Land parser + direct section runner + tests.
2. Keep legacy `actions` plans fully supported.
3. Switch planner prompt to emit `sections`.
4. Monitor failure rate and only then remove legacy prompt shape.

## 9. Non-Goals
- No repair-loop redesign in this change.
- No parser opinion fields (facts-only parser stays intact).
- No synthesis-pipeline changes.
