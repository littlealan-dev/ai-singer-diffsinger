# SIG-6 Preflight Plan Lint Rules

## Purpose
Validate timeline-section plans before execution so we catch plan mistakes at score level, not after transform.

## Rule 1: `section_timeline_contiguous_no_gaps`
In sheet-score terms:
- A planned vocal line cannot "skip" bars accidentally.
- If the line plan starts at bar N and ends at bar M, every bar between N..M must belong to exactly one section.
- No holes, no overlaps.

Why:
- Prevents missing bars caused by wrong section boundaries.

## Rule 2: `no_rest_when_target_has_native_notes`
In sheet-score terms:
- If the requested line (for example, Tenor) has notes in the original score at bar X,
  that bar cannot be planned as `rest` for that same requested line.

Why:
- Prevents silencing bars where the target singer is clearly written to sing.

Notes:
- This rule only checks the target line itself.
- It does not inspect sibling lines.

## Rule 3: `same_clef_claim_coverage`
In sheet-score terms:
- For one staff/part group (for example, Men staff), if the original source has sung material in bar X,
  at least one derived line plan in that same group must claim bar X as `derive`.
- If source sings but no derived line claims that bar, lint fails.

Why:
- Prevents dropping music from the staff group during split planning.
- Acts like a conductor check: "who is singing this bar?"

## Rule 4: `same_part_target_completeness`
In sheet-score terms:
- If a staff/part has multiple split lines (for example, two independent lines in Men staff),
  a timeline plan must explicitly include all non-unison split lines as targets.
- Example: if Men has `voice part 1` and `voice part 3`, planning only one of them fails this rule.

Why:
- Avoids partial planning where one sibling line is never assigned any timeline sections.
- Enables reliable group checks for dropped bars.

## Rule 5: `cross_staff_melody_source_when_local_available`
In sheet-score terms:
- If the target staff already has local notes in a section, plan cannot copy melody from another staff for that section.
- Example: Tenor section has men notes in bars 37-41, so melody_source must stay in Men staff.

Why:
- Prevents accidental melody drift across staves when local written material exists.

## Rule 6: `cross_staff_lyric_source_when_local_available`
In sheet-score terms:
- If the target staff has local word-lyrics in a section, plan cannot copy lyrics from another staff for that section.
- Example: Men unison has real words in bars 37-41, so tenor/bass lyric_source should remain in Men staff.

Why:
- Prevents text-source mistakes when local lyrics are available.

## Lint Outcome
- If any rule fails, preprocessing returns:
  - `status: action_required`
  - `action: plan_lint_failed`
  - `lint_findings`: structured rule violations
- Execution does not start until plan is corrected.
