
# HLD: Stable Preprocess Repair Architecture for SATB Voice-Part Planning

## 1. Purpose

This design introduces a minimal yet robust stability mechanism for the preprocess repair loop used to generate SATB voice‑part preprocessing plans.

The goal is to:

- Reduce repair oscillation across attempts
- Encourage better second attempts from the LLM (proactive guidance)
- Reject worse plans automatically (reactive safety)
- Maintain compatibility with the current preprocess pipeline:
  - preflight lint
  - execution
  - post‑flight validation
  - bounded repair loop

The design also clarifies responsibilities between:

- `system_prompt.txt` (orchestration rules)
- `system_prompt_lessons.txt` (planning heuristics)

---

# 2. Current Architecture Summary

The existing preprocess planning flow is:

1. LLM generates `preprocess_voice_parts` plan
2. Backend runs **preflight lint**
3. If lint fails → repair loop
4. If lint passes → execute transform
5. Post‑flight validation runs
6. If validation fails → repair loop

Currently the repair loop uses:

    latest_attempted_plan

as the repair baseline.

This plan may:

- fail preflight lint
- never execute
- fail post‑flight validation

This causes instability because repairs are based on the **most recent failure**, not the **best known valid baseline**.

---

# 3. Root Cause of Repair Oscillation

The current repair model behaves as:

    latest attempted plan + latest failure → replan

This allows patterns like:

Attempt 1: fix structural issue  
Attempt 2: fix lyric issue but break structure  
Attempt 3: restore structure but break lyric again  

The loop lacks:

- a stable repair baseline
- preservation of previously satisfied constraints

---

# 4. Core Design Change

Introduce a new controller concept:

    best_plan_so_far

This becomes the **true repair baseline**.

Important distinction:

| Term | Meaning |
|-----|--------|
| latest_attempted_plan | most recent LLM plan regardless of outcome |
| best_plan_so_far | best accepted executable plan |
| candidate_plan | plan currently under evaluation |

---

# 5. Definition of best_plan_so_far

A plan can become `best_plan_so_far` only if:

1. **Preflight lint passes**
2. **Execution succeeds**
3. **Post‑flight validation completes**
4. **Outcome status is usable**
5. **Plan quality is better than the previous best**

Eligible post‑flight statuses:

- `ready`
- `ready_with_warnings`
- `action_required: validation_failed_needs_review`

Ineligible statuses:

- `action_required` for structural hard-fail or any non-reviewable action
- `error`

Rationale:

- `validation_failed_needs_review` already represents an executed, materialized derived candidate.
- It is surfaced to the UI as `review_required=true` and can drive score review.
- It is therefore a usable repair baseline, even though it is not auto-accepted.
- `plan_lint_failed` remains ineligible because no executable candidate was produced.

---

# 6. Post‑Flight Outcome Interpretation

Possible outcomes from post‑flight validation:

| Status | Meaning |
|------|--------|
| ready | validation passed cleanly |
| ready_with_warnings | usable result but non‑blocking issues |
| action_required: validation_failed_needs_review | usable derived candidate exists, but lyric coverage needs manual/LLM review |
| action_required: structural_validation_failed | executed output is not synthesis-safe and must not be used as a baseline |
| error | execution failure |

Only these are eligible for baseline promotion:

    ready
    ready_with_warnings
    action_required: validation_failed_needs_review

These are not acceptable repair baselines:

    action_required: structural_validation_failed
    error

---

# 7. Minimal State Maintained by Backend

The repair controller maintains:

```
best_plan_so_far
best_plan_quality_summary

fixed_structural_p1_issue_codes
fixed_other_p1_issue_codes

latest_attempted_plan
```

This intentionally avoids heavy repair history tracking.

---

# 8. Repair Loop Architecture

## 8.1 Candidate Generation

The LLM generates:

```
candidate_plan
```

which becomes:

```
latest_attempted_plan
```

---

## 8.2 Preflight Lint Stage

### If lint fails

- candidate rejected
- best_plan_so_far unchanged
- lint findings returned to repair prompt
- repair loop continues

### If lint passes

Candidate proceeds to execution.

---

## 8.3 Execution

Plan executes and produces:

- derived score
- validation diagnostics

---

## 8.4 Post‑Flight Validation

If result status is:

### error

Candidate rejected.

### action_required: structural_validation_failed

Candidate rejected but findings are returned to repair loop.

### action_required: validation_failed_needs_review

Candidate is reviewable and eligible for comparison/promotion to `best_plan_so_far`.

### ready / ready_with_warnings

Candidate becomes eligible for comparison.

---

# 9. Plan Quality Summary

Each executed candidate produces a summary:

```
structural_p1_issue_codes
other_p1_issue_codes
p2_issue_codes

structural_p1_union_affected_measure_count
other_p1_union_affected_measure_count
p2_union_affected_measure_count

plan_delta_size
```

Important rule:

Affected measure counts use **union of measures**, not raw issue count.

---

# 10. Plan Comparison Policy

Plans are compared lexicographically:

```
(
 structural_p1_union_affected_measure_count,
 other_p1_union_affected_measure_count,
 p2_union_affected_measure_count,
 plan_delta_size
)
```

Lower is better.

Priority:

1. structural correctness
2. lyric correctness
3. warning reduction
4. minimal plan changes

---

# 11. Hard Regression Protection

Candidate must be rejected if:

```
candidate introduces issue_code ∈ fixed_structural_p1_issue_codes
```

This ensures structural fixes are never undone.

---

# 12. Updating Fixed Issue Sets

When a candidate becomes the new best plan:

```
fixed_structural_p1_issue_codes +=
    previous_best.structural_p1_issue_codes
    − new_best.structural_p1_issue_codes
```

Similarly for `fixed_other_p1_issue_codes`.

This tracks which issues have been eliminated.

---

# 13. Proactive Stability Design

Reactive rejection alone is insufficient.

The repair prompt must encourage the LLM to produce better candidates.

Key proactive rules:

1. Repair from **best_plan_so_far**
2. Treat repair as **minimal‑delta revision**
3. Include **preserve constraints**
4. Limit edits to failing sections
5. Require **self‑check reasoning**

---

# 14. Minimal‑Delta Revision Mode

Even though the executor requires a **full plan artifact**, the model should behave as if performing a patch.

Conceptual behavior:

- preserve existing valid sections
- modify only failing spans
- avoid global redesign
- return full plan serialization

---

# 15. Preserve Constraints

Repair prompts should include:

### Previously Fixed Issues

- fixed structural issues
- fixed lyric issues

### Core Preprocess Invariants

Examples:

- do not reduce valid visible target coverage
- do not modify unrelated voice assignments
- do not alter lyric sourcing outside repair scope
- preserve valid behavior in unaffected measures

---

# 16. Scope‑Limited Repair

Repairs should modify only:

- failing measure spans reported by lint
- dependent sibling sections required for completeness

This prevents unnecessary changes.

---

# 17. Self‑Check Requirement

Before returning a plan, the LLM should verify:

- what sections changed
- why those changes fix the issue
- which sections remain unchanged
- that preserved constraints still hold

---

# 18. Prompt Architecture

Two prompt files must have **clear responsibilities**.

---

# 19. system_prompt.txt Responsibilities

This file defines:

- orchestration rules
- persona
- tool calling rules
- workflow sequencing
- repair loop protocol

Examples from the current prompt include workflow control and tool contracts.

Planning heuristics should **not** live here.

---

## Required High‑Level Changes

1. Introduce `best_plan_so_far` concept  
2. Treat `last_preprocess_plan` as diagnostic context, not repair baseline  
3. Emphasize minimal‑delta revision behavior  
4. Remove duplicated planning heuristics

---

# 20. system_prompt_lessons.txt Responsibilities

This file contains planning guidance such as:

- chord splitting strategy
- lyric propagation
- section design
- repair heuristics

This is the correct location for these rules.

---

## Required High‑Level Changes

1. Explicitly align repair guidance with backend repair policy
2. Emphasize minimal‑delta repair philosophy
3. Clarify structural‑before‑lyric priority
4. Consolidate all musical planning heuristics here

---

# 21. Repair Information Flow

Each repair turn should provide the LLM with:

```
best_plan_so_far
latest_attempted_plan
lint findings
repair_context metadata
```

The model should:

- repair from `best_plan_so_far`
- fix the latest failures
- preserve existing valid sections

---

# 22. Edge Case: No Best Plan Yet

If no plan has reached `ready` or `ready_with_warnings` yet:

- `best_plan_so_far` remains null
- repairs are based on latest failure findings
- once a valid candidate appears it becomes the baseline

---

# 23. Expected Benefits

This architecture provides:

- fewer oscillating repairs
- higher quality second attempts
- deterministic plan acceptance
- minimal backend complexity
- clear prompt responsibilities

---

# 24. Implementation Effort

Low complexity changes:

Backend:
- add best_plan_so_far tracking
- implement candidate comparator
- track fixed issue codes

Prompt:
- clarify baseline concept
- separate orchestration vs planning guidance

---

# 25. One‑Sentence Architecture Summary

The improved repair loop anchors repair generation to the **best executable candidate plan**, proactively guides the LLM toward minimal‑delta revisions, and reactively rejects candidates that worsen structural or lyric correctness using measure‑based quality comparison.
