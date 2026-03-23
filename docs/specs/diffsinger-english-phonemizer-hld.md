# HLD: Dedicated DiffSinger English Phonemizer Path

## Purpose

Add a new `DiffSingerEnglishPhonemizer` path alongside the current generic phonemizer flow.

The goal is to improve compatibility for English DiffSinger voicebanks that were authored and validated against OpenUtau's dedicated English phonemizer behavior, without regressing the current generic synthesis path that already works for other banks.

Primary target:
- `assets/voicebanks/Hoshino Hanami v1.0`

Motivation:
- Hoshino currently synthesizes successfully but produces unstable note pitch
- the instability is already visible before vocoding in `predict_pitch(...)`
- the voicebank explicitly declares:
  - `default_phonemizer: OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`

## Problem Summary

Current behavior:
- the backend uses one generic English phonemizer/alignment flow
- it is valid enough to produce supported phonemes
- it is not guaranteed to match the phoneme normalization and note-assignment behavior expected by a specific DiffSinger English bank

Observed consequence for Hoshino:
- the predicted pitch curve is off-target and unstable before vocoding
- V2 alignment/timing does not remove the issue
- dictionary selection fixes help correctness but do not solve the instability

Important distinction:
- this is not a generic "phonemizer broken for all banks" problem
- it is a parity problem for banks that expect the OpenUtau DiffSinger English phonemizer path

## Design Goals

1. Add a dedicated `DiffSingerEnglishPhonemizer` path without removing the existing generic phonemizer.
2. Keep the new path opt-in and bank-aware.
3. Respect voicebank-declared phonemizer intent when available.
4. Reuse as much of the current pipeline as possible after phonemization/alignment output is produced.
5. Keep the existing public synthesis API stable.

## Non-Goals

1. Do not rewrite the entire synthesis pipeline to mirror OpenUtau.
2. Do not add full support for every OpenUtau phonemizer in this change.
3. Do not change non-English bank behavior.
4. Do not replace the current generic phonemizer as the universal default.
5. Do not attempt a full OpenUtau phrase-renderer parity implementation in one step.

## Why A Separate Path Is Needed

OpenUtau's DiffSinger English phonemizer does more than basic grapheme-to-phoneme lookup.

It also determines:
- which English phoneme variants are used
- how replacements and remapping are applied
- how vowel/glide/consonant structure is distributed over notes

The current backend path does:
- dictionary lookup
- validation against the phoneme inventory
- generic alignment rules

That difference is acceptable for some banks, but not for all.

## Proposed Architecture

Introduce a new internal phonemizer strategy:

- `GenericPhonemizerStrategy`
- `DiffSingerEnglishPhonemizerStrategy`

Selection rule:
- if a voicebank declares `default_phonemizer: OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`
- and synthesis language is English
- use the new dedicated path

Otherwise:
- continue using the current generic path

## Integration Point

The new strategy should sit before duration and pitch prediction.

High-level flow:

1. Parse score
2. Select synthesis phonemizer strategy
3. Produce phoneme sequence and note/word alignment payload
4. Pass the resulting payload into the existing:
   - `predict_durations(...)`
   - `predict_pitch(...)`
   - `predict_variance(...)`
   - `synthesize_audio(...)`

This keeps the new change localized to:
- phoneme generation
- phoneme-to-note distribution
- alignment payload construction

## Strategy Selection Inputs

Inputs that may be used:
- voicebank `character.yaml`
- voicebank language / dictionary layout
- explicit language argument when present

Primary selector for v1:
- `character.yaml default_phonemizer`

Expected mappings:
- `OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`
- optionally later:
  - `OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer`

## New Internal Contract

The dedicated phonemizer path should output the same downstream alignment contract shape already expected by the synthesis pipeline, including:

- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `word_pitches`
- `note_durations`
- `note_pitches`
- `note_rests`
- `phonemes` when requested

This keeps duration/pitch/variance stages unchanged at the API boundary.

## Key Behavioral Differences From Generic Path

The new path is expected to emulate these OpenUtau-style English behaviors:

1. Prefer the dedicated English dictionary:
- `dsdict-en.yaml`
- then fallback to `dsdict.yaml`

2. Apply DiffSinger-English-specific phoneme normalization and remapping
- not just raw dictionary output

3. Use vowel-centered note distribution
- vowel anchors the sung note
- consonants/glides are placed around it

4. Preserve bank-compatible phoneme identity conventions
- prefixed or bare symbols according to the bank's expected English path

## Rollout Strategy

Phase 1:
- detect declared English DiffSinger phonemizer
- use new phonemizer and note distribution path
- keep current duration/pitch/acoustic stages unchanged

Phase 2 if needed:
- add more OpenUtau-style phrase-level timing shaping
- add ARPA+ English variant support

## Expected Benefits

For Hoshino-style banks:
- better phoneme normalization consistency
- better note-to-vowel placement
- improved pitch stability
- closer parity with OpenUtau demo behavior

For existing compatible banks:
- no behavior change unless they explicitly match the new selector

## Risks

1. Some English banks may declare the OpenUtau phonemizer but still behave acceptably on the generic path.
- mitigation:
  - narrow initial selector
  - add tests before widening usage

2. OpenUtau parity is not only phonemizer logic.
- mitigation:
  - keep this phase focused on the most likely upstream mismatch first

3. We may need follow-up work if phrase-level timing assumptions still differ.
- mitigation:
  - keep the new phonemizer path isolated so later parity work can extend it

## Acceptance Criteria

1. A voicebank that declares `OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer` can be routed to the new path automatically.
2. Existing generic English banks still use the old path unless explicitly selected for the new one.
3. The new path returns the same alignment contract shape as the current pipeline expects.
4. Hoshino Amazing Grace soprano synthesis remains functional.
5. The codebase can compare generic vs dedicated phonemizer outputs in tests for the same score/bank.
