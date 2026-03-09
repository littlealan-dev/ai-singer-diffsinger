# Preprocess Bootstrap Baseline LLD

## Purpose

Add a temporary `bootstrap_plan_baseline` to the preprocess repair loop so the LLM can repair against a concrete failed plan before any comparable candidate exists.

This addresses the current bootstrap weakness:

- attempt 1 fails at preflight lint
- no `best_plan_so_far` exists
- the next repair turn relies mostly on diagnostics
- the LLM can drift into broader rewrites instead of locally editing the failed plan

The design must not weaken the existing `best_plan_so_far` semantics and must not introduce a second candidate-comparison system.

## Problem Statement

The current repair loop only promotes executed, comparable candidates into `best_plan_so_far`.

Comparable candidates are currently limited to:

- `ready`
- `ready_with_warnings`
- `action_required: validation_failed_needs_review`

Preflight failures with:

- `action_required: plan_lint_failed`

do not become `WorkflowCandidate` instances and therefore do not participate in:

- baseline preservation
- semantic section diffing against a prior concrete plan
- local repair anchoring before the first comparable candidate exists

This makes early repair turns more fragile than later repair turns.

## Design Goals

1. Preserve a concrete failed plan as the repair baseline before the first comparable candidate exists.
2. Keep exactly one comparator system.
3. Do not promote lint-failed plans into `best_plan_so_far`.
4. Reuse the existing repair-scope and section-diff machinery as much as possible.
5. Avoid any downstream confusion at attempt exhaustion or review-materialization time.

## Non-Goals

1. Do not make preflight lint-failed plans comparable candidates.
2. Do not use bootstrap baselines as final selected output.
3. Do not materialize review output from bootstrap baselines.
4. Do not change the existing comparator tuple or candidate-ranking rules.

## Current State

Current preprocess loop state in `orchestrator.py` includes:

- `best_valid_candidate`
- `best_invalid_candidate`
- `fixed_structural_issue_keys`
- `fixed_other_issue_keys`
- `current_repair_scopes`

Current behavior:

- `plan_lint_failed` returns `None` from `_build_workflow_candidate(...)`
- therefore no candidate object exists for those attempts
- `_build_repair_planning_prompt(...)` uses:
  - `best_plan_so_far` if present
  - otherwise `latest_attempted_plan`

This fallback is not sufficient because the latest attempted plan is not persisted as a dedicated repair baseline with repair-scope metadata and lifecycle rules.

## Proposed State Model

Add one new loop-local state:

- `bootstrap_plan_baseline`

Recommended shape:

```python
@dataclass
class BootstrapPlanBaseline:
    attempt_number: int
    plan: Dict[str, Any]
    action: str
    lint_findings: List[Dict[str, Any]]
    repair_scopes: List[Dict[str, Any]]
```

If lower implementation churn is preferred, this may be stored as a plain dict with the same fields.

### Semantics

`bootstrap_plan_baseline` means:

- the latest concrete preprocess plan that failed preflight lint
- stored only until the first comparable candidate is promoted
- used only as the repair baseline for the next repair turn

It does **not** mean:

- best comparable candidate
- reviewable candidate
- terminal fallback candidate

## Baseline Selection

Repair prompt baseline selection becomes:

1. If `best_valid_candidate` exists:
   - `baseline_plan_source = "best_plan_so_far"`
   - `baseline_plan = best_valid_candidate.plan`
2. Else if `bootstrap_plan_baseline` exists:
   - `baseline_plan_source = "bootstrap_plan_baseline"`
   - `baseline_plan = bootstrap_plan_baseline.plan`
3. Else if latest attempted plan exists:
   - `baseline_plan_source = "latest_attempted_plan"`
   - `baseline_plan = latest attempted plan`
4. Else:
   - no baseline plan

This is a baseline chooser, not a second comparison system.

## Bootstrap Baseline Lifecycle

### Set / Replace

Set or replace `bootstrap_plan_baseline` only when all are true:

1. `best_valid_candidate is None`
2. the current attempt contains an extracted preprocess plan
3. tool result is `action_required: plan_lint_failed`

When set, store:

- `attempt_number`
- `plan`
- `action = "plan_lint_failed"`
- `lint_findings`
- normalized `repair_scopes`

### Why Replace Instead of Keep-First

Use the latest lint-failed plan as the bootstrap baseline because it is the closest concrete structure to the next repair attempt and may already contain partial local fixes.

### Clear

Clear `bootstrap_plan_baseline` as soon as a comparable candidate is promoted into `best_valid_candidate`.

After that point, the repair loop should always anchor on `best_plan_so_far`.

## Comparator Logic

No comparator changes.

The candidate comparison system remains exactly one system, and it still applies only to comparable candidates.

### Comparable Candidates

- `ready`
- `ready_with_warnings`
- `validation_failed_needs_review`

### Non-Comparable Candidates

- `plan_lint_failed`
- other unusable post-flight failures

### Explicit Rule

`bootstrap_plan_baseline` must never participate in:

- comparator tuple ranking
- `best_plan_so_far` promotion
- terminal candidate selection

## Repair Scope Integration

The bootstrap baseline must integrate with the existing repair-scope logic so that early repair turns can still use local anchoring.

### Rule

When `best_valid_candidate` is absent but `bootstrap_plan_baseline` exists:

- use `bootstrap_plan_baseline.plan` as the anchor plan for `_build_repair_scopes(...)`

This preserves the existing in-scope / out-of-scope definition:

- same-target overlap with failing span
- same-target overlap with the anchor section that covered the failing span

### Important Consequence

The existing semantic section-diff and anchor reshaping logic can be reused without introducing a second scope model.

## Prompt Payload Changes

`_build_repair_planning_prompt(...)` should emit:

- `baseline_plan_source`
- `baseline_plan`
- `latest_attempted_plan_summary`
- `repair_context`

with a new allowed baseline source:

- `bootstrap_plan_baseline`

### Prompt Rule

When `baseline_plan_source == "bootstrap_plan_baseline"`:

- treat the plan as a concrete but lint-failed baseline
- repair it locally using the reported failing spans and issue keys
- do not rewrite unrelated targets or unrelated ranges
- do not assume the baseline is already valid or reviewable

## Attempt Summary / Observability

Add to attempt summaries:

- `baseline_plan_source_for_next_repair`
- `used_bootstrap_plan_baseline` (boolean)

Add logs:

- `bootstrap_plan_baseline_set`
- `bootstrap_plan_baseline_replaced`
- `bootstrap_plan_baseline_cleared`

Recommended structured logging fields:

- `session_id`
- `attempt_number`
- `baseline_plan_source`
- `has_best_valid_candidate`
- `has_bootstrap_plan_baseline`

## Downstream Safety Rules

To avoid incorrect terminal behavior:

1. Bootstrap baselines must not be passed into:
   - `_render_selected_candidate_response(...)`
   - `_materialize_review_candidate_if_needed(...)`

2. At attempt exhaustion:
   - still prefer `best_valid_candidate`
   - then `best_invalid_candidate`
   - bootstrap baseline is ignored for final selection

3. If no comparable or invalid candidate exists:
   - existing generic preprocess failure response remains unchanged

## Detailed Control Flow

### Before First Comparable Candidate

1. Attempt 1 runs preprocess.
2. Preflight lint fails.
3. No `WorkflowCandidate` is created.
4. Store attempted plan as `bootstrap_plan_baseline`.
5. Build repair prompt with:
   - `baseline_plan_source = "bootstrap_plan_baseline"`
   - full `baseline_plan`
   - normalized `repair_context`
6. Repair scopes use the bootstrap baseline for anchor-section resolution.

### After First Comparable Candidate

1. A later attempt returns:
   - `ready`
   - `ready_with_warnings`
   - or `validation_failed_needs_review`
2. Candidate is evaluated by the normal comparator.
3. If promoted, it becomes `best_valid_candidate`.
4. Clear `bootstrap_plan_baseline`.
5. Future repair prompts use:
   - `baseline_plan_source = "best_plan_so_far"`

## Suggested Code Changes

### 1. Main Preprocess Loop

In the preprocess loop inside `handle_chat(...)`:

- add `bootstrap_plan_baseline: Optional[...] = None`

After tool execution:

- if `best_valid_candidate is None`
- and `attempted_plan` exists
- and action is `plan_lint_failed`
- set or replace `bootstrap_plan_baseline`

### 2. Baseline Selection Helper

Add a small helper to centralize repair baseline choice:

```python
def _select_repair_baseline(
    best_valid_candidate: Optional[WorkflowCandidate],
    bootstrap_plan_baseline: Optional[BootstrapPlanBaseline],
    latest_attempted_plan: Optional[Dict[str, Any]],
) -> tuple[str, Optional[Dict[str, Any]]]:
    ...
```

Return:

- source string
- baseline plan

This avoids scattering precedence logic.

### 3. Repair Prompt Builder

Update `_build_repair_planning_prompt(...)` to accept:

- `bootstrap_plan_baseline`

and to use `_select_repair_baseline(...)`.

### 4. Repair Scope Builder Call Site

Where `current_repair_scopes` is updated after a repair turn, use:

- `best_valid_candidate.plan` if present
- else `bootstrap_plan_baseline.plan` if present
- else `attempted_plan`

### 5. Promotion Path

When a comparable candidate is promoted to `best_valid_candidate`:

- clear `bootstrap_plan_baseline`

## Interaction With Existing LLD

This LLD is additive to the existing preprocess repair-loop stability LLD.

It does **not** change:

- issue fingerprinting
- comparator tuple
- out-of-scope penalty
- structural regression rejection
- latest-attempt summary format

It changes only:

- the baseline source used before the first comparable candidate exists

## Risks

### 1. Baseline Drift

If every new lint-failed plan replaces the bootstrap baseline, the model may still drift if each failed plan rewrites too much.

Mitigation:

- keep latest attempted replacement for v1
- log replacements
- later, optionally reject bootstrap replacements that are obvious full rewrites

### 2. Misuse As Final Candidate

If bootstrap baseline is accidentally treated as a real candidate, terminal behavior could become inconsistent.

Mitigation:

- keep bootstrap baseline out of candidate classes
- explicitly exclude it from terminal selection helpers

### 3. Prompt Ambiguity

If the prompt does not clearly explain bootstrap baseline semantics, the LLM may over-trust the failed plan.

Mitigation:

- explicitly label it as lint-failed but concrete

## Test Plan

1. `plan_lint_failed` creates bootstrap baseline when no `best_valid_candidate` exists.
2. second lint-failed attempt replaces bootstrap baseline before any comparable promotion.
3. repair prompt uses:
   - `baseline_plan_source = "bootstrap_plan_baseline"`
   - full baseline plan
4. repair scopes use bootstrap anchor sections when no `best_valid_candidate` exists.
5. first promoted comparable candidate clears bootstrap baseline.
6. once `best_valid_candidate` exists, repair prompt no longer uses bootstrap baseline.
7. attempt exhaustion does not try to render bootstrap baseline.
8. comparator behavior remains unchanged for comparable candidates.

## Acceptance Criteria

1. A lint-failed preprocess plan can become the concrete repair baseline before the first comparable candidate exists.
2. No lint-failed plan becomes `best_plan_so_far`.
3. Only one comparator system remains in the codebase.
4. Repair prompts before first comparable promotion use a concrete baseline plan instead of diagnostics-only fallback.
5. Existing terminal candidate selection behavior remains unchanged for valid and invalid comparable candidates.
