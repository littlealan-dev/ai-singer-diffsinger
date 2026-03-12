# SIG-6: DiffSinger Anchored Phoneme Timing Design

## 1. Goal
Fix overlong onset consonants on long notes (for example `things`, `me`) while keeping split-word pronunciation improvements from V2.

Adopt the `DiffSingerBasePhonemizer.ProcessPart` strategy:
- model-driven phoneme durations
- note-anchor constrained stretching
- per-word/per-note timing consistency

## 2. Problem
Current V2 alignment (`src/api/syllable_alignment.py`) emits debug per-phoneme durations with a naive rule:
- head phonemes get `1` frame
- final phoneme gets remaining frames

Although synth currently still uses model-predicted durations downstream, this design gap causes:
- weak timing contract semantics
- hard-to-reason consonant timing behavior
- mismatch from OpenUtau DiffSinger reference behavior

## 3. Reference Behavior (OpenUtau DiffSinger)
From `DiffSingerBasePhonemizer.ProcessPart`:
1. Build phrase phoneme list with note-level anchor points.
2. Run linguistic and duration models for phoneme duration prediction.
3. Build alignment anchors from score timing.
4. Stretch predicted durations segment-by-segment to meet note anchor times.
5. Emit per-note/per-phoneme positions.

Key property:
- consonant timing is model-driven and anchor-constrained, not fixed frame heuristics.

Important distinction for this project:
- OpenUtau can effectively realize vowel-anchored behavior by moving leading consonants across boundaries in some flows.
- This design adopts a pragmatic first step: note-anchored proportional scaling inside existing group windows.
- Strict vowel-onset shifting is deferred to a future phase.

## 4. Scope
In scope:
- Replace naive V2 contract duration generation with anchored timing generation.
- Preserve existing split-word syllable assignment logic.
- Keep derived MusicXML as UI artifact only.

Out of scope:
- Replacing inference models.
- Redesigning plan/preflight lint.
- Full OpenUtau parity for all phonemizer plugins.

## 5. Proposed Architecture
## 5.1 New module responsibilities
`src/api/syllable_alignment.py` should produce:
- phoneme sequence + note association
- note anchor map (start/end frame by note/group)
- word/note boundaries

Then a new timing helper in `src/api/synthesize.py` (or dedicated module) computes:
- phoneme-level timing using model-predicted durations
- anchor-constrained rescaling
- stable per-phoneme positions/durations
- exact integer frame conservation per anchor window

## 5.2 Data flow
1. `align(...)` returns symbolic structure only:
- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `note_phonemes`
- `note_slur`
- `group_note_indices`
- `group_anchor_frames` (`start_frame`, `end_frame`)

2. After `predict_durations(...)` returns phoneme duration predictions:
- run `apply_anchor_constrained_timing(...)`
- compute final phoneme `positions` and `durations`

3. Use final durations for downstream pitch/variance synthesis inputs.

## 6. Timing Algorithm
## 6.1 Inputs
- predicted phoneme durations (frames) from duration model
- aligned phoneme sequence index ranges by note-group
- group anchor frames (`group_start`, `group_end`)

Definitions:
- `anchor_total = group_end - group_start` is the only legal frame budget for the group.
- Phase 1 aligns to note/group onsets, not strict vowel onsets.

## 6.2 Steps
1. Partition predicted phoneme durations by group.
2. For each group:
- `pred_total = sum(pred_group_durations)`
- `anchor_total = group_end - group_start`
- compute `ratio = anchor_total / pred_total`
- rescale each phoneme duration by `ratio`
3. Normalize to integer frames:
- minimum `1` frame per phoneme
- apply largest-remainder (or equivalent cumulative-error) rounding so sum matches exactly
4. Derive cumulative positions from group start.
5. Validate frame conservation per group and globally.

### 6.2.1 Physical impossibility handling (short anchors)
When `num_phonemes > anchor_total`, exact `>=1 frame per phoneme` is impossible.

Policy:
1. Emit validation signal `insufficient_anchor_budget`.
2. Fallback merge strategy within the same group:
- merge adjacent consonants first,
- then merge non-primary consonants into nearest vowel-bearing segment,
- preserve at least one vowel-bearing segment when present.
3. Re-run rounding with merged segments until feasible.
4. If still infeasible, mark `action_required` for repair loop instead of producing invalid timing.

## 6.3 Consonant control
Optional guardrails (feature-flagged):
- consonant max ratio cap for very long notes
- preserve minimum vowel share in each group

Default first phase:
- no hard-coded cap
- rely on model prediction + anchor scaling (matches DiffSinger reference intent)

Future refinement (deferred):
- strict vowel-onset anchoring by moving leading consonants to previous anchor window.
- this is intentionally out of initial rollout scope.

## 7. Contract changes
`align(...)` output changes:
- remove naive final `durations` as canonical field
- keep `durations_debug` only if needed
- add anchor metadata required for post-duration timing

New timing output (post-duration stage):
- `phoneme_positions`
- `phoneme_durations`

## 8. Integration plan
Phase 1:
- add anchor metadata to V2 aligner output
- keep current runtime behavior unchanged

Phase 2:
- implement `apply_anchor_constrained_timing(...)`
- gate with `SYLLABLE_TIMING_V2=true`
- implement largest-remainder integer rounding and short-anchor fallback policy

Phase 3:
- switch synth runtime to anchored phoneme timing
- keep fallback path one release cycle

Phase 4:
- remove naive per-phoneme duration generation in aligner

Phase 5 (optional):
- evaluate strict vowel-onset mode behind separate flag (`SYLLABLE_VOWEL_ONSET_ALIGN=true`).

## 9. Validation
## 9.1 Unit tests
- `tests/test_syllable_alignment_anchor_timing.py`
Cases:
- long-note single-word (`me`)
- onset-heavy word (`things`)
- split words (`voic-es`, `grat-i-tude`)
- mixed slur/tie groups

Assertions:
- group frame conservation
- no negative/zero phoneme durations
- onset consonant durations remain bounded relative to vowel in long notes
- short-anchor impossibility path triggers expected fallback/flag behavior

## 9.2 Integration tests
- My Tribute soprano in-memory synthesis path
- Amazing Grace baseline regression

Checks:
- pronunciation remains improved for split words
- beat alignment does not drift
- onset consonants are no longer unnaturally prolonged
- no group violates anchor frame budget after integer rounding

## 10. Risks
- Duration model may still over-allocate consonants for specific words.
Mitigation:
- add optional consonant-vowel ratio post-pass (flagged).

- Anchor metadata bugs may shift indices.
Mitigation:
- explicit invariant tests for sorted-group offset mapping.

## 11. Acceptance criteria
1. My Tribute soprano:
- `glo-ry`, `voices` keep improved pronunciation.
- `things`, `me` no longer exhibit repeated/overlong onset consonants.
2. No regression in full synthesis test (`tests/test_end_to_end.py::test_full_synthesis`).
3. In-memory transformed score remains synthesis source of truth.
4. For every group, `sum(group_phoneme_durations) == anchor_total` holds exactly.
5. For short anchors, system emits deterministic fallback or explicit `action_required`; never silent invalid timing.
