# Task List: Bounded 3-Attempt SATB Split Planning

Derived from [satb_split_design_doc.md](/Users/alanchan/antigravity/ai-singer-diffsinger/docs/specs/satb_split_design_doc.md).

## Scope

Implement a bounded 3-attempt preprocess planning loop that:

- classifies all preflight and postflight rules by severity and domain
- prevents new P0 regressions
- prioritizes STRUCTURAL over LYRIC when trade-offs conflict
- selects the best valid candidate after up to 3 attempts
- returns an error only if no structurally valid candidate exists

## Phase 1: Rule Metadata Model

- Add canonical rule metadata for postflight validations, parallel to [`LINT_RULE_SPECS`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_part_lint_rules.py).
- Introduce structured fields for every rule:
  - `code`
  - `name`
  - `severity`
  - `domain`
  - `definition`
  - `fail_condition`
  - `suggestion`
  - `message_template`
- Keep preflight and postflight metadata separate, but with the same shape.
- Replace hard-coded postflight failure/warning labels in [`src/api/voice_parts.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_parts.py) with rule-registry lookups.

## Phase 2: Structured Issue Emission

- Normalize all preflight lint findings so they expose:
  - canonical rule metadata
  - structured failing attributes
  - measure or section ranges
- Normalize all postflight validation outputs so they expose the same structure.
- Add explicit measure-impact fields for postflight issues:
  - `impacted_measures`
  - `impacted_ranges`
- Ensure rules without natural section boundaries still emit a deterministic measure-impact payload when possible.

## Phase 3: Candidate Evaluation Model

- Define an internal candidate object in [`src/backend/orchestrator.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py) or a helper module.
- Candidate should store:
  - attempt number
  - plan
  - preflight issues
  - execution result
  - postflight issues
  - current score snapshot
  - whether candidate is structurally valid
  - quality class
  - impacted-measure aggregates
- Keep only rolling selection state in the main loop:
  - `best_valid`
  - `best_invalid`
  - `current_attempt`
- Update `best_valid` or `best_invalid` only when the current attempt ranks better than the previously stored best candidate.
- Do not require all full candidates to remain in memory for selection.
- If debugging history is desired, persist only lightweight per-attempt summaries in session state.

## Phase 4: Severity and Domain Classification

- Classify all preflight findings into:
  - `P0 STRUCTURAL`
  - `P1 STRUCTURAL`
  - `P1 LYRIC`
- Classify all postflight validations into:
  - `P0 STRUCTURAL`
  - `P1 LYRIC`
  - `P2 LYRIC`
- Add helpers to compute:
  - `has_p0_preflight`
  - `has_p0_postflight`
  - `structural_p1_impacted_measures`
  - `lyric_p1_impacted_measures`
  - `p2_impacted_measures`

## Phase 5: Measure-Impact Aggregation

- Implement union-based measure aggregation per bucket:
  - union all STRUCTURAL P1 measure ranges
  - union all LYRIC P1 measure ranges
  - union all P2 measure ranges
- Do not double-count overlapping ranges.
- Add reusable helpers in backend or API layer for:
  - range normalization
  - range union
  - impacted-measure counts

## Phase 6: Bounded 3-Attempt Selection Loop

- Refactor the current repair loop in [`src/backend/orchestrator.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py) to evaluate candidates instead of only the latest followup result.
- Per attempt:
  - generate plan with LLM
  - run preflight
  - reject immediately if any preflight P0 exists
  - execute preprocess if preflight passes P0
  - run postflight
  - mark invalid if postflight has `P0 STRUCTURAL`
  - otherwise keep candidate as valid
- Exit early if a valid Class 3 candidate appears.
- After 3 attempts:
  - return `best_valid` if one exists
  - otherwise return error with best invalid diagnostics

## Phase 7: Candidate Quality Classification

- Implement quality classes exactly as specified:
  - Class 3: no P1 or P2
  - Class 2: no P1, has P2
  - Class 1: has P1
- Implement ranking order:
  - higher quality class
  - smaller structural P1 impacted measures
  - smaller lyric P1 impacted measures
  - smaller P2 impacted measures
  - earlier attempt as final tie-breaker

## Phase 8: Domain-Priority Repair Guidance

- Update prompt guidance in:
  - [`src/backend/config/system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)
  - [`src/backend/config/system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)
- Add explicit repair policy:
  - try to resolve all issues
  - never introduce new P0 issues
  - if equal severity conflicts remain, preserve STRUCTURAL over LYRIC
  - preserve already-working regions while patching failing ranges only
- Inject canonical rule metadata into prompt context for postflight validations too, not only preflight lint.

## Phase 9: User-Facing Return Behavior

- When `best_valid` exists after 3 attempts:
  - return that candidate’s score/result to frontend for review
  - include its warnings/diagnostics
- When no structurally valid candidate exists:
  - return error:
    - `Unable to produce synthesis-safe monophonic output after 3 attempts.`
  - include diagnostics from `best_invalid`
- Ensure progress-job completion payload supports:
  - final selected candidate message
  - review-required flag
  - surfaced diagnostics for non-perfect but valid outputs

## Phase 10: Session and Debugging Support

- Persist per-attempt debugging summaries in session state:
  - generated plan
  - rule findings
  - candidate classification
-  - whether it replaced `best_valid` or `best_invalid`
- Keep latest successful plan in prompt context only.
- Do not require full candidate payload retention across all attempts unless later needed for debugging.

## Phase 11: Tests

- Add unit tests for:
  - severity/domain classification
  - measure union logic
  - candidate quality classification
  - selection ranking
  - early exit on Class 3
  - fallback to best valid after 3 attempts
  - error when all attempts are structurally invalid
- Add regression tests with mocked candidate sequences:
  - better lyric candidate should lose to structurally cleaner candidate
  - more issues in fewer measures should beat fewer issues in wider ranges
  - overlapping measure ranges should not double-count
- Add orchestrator-level tests for:
  - preflight P0 rejection without execution
  - postflight P0 invalidation
  - best-valid return path
  - best-invalid diagnostics path

## Suggested File Touch Points

- [`src/api/voice_part_lint_rules.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_part_lint_rules.py)
- [`src/api/voice_parts.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voice_parts.py)
- [`src/backend/orchestrator.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py)
- [`src/backend/llm_prompt.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py)
- [`src/backend/config/system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)
- [`src/backend/config/system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)
- [`tests/test_voice_parts.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_voice_parts.py)
- [`tests/test_voice_part_lint_regressions.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_voice_part_lint_regressions.py)
- [`tests/test_backend_api.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_backend_api.py)

## Open Questions

1. `same_part_target_completeness`
   Decision:
   keep it as `P0 STRUCTURAL`.

2. Best valid candidate return behavior
   Decision:
   still return the best valid candidate for review, and have the LLM explain why the workflow stopped there and what warnings remain.

3. All attempts structurally invalid
   Decision:
   if no attempt passes structural P0 post-check, return an error with no fallback mode.

4. Warning-only candidates
   Decision:
   yes, warning-only candidates are valid, but only the single best candidate should be returned.

5. `best_invalid` participation
   Decision:
   only post-flight-invalid attempts participate in `best_invalid`.

6. Repair feedback scope
   Decision:
   return all outstanding issues to the LLM on each repair turn.
