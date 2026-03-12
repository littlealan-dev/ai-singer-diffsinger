# Preprocess Repair Loop Stability LLD

## 1. Purpose

Define the low-level design for stabilizing the `preprocess_voice_parts` repair loop so that later attempts repair failing spans without oscillating between previously fixed structural and lyric issues.

This document implements the decisions in [preprocess_repair_loop_hld.md](/Users/alanchan/antigravity/ai-singer-diffsinger/docs/specs/preprocess_repair_loop_hld.md).

## 2. Scope

This change covers:

- preprocess repair state inside the backend workflow controller
- candidate eligibility and comparison
- normalized repair-context payloads for LLM follow-up turns
- plan-diff and in-scope/out-of-scope change detection
- prompt updates for minimal-delta repair behavior

This change does not cover:

- new lint rules
- per-measure health maps
- deterministic music processing changes
- new UI review flows

## 3. Current Problem

The current loop is effectively:

```text
latest_attempted_plan + latest failure -> replan
```

That is unstable because:

- the latest attempted plan may already be worse than a previous candidate
- the latest failure does not preserve all previously fixed constraints
- the model can repair one P1 class while regressing another

Observed pattern:

1. fix structural coverage
2. fix lyric source and regress structure
3. restore structure and regress lyric source

## 4. Design Summary

Introduce a backend-controlled repair baseline:

```text
best_plan_so_far
```

The backend will:

- keep `best_plan_so_far` as the only full baseline injected to the LLM
- keep `latest_attempted_plan` only as a compact summary
- build normalized `repair_context` from deterministic findings
- compare eligible executed candidates lexicographically
- penalize out-of-scope rewrites through comparator ranking

The LLM will:

- repair from `best_plan_so_far`
- preserve valid sections
- prefer local edits in the failing target/span only

If `best_plan_so_far` does not yet exist, the backend temporarily uses the full latest attempted plan as the repair baseline for the next turn.

## 5. Candidate Types

### 5.1 Non-comparable candidates

These never participate in `best_plan_so_far` comparison:

- preflight lint failures
- execution failures
- `action_required: structural_validation_failed`

They still generate a `repair_context_summary`.

### 5.2 Comparable candidates

These are eligible for comparison and promotion to `best_plan_so_far`:

- `ready`
- `ready_with_warnings`
- `action_required: validation_failed_needs_review`

Rationale:

- `validation_failed_needs_review` is already an executed, materialized derived candidate
- it already surfaces to the UI as reviewable
- it is a usable repair baseline even though it is not auto-accepted

## 6. State Model

Per preprocess workflow, backend maintains only:

```text
best_plan_so_far
best_plan_quality_summary
latest_attempted_plan_summary
latest_attempted_outcome_summary
latest_repair_context
fixed_structural_p1_issue_keys
fixed_other_p1_issue_keys
```

It does not maintain full attempt history.

### 6.1 `best_plan_so_far`

Stored as the full normalized preprocess plan object from the best eligible candidate.

### 6.2 `best_plan_quality_summary`

Stored as the comparator data for `best_plan_so_far`.

### 6.3 `latest_attempted_plan_summary`

Compact summary only. It should include:

- outcome stage:
  - `preflight_failed`
  - `postflight_reviewable`
  - `postflight_unusable`
  - `ready`
- changed scope vs `best_plan_so_far`
- main failure findings
- rejection / non-promotion reason

The full latest attempted plan is not injected to the LLM unless there is no `best_plan_so_far` yet.

### 6.4 Bootstrap behavior when no `best_plan_so_far` exists

Before the first comparable candidate is established, prompt payload should include:

- full latest attempted plan
- normalized `repair_context`

It should not require:

- `best_plan_so_far`
- `latest_attempted_plan_summary` as the only baseline

Rationale:

- before any comparable candidate exists, the model still needs a concrete plan baseline to repair from

## 7. Normalized Issue Fingerprint

Fixed issues and regressions must be tracked by scoped issue key, not by rule code alone.

### 7.1 Issue key shape

Use a normalized fingerprint with only stable, meaningful fields for the rule:

```json
{
  "rule_code": "same_part_chord_source_underclaimed_by_visible_targets",
  "severity_bucket": "P1",
  "part_index": 0,
  "target_voice_part_id": "alto",
  "source_part_index": 0,
  "source_voice_part_id": "voice part 2",
  "affected_spans": [{"start": 12, "end": 15}]
}
```

### 7.2 Required dimensions

V1 issue key construction should use:

- `rule_code`
- `severity_bucket`
- normalized affected measure spans
- relevant target identity when present
- relevant source identity when present

This yields:

- `fixed_structural_p1_issue_keys`
- `fixed_other_p1_issue_keys`

instead of rule-code-only sets.

## 8. Repair Context vs Quality Summary

V1 separates two backend summaries.

### 8.1 `repair_context_summary`

Can be built for:

- preflight lint failures
- post-flight unusable failures
- comparable executed candidates

Used for:

- prompt guidance
- repair targeting
- preserve constraints
- scope restriction

### 8.2 `plan_quality_summary`

Can be built only for comparable executed candidates:

- `ready`
- `ready_with_warnings`
- `validation_failed_needs_review`

Used for:

- comparator ranking
- `best_plan_so_far` promotion
- fixed-issue tracking

## 9. Candidate Quality Summary

Each comparable candidate produces:

```text
structural_p1_union_affected_measure_count
other_p1_union_affected_measure_count
p2_union_affected_measure_count
out_of_scope_changed_section_count
plan_delta_size
```

Affected measure counts use union of impacted measures, not raw issue count.

### 9.1 `plan_delta_size`

`plan_delta_size` is:

- the number of target-section entries whose semantic content changed relative to `best_plan_so_far`

A section counts as changed if any execution-relevant field changed, such as:

- target identity
- `start_measure` / `end_measure`
- `mode`
- melody source selection
- lyric source selection
- strategy
- lyric policy
- extraction method
- split parameters

Do not use:

- raw JSON diff size
- line count diff
- character diff

## 10. In-Scope vs Out-of-Scope Change Detection

V1 uses a deliberately simplified repair-scope definition.

### 10.1 Inputs

For each failing finding, backend records:

- target identity
- failing measure span

For each failing span, backend also finds the overlapping section(s) in `best_plan_so_far` on the same target.

These are the repair anchor sections.

### 10.1.1 `RepairScope`

V1 should define a small internal structure now:

```text
RepairScope
{
  target_voice_part_id
  failing_spans
  anchor_sections
}
```

This keeps v1 simple while leaving room for future scope refinements.

### 10.2 Anchor-section rule

Anchor sections are:

- all best-plan sections on the same target whose measure span intersects the failing span

If the best plan already contains multiple split sections across the failing region, all intersecting sections are anchors.

### 10.3 In-scope change rule

A changed candidate section is in-scope if:

1. it is on the same target, and
2. it overlaps the failing span

or:

1. it is on the same target, and
2. it overlaps a repair anchor section from `best_plan_so_far`

This allows local reshaping of the original failing section.

### 10.4 Out-of-scope change rule

Everything else is out-of-scope.

Examples:

- changing another target
- changing a far-away section on the same target
- changing unrelated lyric policy elsewhere
- changing a different similar-looking region not named by diagnostics

### 10.4 Why anchor sections are used

Without anchor sections, splitting a single best-plan section into several local sections would look like unrelated churn.

Anchor sections allow:

- `best_plan_so_far`: Alto `10-20`
- failing span: Alto `12-15`
- candidate: Alto `10-11`, `12-15`, `16-20`

All of those changes remain in-scope.

### 10.6 Section identity for diffing

Section identity is determined by:

```text
(target_voice_part_id, start_measure, end_measure)
```

If this identity changes, diff logic treats it as:

- deletion of the old section
- addition of the new section

This keeps changed-section detection deterministic.

## 11. Comparator Policy

Candidates are ranked lexicographically:

```text
(
  structural_p1_union_affected_measure_count,
  other_p1_union_affected_measure_count,
  p2_union_affected_measure_count,
  out_of_scope_changed_section_count,
  plan_delta_size
)
```

Lower is always better.

This means:

1. structural correctness dominates
2. lyric correctness is next
3. warning reduction is next
4. out-of-scope rewrites are penalized
5. smaller overall semantic change wins as final tiebreak

If all comparator tuple fields are equal, outcome class breaks ties in this order:

1. `ready`
2. `ready_with_warnings`
3. `validation_failed_needs_review`

So a fully ready candidate outranks a reviewable candidate when the measured quality buckets are otherwise equal.

### 11.1 No separate scoring system

V1 must not introduce weighted numeric scoring.

Do not use:

- `score = a * 1000 + b * 100 + ...`

Lexicographic ordering is deterministic and easier to reason about.

### 11.2 Comparator effect of out-of-scope edits

`out_of_scope_changed_section_count` is a comparator penalty only.

It is not a separate scoring system and not an immediate reject by itself in normal cases.

## 12. Regression Protection

### 12.1 Hard backend policy

Structural priority is not prompt-only. Backend must enforce it.

Rules:

- reintroduction of previously fixed structural issue keys is a regression
- lyric improvement never justifies structural regression
- structural bucket dominates lyric bucket in comparator ranking

### 12.2 Promotion rule

A candidate may become `best_plan_so_far` only if:

- it is a comparable candidate
- its comparator tuple is better than the current best
- it does not violate hard regression rules

## 13. Hard Reject Rules

V1 should not reject candidates only because they have a small number of out-of-scope changes.

V1 should reserve hard reject for obvious plan destruction, such as:

- out-of-scope changed section count above a high threshold
- most sections rewritten
- most measure coverage rewritten

Default v1 thresholds:

- `MAX_OUT_OF_SCOPE_SECTIONS = 10`
- `MAX_SECTION_CHANGE_RATIO = 0.7`
- `MAX_MEASURE_CHANGE_RATIO = 0.8`

These should be implementation constants and logged.

Default v1 stance:

- penalize out-of-scope rewrites through comparator ranking
- hard-reject only clear full-plan rewrite behavior

## 14. Prompt Payload Design

When prompting the LLM for a repair attempt, inject:

1. full `best_plan_so_far` when available
2. compact `latest_attempted_plan_summary`
3. normalized `repair_context`

Do not inject the full latest attempted plan by default once a best baseline exists.

### 14.1 `repair_context` contents

V1 `repair_context` is built from deterministic findings only:

- preflight lint findings
- post-flight findings
- normalized failing spans
- relevant source/target identities
- repair anchor sections

Per-measure health maps are explicitly out of scope for v1.

### 14.2 Prompt rules

Prompt instructions must tell the LLM to:

- repair from `best_plan_so_far`
- preserve valid sections
- edit only failing spans and anchor-local reshapes
- preserve structural correctness over lyric completeness
- avoid global rewrites
- do not modify targets other than the failing target unless necessary to maintain section continuity

## 15. Backend Data Structures

### 15.1 `RepairContextSummary`

Suggested shape:

```json
{
  "attempt_number": 2,
  "outcome_stage": "preflight_failed",
  "findings": [
    {
      "issue_key": {
        "rule_code": "same_part_chord_source_underclaimed_by_visible_targets",
        "severity_bucket": "P1",
        "part_index": 0,
        "target_voice_part_id": "alto",
        "source_part_index": 0,
        "source_voice_part_id": "voice part 2",
        "affected_spans": [{"start": 12, "end": 15}]
      },
      "failing_spans": [{"start": 12, "end": 15}],
      "anchor_sections": [{"start_measure": 10, "end_measure": 20}]
    }
  ]
}
```

### 15.2 `PlanQualitySummary`

Suggested shape:

```json
{
  "structural_p1_union_affected_measure_count": 0,
  "other_p1_union_affected_measure_count": 1,
  "p2_union_affected_measure_count": 0,
  "out_of_scope_changed_section_count": 0,
  "plan_delta_size": 2
}
```

### 15.3 `LatestAttemptedPlanSummary`

Suggested shape:

```json
{
  "outcome_stage": "postflight_reviewable",
  "changed_sections_vs_best": 3,
  "out_of_scope_changed_sections_vs_best": 1,
  "main_failure_rules": [
    "validation_failed_needs_review"
  ],
  "not_promoted_reason": "worse_than_best_tuple"
}
```

## 16. Backend Flow

### 16.1 Candidate generation

1. LLM emits `candidate_plan`
2. backend stores compact summary as latest attempted metadata

### 16.2 Preflight stage

If preflight fails:

- build `repair_context_summary`
- do not build `plan_quality_summary`
- do not compare against `best_plan_so_far`
- follow up with LLM repair prompt

### 16.3 Execution and post-flight

If execution completes:

- normalize post-flight findings
- if candidate is comparable:
  - compute `plan_quality_summary`
  - compare against current best
  - promote if better
- regardless of promotion:
  - build `repair_context_summary` if another repair turn is needed

### 16.4 Max-attempt termination

At max attempts:

- if a `best_plan_so_far` exists, return the best candidate outcome
- if no comparable candidate exists, return the normal failure path

## 17. File-Level Changes

### 17.1 [src/backend/orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py)

Add:

- issue fingerprint builder
- repair context builder
- plan diff / changed-section detector
- in-scope vs out-of-scope classifier
- quality summary builder
- comparator logic for `best_plan_so_far`
- latest-attempt summary builder

Change:

- preprocess repair loop to anchor on `best_plan_so_far`
- prompt payload assembly to inject:
  - full best plan
  - latest attempted summary
  - normalized repair context

### 17.2 [src/backend/config/system_prompt.txt](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)

Update preprocess repair guidance to:

- repair from the best baseline
- preserve structural fixes
- prefer same-target local edits
- avoid out-of-scope rewrites

### 17.3 [src/backend/config/system_prompt_lessons.txt](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)

Update planning heuristics to reinforce:

- local repair behavior
- structural-first tradeoff
- anchor-section reshaping instead of global rewrites

### 17.4 Tests

Primary coverage can go into:

- [tests/test_backend_api.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_backend_api.py)

If helper logic grows, add focused orchestrator tests.

## 18. Logging and Observability

For every comparable candidate, log:

- `attempt_number`
- `structural_p1_union_affected_measure_count`
- `other_p1_union_affected_measure_count`
- `p2_union_affected_measure_count`
- `out_of_scope_changed_section_count`
- `plan_delta_size`
- `best_plan_tuple`
- `candidate_plan_tuple`
- comparator tuple
- decision:
  - `promoted`
  - `rejected_worse_than_best`
  - `rejected_regression`
  - `rejected_full_rewrite`

For every preflight-failed candidate, log:

- normalized failing issue keys
- anchor sections
- latest attempted outcome stage

## 19. Test Plan

### Test 1: Reviewable candidate promotion

Given a candidate with:

- `action_required: validation_failed_needs_review`
- no structural hard fail

Expected:

- candidate is comparable
- candidate may become `best_plan_so_far`

### Test 2: Preflight-failed candidate is non-comparable

Expected:

- `repair_context_summary` is produced
- `plan_quality_summary` is not
- candidate cannot become `best_plan_so_far`

### Test 3: Scoped issue fingerprinting

Two failures with the same rule in different spans should produce distinct issue keys.

### Test 4: Anchor-section in-scope reshaping

Given:

- best plan section `10-20`
- failing span `12-15`
- candidate sections `10-11`, `12-15`, `16-20`

Expected:

- all counted as in-scope

### Test 5: Out-of-scope penalty

Given equal structural/lyric/P2 quality:

- candidate A with `out_of_scope_changed_section_count = 0`
- candidate B with `out_of_scope_changed_section_count = 3`

Expected:

- A ranks higher

### Test 6: Out-of-scope before delta

Given:

- candidate A with larger in-scope delta
- candidate B with smaller delta but out-of-scope edits

Expected:

- comparator prefers A if all earlier buckets are equal

### Test 7: Structural regression rejection

Given:

- a previously fixed structural P1 issue key
- new candidate reintroduces that exact scoped issue

Expected:

- candidate rejected as regression

### Test 8: Latest attempted summary only

When `best_plan_so_far` exists:

Expected:

- prompt payload includes full best plan
- latest attempted plan is summary only

### Test 9: Full rewrite hard reject

Given a candidate that rewrites nearly the whole plan:

Expected:

- hard reject path is triggered

### Test 10: Anchor section expansion

Given:

- best plan section `10-20`
- failing span `12-15`
- candidate sections `10-14`, `15-20`

Expected:

- all changed sections are in-scope

## 20. Rollout Notes

This is a coordinated backend-plus-prompt change.

Prompt-only is insufficient because it cannot enforce regression rejection.
Backend-only is insufficient because it cannot reduce bad candidate generation.

The change should ship as one repair-loop stability unit:

- backend baseline/comparator/rejection logic
- prompt repair instructions

## 21. Open Questions

None at this stage. The v1 boundaries are intentionally narrow:

- no per-measure health summaries
- no true dependency graph
- no rule-specific solver expansion beyond current lint semantics
