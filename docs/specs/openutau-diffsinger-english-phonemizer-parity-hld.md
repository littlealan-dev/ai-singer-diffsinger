# HLD: OpenUtau DiffSinger English Phonemizer Parity Unit

## Purpose

Define the smallest coherent OpenUtau-derived unit to port for English DiffSinger phonemization parity.

This is intentionally broader than a single class port.

Goal:
- reproduce the full English DiffSinger phonemization behavior that OpenUtau voicebanks expect
- avoid a half-port where one class is copied but its supporting behavior is silently reimplemented differently

Primary driver:
- `Hoshino Hanami v1.0` declares:
  - `OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`

## Problem

Porting only `DiffSingerEnglishPhonemizer` is not enough.

That class depends on a small but behavior-critical stack:
- English G2P selection
- dictionary loading order
- replacement/remapping behavior
- phoneme symbol validation
- vowel/glide/consonant classification
- phoneme-to-note distribution

If only the top class is ported, the result will still drift from OpenUtau and likely preserve the same quality problems.

## Design Principle

Port the smallest coherent behavior-owning unit, not the smallest number of files.

For English DiffSinger, that coherent unit is:

1. `DiffSingerEnglishPhonemizer`
2. `DiffSingerG2pPhonemizer`
3. the relevant behavior slice of `DiffSingerBasePhonemizer`

This unit owns:
- dictionary choice
- phoneme normalization/remapping
- phonetic hint resolution
- word-to-note phoneme distribution semantics

This unit does **not** own:
- duration ONNX inference
- pitch ONNX inference
- acoustic ONNX inference
- UI editing curves
- OpenUtau project/session state

## Proposed Architecture

Add a new internal parity module:

- `OpenUtauDiffSingerEnglishPhonemizer`

It will be backed by a small internal support layer that mirrors the minimum OpenUtau semantics needed for English DiffSinger behavior.

Recommended internal structure:

- `OpenUtauDiffSingerBase`
- `OpenUtauDiffSingerG2p`
- `OpenUtauDiffSingerEnglish`

These are local Python implementations, not a runtime dependency on OpenUtau itself.

## Why This Is The Right Boundary

This boundary is large enough to preserve behavior, but small enough to avoid pulling in the renderer/UI side of OpenUtau.

It includes:
- phonemizer logic
- remapper logic
- note assignment logic

It excludes:
- phrase renderer
- expression curves
- pitch renderer internals
- GUI preferences/state

So it is the smallest unit that still has a chance of real parity.

## Strategy Selection

Use the parity unit only when all are true:

1. synthesis language is English
2. voicebank declares:
   - `OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`

Otherwise:
- continue using the current backend phonemizer path

This keeps rollout narrow and low-risk.

## Behavioral Responsibilities

The parity unit must own these responsibilities end to end:

1. Dictionary selection
- prefer `dsdict-en.yaml`
- fallback to `dsdict.yaml`

2. English G2P fallback
- use an ARPABET-compatible English G2P behavior

3. Dictionary replacements/remapping
- support OpenUtau-style replacement semantics

4. Symbol validation
- validate against the actual bank phoneme inventory

5. Vowel/glide-aware note distribution
- assign phonemes to notes using the same singing-oriented rules OpenUtau applies

## Dictionary Override Policy

Phase 1 parity will support only voicebank-level dictionaries.

Supported lookup:
- `dsdict-en.yaml`
- fallback `dsdict.yaml`

Not supported in Phase 1:
- project-level or user-level override dictionaries
- ad hoc session dictionaries

Rationale:
- the current backend is chat-driven and voicebank-centric
- there is no OpenUtau-style project dictionary layer in the product architecture today
- adding user dictionary override semantics now would expand scope beyond the smallest coherent parity unit

If user dictionary support is needed later, it should be added as a separate layer above the parity unit, not embedded into the initial port boundary.

## Integration Contract

The parity unit should still output the same downstream contract our pipeline already expects:

- `phonemes`
- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `word_pitches`
- `note_durations`
- `note_pitches`
- `note_rests`

This allows the existing:
- duration predictor
- pitch predictor
- variance predictor
- acoustic stage

to remain unchanged initially.

## Benefits

1. Higher probability of real bank compatibility than a minimal approximation.
2. Lower chance of repeated rewrites chasing one edge case at a time.
3. Clearer ownership boundary:
   - OpenUtau English DiffSinger behavior lives in one dedicated stack
   - generic backend phonemization remains separate

## Risks

1. More code than a minimal patch.
2. More parity maintenance if OpenUtau behavior evolves.
3. Still not guaranteed to solve every bank issue if later render stages also differ.

## Non-Goals

1. Do not port full OpenUtau rendering.
2. Do not add support for every OpenUtau phonemizer in this change.
3. Do not change the generic phonemizer path.
4. Do not add new user-facing phonemizer controls.

## Rollout

Phase 1:
- English-only parity unit
- voicebank-declared selection only
- diagnostics comparing parity vs generic path

Phase 2 if needed:
- add `DiffSingerARPAPlusEnglishPhonemizer`
- extend to other explicitly requested phonemizers

The proposed parity module boundary is intentionally compatible with that extension:
- `OpenUtauDiffSingerBase` remains language-agnostic
- `OpenUtauDiffSingerG2p` remains remapper-oriented
- the English-specific class can gain an ARPA+ sibling without changing the generic pipeline integration contract

## Acceptance Criteria

1. The system can route declared OpenUtau English DiffSinger voicebanks to the parity unit.
2. The parity unit owns the full English phonemization behavior slice, not just one top-level class.
3. Existing banks on the generic path remain unchanged.
4. The downstream synthesis contract remains stable.
