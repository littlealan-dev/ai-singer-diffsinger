# SIG-6 Anchored Phoneme Timing - Implementation Task List

## Goal
Complete SIG-6 implementation anchored in OpenUtau/DiffSinger parity per `docs/specs/SIG-6-openutau-diffsinger-parity-design.md` while preserving critical improvements like split-word pronunciation (`ba-by`) and slur audibility.

## Locked Decisions
- Conservation domain: all selected notes, including zero-beat notes coerced to 1 frame.
- `action_required` surfacing: preprocessing path only (`preprocess_voice_parts` repair loop), not mid-synthesis hard stop as primary UX.
- Short-anchor merge policy: nearest vowel first; if tie, deterministic left-to-right.
- Split-word non-regression gate: listening check only.
- Minimum regression corpus: My Tribute + Amazing Grace + All I Want.
- Flag rollout order: enable in E2E test setup first (not global local defaults first).
- Onset-consonant regression acceptance: manual A/B listening only.
- Metric policy during parity stabilization: listening quality takes priority over `word_lyric_coverage_ratio` proxy drift.
- Timing owner in parity mode: anchor alignment stage in `synthesize.py` is final shaper (Option A).

## Non-Regression Rule (Must Keep)
- Do not regress split-word handling introduced in V2.
- Preserve note-level syllable chunking behavior that made `ba-by`, `glo-ry`, `voic-es` sound connected and natural.
- Any timing refactor must pass explicit split-word audio/listening checks before merge.

## Workstreams

## 1) Stabilize V2 Aligner Contract (Grouping Parity)
- [x] Rework `validate_ds_contract` in `src/api/syllable_alignment.py` so it validates invariants that are stable for real-world scores (including zero-beat/chord helper notes).
- [ ] Audit V2 grouping logic against OpenUtau `ProcessWord(...)` semantics:
  - consonant-glide-vowel mapping,
  - phoneme-to-note assignment within syllables.
- [ ] Remove false-failure path that currently triggers `frame_conservation_failed` for valid musical inputs.
- [ ] Define and document exact conservation basis:
  - `sum(final_phoneme_durations)` must match the same timing domain used by synthesis runtime.
- [x] Implement conservation using all selected notes (including zero-beat notes coerced to 1 frame).
- [x] Add structured diagnostics payload when validation fails (group index, anchor window, phoneme count, predicted sum, final sum).

## 2) Implement Spec Short-Anchor Fallback
- [x] Implement explicit `insufficient_anchor_budget` detection (fallback-only for physical budget violations, not normal timing flow).
- [ ] Implement full `insufficient_anchor_budget` fallback merge policy from design spec section 6.2.1.
- [ ] Add deterministic merge strategy:
- merge adjacent consonants first,
- merge non-primary consonants into nearest vowel-bearing segment,
- preserve at least one vowel-bearing segment where available.
- [ ] Determinism rule: nearest vowel first, tie-break left-to-right.
- [ ] Ensure synthesis path emits a typed infeasibility error (e.g. `InfeasibleAnchorError`) that `preprocess_voice_parts` translates to the user-facing `action_required` status.

## 3) Make V2 Aligner Symbolic-First (Per Design)
- [x] Refactor `src/api/syllable_alignment.py` to avoid using naive per-phoneme estimates as canonical final durations in v2 flow.
- [ ] Keep debug-only timing fields clearly marked as debug.
- [x] Ensure runtime timing authority is post-duration anchor-constrained step in `src/api/synthesize.py`.

## 4) Align Runtime Timing Flow with OpenUtau DiffSinger Intent (Alignment Parity)
- [ ] Audit phrase-level alignment against OpenUtau `ProcessPart` semantics:
  - phrase-initial pause (SP/AP) handling,
  - segment-by-segment ratio stretch behavior,
  - integer frame rounding guarantees.
- [ ] Ensure timing v2 path is model-driven + anchor-constrained without introducing hard-coded consonant caps by default.
- [ ] Keep optional guardrails feature-flagged only.
- [ ] Verify whether OpenUtau DiffSinger has explicit slur-note anti-shortening logic in the relevant path.
- [ ] If not present (or insufficient), preserve current slur audibility behavior as a bounded post-parity polish step.

## 5) Preserve Split-Word Quality Explicitly
- [ ] Add targeted regression tests for split words:
- `ba-by`, `glo-ry`, `voic-es`, `grat-i-tude`.
- [ ] Add assertions for:
- syllable-to-note chunk mapping remains stable,
- no syllable drop/duplication after timing v2.
- [ ] Add listening-test checklist artifact for My Tribute and one additional external song sample.

## 6) Expand Test Coverage to Match Spec
- [x] Add unit coverage for anchor insufficiency guard behavior in `tests/test_anchor_timing.py`.
- [ ] Add dedicated unit test module for full syllable aligner v2 contract/fallback matrix.
- [ ] Add integration tests covering:
- [ ] My Tribute female voice part 1,
- Amazing Grace soprano baseline,
- single-voice dense-pop score case (for example `all-i-want-for-christmas-is-you-mariah-carey.mxl`).
- [ ] Add matrix runs:
- baseline (both flags off),
- aligner-only,
- timing-only,
- both flags on.
- [ ] Gate rollout on matrix pass + listening-based split-word non-regression pass.

## 7) Rollout and Flags
- [ ] Keep `SYLLABLE_ALIGNER_V2` and `SYLLABLE_TIMING_V2` opt-in until matrix is green.
- [ ] Add rollout notes with known failure modes and quick-disable instructions.
- [ ] First enablement step: E2E test setup only.
- [ ] Only enable defaults after:
- no contract failures on target corpus,
- no split-word regressions,
- no onset-consonant broad regressions vs v1 (manual A/B acceptance).

## 8) Documentation Updates
- [ ] Update `docs/specs/SIG-6-ds-anchored-phoneme-timing-design.md` with:
- implemented behavior vs deferred items,
- exact conservation invariant definition,
- fallback behavior examples.
- [x] Update parity review spec with locked decisions:
- `docs/specs/SIG-6-openutau-diffsinger-parity-design.md`
- [ ] Update `docs/specs/SIG-6-score-transform-system-prompt-lessons.md` only if planning constraints need changes from timing findings.

## Exit Criteria
- [ ] `aligner_only` no longer hard-fails on representative real scores.
- [ ] `both flags on` produces valid synthesis outputs across required corpus:
  - My Tribute female voice part 1,
  - Amazing Grace soprano baseline,
  - external single-voice score case.
- [ ] Split-word pronunciation improvement is retained (explicitly verified).
- [ ] No broad onset-consonant regression compared with current stable path.
- [ ] Slur-note audibility parity/fix verified (final gate).
