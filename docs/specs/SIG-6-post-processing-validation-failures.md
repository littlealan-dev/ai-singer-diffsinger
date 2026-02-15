# SIG-6 Post-Processing Validation Failures

## Purpose
Document validation failures returned after section execution, their likely causes, and practical fixes.

## Scope
These are post-execution checks (after transform actions run), not preflight lint checks.

## Failure: `validation_failed_needs_review`
Meaning in score terms:
- The target derived line still has sung notes without usable lyrics after execution.
- Validation checks lyric coverage across sung notes and fails if unresolved gaps remain.

Typical response shape:
- `status: action_required`
- `action: validation_failed_needs_review`
- `validation.lyric_coverage_ratio`
- `validation.unresolved_measures`
- `failing_ranges`
- `section_results` (per-section copied notes/lyrics and missing counts)

## Root Cause Patterns And Fixes

### 1. Wrong lyric source for a section
Symptoms:
- `section_results[].copied_note_count > 0` but `copied_lyric_count` is low/zero.
- Missing lyrics concentrate in specific measure ranges.

Potential causes:
- Chosen lyric source has no word-lyrics in that range.
- Source is extension-heavy (`+`) while another local source has real words.

Fix:
- Split section by measure range and switch `lyric_source` only for failing bars.
- Prefer source with higher `word_lyric_coverage_ratio` in those bars.

### 2. Section over-compression
Symptoms:
- A broad section covers bars with different local lyric behavior.
- Failure happens only on a subset of bars inside one long section.

Potential causes:
- Planner merged bars that should have different actions/sources.

Fix:
- Replan in smaller contiguous sections.
- Keep one action/source tuple per homogeneous measure range.

### 3. Cross-staff sourcing that passes lint but still mismatches timing
Symptoms:
- Notes copied, lyrics still not attached in overlap/strict matching.
- Missing lyrics in bars with complex rhythm differences.

Potential causes:
- Source and target timing diverge enough that matching strategy misses note alignment.

Fix:
- Keep source local to target part/staff when possible.
- If cross-staff is unavoidable, split range and use strategy per range (`strict_onset` for exact match ranges, `overlap_best_match` for shifted ranges).

### 4. Structural constraints force conservative output
Symptoms:
- Previous plan revisions replaced problematic sections with rest.
- No notes/lyrics in bars that visually seem singable.

Potential causes:
- Earlier structural checks detected overlap/simultaneity conflicts.
- Planner adopted rest workaround in later iterations.

Fix:
- Re-evaluate those bars after structural checker changes (for example epsilon overlap tolerance).
- Remove stale rest workaround and attempt derive again.

### 5. Partial lyric expected by design
Symptoms:
- Very high lyric coverage (for example `>= 0.90`) with small unresolved set.
- Status may be `ready_with_warnings` instead of hard action required.

Potential causes:
- Isolated melisma/continuation edge cases.
- Rarely-singable text segmentation in source.

Fix:
- Accept warning if downstream quality is acceptable.
- Otherwise patch only unresolved measures with section-level overrides.

## How To Triage Quickly
1. Check `failing_ranges` and `validation.unresolved_measures`.
2. Inspect `section_results` for sections where `copied_note_count` is high but `copied_lyric_count` is low.
3. Replan only failing measure ranges; do not rewrite successful sections.
4. Re-run preprocess; if successful, keep derived XML for UI and use in-memory transformed score for synthesis.

## Important Distinction
- Preflight lint errors (`plan_lint_failed`) mean plan must be corrected before execution.
- Post-processing validation errors (`validation_failed_needs_review`) mean execution ran, but output quality gates were not met.
