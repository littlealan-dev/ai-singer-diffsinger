# Change List: Multi-Target Preprocess Result Model

## Goal

Fix the current mismatch between:

- per-target preprocess execution
- aggregated candidate diagnostics
- user-visible derived score materialization

The new model should make candidate quality, warnings, and blocking issues explicit **per target voice part**, while keeping only truly global fields at the top level.

## Desired Result Shape

Top-level fields should contain only workflow-global metadata:

- `status`
- `action`
- `message`
- `attempt_number`
- `max_attempts`
- `candidate_selected`
- `all_targets_class`
- `score`
- `review_required`

Target-specific fields should move under a per-target structure, for example:

```json
{
  "status": "action_required",
  "action": "validation_failed_needs_review",
  "attempt_number": 2,
  "max_attempts": 3,
  "all_targets_class": 1,
  "targets": [
    {
      "part_index": 1,
      "target_voice_part_id": "voice part 1",
      "derived_part_index": 4,
      "derived_part_id": "P_DERIVED_...",
      "hidden_default_lane": false,
      "quality_class": 1,
      "structurally_valid": true,
      "validation": { "...": "..." },
      "issues": [ "...per-target issues..." ],
      "failing_ranges": [ "...per-target ranges..." ]
    },
    {
      "part_index": 1,
      "target_voice_part_id": "voice part 2",
      "derived_part_index": 5,
      "derived_part_id": "P_DERIVED_...",
      "hidden_default_lane": false,
      "quality_class": 3,
      "structurally_valid": true,
      "validation": { "...": "..." },
      "issues": [],
      "failing_ranges": []
    }
  ]
}
```

## Core Changes

### 1. Per-Target Postflight Output

- Change preprocess execution output so validation/warning/error data is emitted per target, not flattened into one mixed payload.
- For each target, include:
  - target identity
  - derived part identity
  - hidden/visible status
  - validation metrics
  - warning/error issues
  - quality class
  - structural validity
  - failing ranges

### 2. Per-Target Candidate Classification

- Compute `quality_class` per target:
  - `Class 3`: no P1 or P2 issues for that target
  - `Class 2`: no P1, has P2
  - `Class 1`: has P1
- Compute `structurally_valid` per target, not by reusing the last target’s payload.

### 3. Global Candidate Classification

- Define whole-attempt candidate quality from the set of visible targets.
- Suggested rule:
  - `all_targets_class = min(target.quality_class for visible targets)`
- Hidden helper/default lanes must not improve global class.
- Hidden helper/default lanes may still exist in metadata, but must not drive user-facing quality.

### 4. Exit Criteria

Replace current stop behavior with:

- stop early only when **all visible target parts are Class 3**
- otherwise continue repair until:
  - attempt quota exhausted, or
  - no further repair can be produced

So the loop exits when:

1. all visible targets are Class 3, or
2. `attempt_number == max_attempts`

### 5. Best Candidate Selection

- Keep rolling best-candidate selection as before.
- But compare candidates using per-target aggregated quality:
  - first by `all_targets_class`
  - then by visible-target issue impact
- Never allow a hidden helper lane to make a candidate appear cleaner than the visible score.

### 6. Materialization Consistency

- When a candidate is selected, materialize all targets from that attempt.
- The exported reviewable MusicXML must match the exact set of visible targets used in:
  - diagnostics
  - class computation
  - review messaging
- Hidden helper/default lanes may remain internal, but must not be the only target satisfying a visible coverage rule.

### 7. Visible vs Hidden Rules

- Keep `hidden_default_lane` as the existing code-level definition of hidden helper lanes.
- All user-facing aggregation must use only:
  - `hidden_default_lane != True`
- This affects:
  - `same_clef_claim_coverage`
  - candidate class aggregation
  - best-candidate ranking
  - final diagnostics shown to UI

### 8. Orchestrator Response Model

- Update orchestrator candidate building to consume per-target result data.
- Do not infer candidate quality from the last tool result alone.
- Build:
  - `targets[]`
  - `all_targets_class`
  - visible-target issue aggregates
  - global workflow metadata

### 9. Prompt / Repair Context

- Repair context sent back to the LLM should include per-target issue state, not only a flattened issue list.
- For each target, expose:
  - target id
  - current class
  - unresolved ranges
  - remaining P1/P2 issues
- LLM should repair the targets that still block `all_targets_class == 3`.

## File Areas To Change

- [`src/api/voice_parts.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_parts.py)
  - preprocess target execution output
  - per-target validation aggregation
  - review materialization payload structure
- [`src/backend/orchestrator.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py)
  - candidate model
  - best-candidate comparison
  - stop criteria
  - repair-loop state passed to LLM
- [`src/mcp/tools.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/mcp/tools.py)
  - formal preprocess output schema
  - per-target `targets[]` result schema
- [`src/backend/config/system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)
- [`src/backend/config/system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)
  - describe per-target repair state and new stop rule
- [`ui/src/MainApp.tsx`](/Users/alanchan/antigravity/ai-singer-diffsinger/ui/src/MainApp.tsx)
  - later, to render per-target diagnostics cleanly if desired

## Tests Needed

### Unit / API

- candidate with:
  - target A = Class 1
  - target B = Class 3
  - hidden target C = Class 3
  - global result must be Class 1
- hidden helper lane must not reduce `all_targets_class`
- candidate selection must compare visible targets only
- repair loop must continue if any visible target is not Class 3
- repair loop may stop early only when all visible targets are Class 3

### Materialization

- selected candidate with 2 visible targets + 1 hidden helper target
  - exported MusicXML must contain the 2 visible targets
  - diagnostics must refer to those same 2 visible targets

### Schema

- preprocess MCP output schema must document:
  - top-level workflow fields
  - per-target result fields
  - class and issue semantics

## Non-Goals

- changing the meaning of `hidden_default_lane`
- redesigning the user-visible score review UI in this step
- changing synth selection semantics

## Recommended Implementation Order

1. Define the new preprocess output schema in code and MCP schema.
2. Refactor preprocess result assembly to emit per-target target results.
3. Refactor orchestrator candidate construction to use `targets[]`.
4. Update candidate ranking and stop criteria.
5. Update repair context sent to LLM.
6. Add tests for multi-target class aggregation and early-stop behavior.
