# SIG-6: OpenUtau/DiffSinger Timing Parity Design

## 1. Objective

Bring our V2 alignment/timing flow to match OpenUtau DiffSinger behavior end-to-end, instead of a hybrid implementation.

Primary success target:
- Remove broad onset-consonant overextension regressions in V2.
- Keep existing wins:
  - split-word pronunciation continuity (`ba-by` sounds like one word),
  - slur-note audibility fix (last slur note remains audible).

This version includes review decisions and approved rollout constraints.

## 2. Reference Behavior (OpenUtau)

Reference implementation:
- `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs`

Key mechanics to mirror:
1. Build `phrasePhonemes` by note/group with vowel-start assignment rules:
   - `ProcessWord(...)` logic, including consonant-glide-vowel handling.
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:203`
2. Build model inputs from grouped structure:
   - `tokens`, `word_div`, `word_dur`, `ph_midi`.
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:327`
3. Run duration model and get predicted per-phoneme durations.
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:380`
4. Build alignment points from note anchors and stretch segment-by-segment by ratio.
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:408`
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:425`
5. Convert aligned positions back to per-note phoneme offsets.
   - Ref: `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs:430`

Important boundary:
- OpenUtau DiffSinger does not implement an explicit hard consonant cap stage.
- Timing is model-driven + anchor alignment driven.

## 3. Current Gaps vs OpenUtau

Current local path:
- Aligner V2: `src/api/syllable_alignment.py`
- Timing V2 runtime: `src/api/synthesize.py`

Observed gaps:
1. Anchor domain mismatch:
   - Current V2 constructs per-group anchors from local grouped note windows in a way that is not 1:1 with OpenUtau `phrasePhonemes` boundaries.
2. Grouping semantics drift:
   - Prefix/onset carry and note-group boundaries are not fully equivalent to OpenUtau `ProcessWord`.
3. Two-stage scaling mismatch:
   - We currently combine `predict_durations._align_durations(...)` (word-level scaling) plus optional anchor rescale.
   - OpenUtau anchor alignment is the dominant shaping step after model prediction in its phonemizer flow.
4. Contract/debug coupling:
   - V2 payload still exposes debug timing fields mixed with canonical fields, which complicates parity validation.

## 4. Design Principles

1. Model-driven timing first:
   - No ad-hoc onset shrink heuristic in parity mode.
2. Anchor-constrained alignment second:
   - Use OpenUtau-style segment ratio alignment to note anchors.
3. Exact frame conservation:
   - Total runtime phoneme frames must equal runtime note-frame budget.
4. Deterministic behavior:
   - Same input score + model output => same final durations.
5. Preserve proven fixes:
   - Keep split-word continuity and slur audibility behavior as hard non-regression constraints.
6. Listening-first acceptance:
   - Manual A/B listening quality is the primary gate; proxy metrics such as `word_lyric_coverage_ratio` are secondary during parity stabilization.

## 5. Proposed Parity Architecture

## 5.1 Alignment Contract (V2)

`src/api/syllable_alignment.py` should output canonical parity-ready structures:
- `phoneme_ids`, `language_ids`
- `word_boundaries`
- `group_anchor_frames` (strictly derived from OpenUtau-equivalent group boundaries)
- `group_note_indices`
- `note_durations` (runtime note timeline domain)
- optional `phonemes` for diagnostics only

Debug fields remain optional and clearly namespaced:
- `*_debug` only, never used by runtime logic.

## 5.2 Runtime Timing Flow

In `src/api/synthesize.py` parity mode:
1. Build alignment groups (already from V2 aligner).
2. Predict durations.
3. Apply anchor alignment using OpenUtau-equivalent per-segment ratio/stretch semantics.
4. Apply required post-processes that are already validated fixes:
   - coda/slur audibility adjustments (existing fix path),
   - no extra onset duration heuristic.

## 5.3 Duration Scaling Ownership

To avoid double-shaping mismatch:
- Introduce one clear owner for final timing shaping in parity mode.
- Either:
  - A) keep `predict_durations._align_durations` minimal and let anchor stage shape final timing, or
  - B) align `predict_durations` behavior with OpenUtau model-input semantics and keep anchor stage as pure positional fitting.

Locked decision: A (anchor stage is final shaper in `synthesize.py` parity mode).

## 6. Change List (Implementation Plan)

## Phase 0: Baseline Lock
- Freeze known-good baseline outputs for:
  - `assets/test_data/my-tribute.mxl` (female voice part 1),
  - `assets/test_data/amazing-grace.mxl` (soprano),
  - `assets/test_data/all-i-want-for-christmas-is-you-mariah-carey.mxl` (single voice).
- Keep A/B artifacts in `tests/output/voice_parts_e2e/` with explicit suffixes.

## Phase 1: Grouping and Anchor Parity
- Refactor V2 group construction to mirror `ProcessWord(...)` boundary semantics exactly.
- Rebuild `group_anchor_frames` from the same group boundary model used for duration alignment.
- Ensure short/zero-beat notes remain in conservation domain (coerced to >=1 frame if needed).
- For short-anchor merge fallback, enforce deterministic policy:
  - nearest vowel first,
  - tie-break left-to-right.

## Phase 2: Timing Shaping Unification
- Define one final timing owner in parity mode (recommended: anchor stage).
- Remove duplicate/competing rescale behavior in parity mode.
- Keep non-parity path behavior unchanged.

## Phase 3: Contract Hardening
- Separate canonical output vs debug-only output fields.
- Enforce invariants:
  - `sum(durations) == sum(runtime_note_durations)`
  - per-group `sum(group_durations) == anchor_total`
  - `sum(word_boundaries) == phoneme_count`

## Phase 4: Regression and Listening Gate
- Run corpus matrix:
  - baseline,
  - aligner_v2 only,
  - timing_v2 only,
  - both_on (parity mode candidate).
- Acceptance by manual A/B listening:
  - no broad onset-consonant elongation,
  - split-word continuity preserved,
  - slur last-note audibility preserved.
- Metric policy during this phase:
  - temporary drift in `word_lyric_coverage_ratio` is acceptable if listening quality improves and contract invariants hold.

## Phase 5: Rollout
- Enable parity mode in e2e test setup first.
- Keep production default unchanged until listening gate passes.
- Promote to local default only after review sign-off.
- Rollout order is locked:
  - e2e first,
  - local default second,
  - production last.

## 6.1 Slur Handling Parity Check

Before final parity sign-off:
1. Verify whether OpenUtau DiffSinger has any explicit slur-note anti-shortening logic beyond base timing alignment.
2. If not present (or insufficient for our corpus), retain our existing slur audibility fix as a bounded "Post-Parity Polish" layer.
3. Post-parity polish must not alter split-word continuity behavior.

## 6.2 Action Required Contract

`action_required` for timing infeasibility remains surfaced via `preprocess_voice_parts` repair loop only.
No new mid-synthesis hard-stop path should be introduced for this class of issue.

## 7. Non-Regression Rules

Must not regress:
1. Split-word continuity:
   - `ba-by`, `glo-ry`, similar split syllables should sound connected, not two disconnected words.
2. Slur-note audibility:
   - final sustained/slur note remains audible and not swallowed.
3. Voice-part preprocessing behavior:
   - no changes to plan/execute flow semantics unless explicitly required.

## 8. Review Decisions (Locked)

1. Metric drift:
   - accepted during parity stabilization; listening quality has priority.
2. Timing owner:
   - Option A approved; anchor alignment in `synthesize.py` is final shaper.
3. Rollout order:
   - approved as e2e first -> local default -> production.
4. Consonant merge determinism:
   - nearest vowel first, tie-break left-to-right.
5. `action_required` path:
   - preprocess repair loop only.

## 9. Deliverables

1. Updated design docs:
   - this file,
   - update `docs/specs/SIG-6-ds-anchored-phoneme-timing-design.md` with finalized parity decisions.
2. Updated task list:
   - refresh `docs/specs/SIG-6-ds-anchored-phoneme-timing-task-list.md` with phase checkpoints above.
3. Test evidence bundle:
   - matrix run log + output artifact index under `tests/output/voice_parts_e2e/`.
