# HLD: OpenUtau DiffSinger Stage-Specific Phoneme Remapping

## Purpose

Define a high-level design for supporting DiffSinger voicebanks whose duration, pitch, and acoustic stages do not share the same phoneme ID space.

This design follows the same parity direction as the earlier compatibility work:

- keep one shared singing/phonemization pipeline where possible
- add a small compatibility layer at the real failing seam
- avoid porting unrelated OpenUtau renderer behavior

Primary drivers:

- `UFR-V1.0/Hitsune_Kumi`
- `PM-31_Commercial_Indigo`
- `PM-31_Commercial_Scarlet`

These banks fail today because at least one model stage expects phoneme symbols to be encoded with a stage-local ID table rather than a single global voicebank table.

## Problem

The current backend effectively assumes:

1. phonemize once
2. convert phoneme symbols to token IDs once
3. reuse that one token-ID sequence across:
   - duration-side linguistic models
   - pitch-side linguistic models
   - acoustic/root-side models

That assumption is valid only for voicebanks where all stages share the same phoneme inventory mapping.

Some newer banks do not.

Example shape:

- root `dsmain/phonemes.json`: many distinct phoneme IDs
- duration `dsdur/.../phonemes.json`: same or similar full inventory
- pitch `dspitch/.../phonemes.json`: same symbol keys, but many symbols collapsed into a smaller ID set

Failure mode:

- backend feeds a root-stage token like `80`
- pitch model embedding table only supports IDs up to `45`
- ONNX runtime fails with out-of-range gather errors

This is not a phoneme-symbol problem.

The symbol stream is often still valid.

It is a stage-specific symbol-to-ID encoding problem.

## Goal

Add a small compatibility layer that:

1. keeps one shared aligned phoneme-symbol sequence
2. builds token IDs separately for each model stage using that stage’s configured phoneme inventory
3. routes the correct stage-specific token sequence into each linguistic/acoustic stage

The system should support voicebanks where:

- all stages share one inventory
- only one stage uses a compressed/remapped inventory
- duration and pitch use different inventories from the root acoustic stage

## Non-Goals

This change should not:

1. re-phonemize independently for each stage
2. replace the current syllable aligner or timing pipeline
3. port the full OpenUtau phonemizer stack
4. infer missing phoneme mappings heuristically across unrelated symbols
5. change user-facing synthesis controls

## Key Observation

The failing seam is not “dictionary selection” and not “phoneme generation.”

The failing seam is:

- conversion from an aligned phoneme-symbol stream into stage-local token IDs

OpenUtau-compatible banks can package multiple phoneme inventories because different submodels may have been trained with:

- full symbol granularity in one stage
- reduced symbol classes in another stage

So the smallest coherent fix is:

- preserve one canonical symbol stream
- add stage-specific token encoders

## Proposed Architecture

Add a new internal subsystem:

- `DiffSingerStageTokenEncoder`

This subsystem sits between:

- shared phoneme/alignment output
- stage-specific ONNX inference calls

Recommended internal structure:

### 1. `DiffSingerStagePhonemeInventory`

Responsibilities:

- load the phoneme inventory path declared by a specific stage config
- parse it into:
  - `symbol -> id`
  - inventory metadata for diagnostics

Supported sources:

- root `dsconfig.yaml` `phonemes`
- duration `dsdur/dsconfig.yaml` `phonemes`
- pitch `dspitch/dsconfig.yaml` `phonemes`
- later variance-stage configs if they start declaring their own inventories

### 2. `DiffSingerStageTokenEncoder`

Responsibilities:

- accept a canonical symbol sequence such as:
  - `["SP", "ah", "m", "ey", "z"]`
- encode that same sequence separately for:
  - root/acoustic
  - duration
  - pitch
- return stage-specific token lists

### 3. `DiffSingerStageTokenBundle`

Responsibilities:

- hold all token sequences for one phrase/note group, for example:
  - `root_tokens`
  - `dur_tokens`
  - `pitch_tokens`
- expose a stable interface for downstream inference code

### 4. `DiffSingerInferenceRouting`

Responsibilities:

- ensure each stage consumes the correct token list
- remove the current assumption that one `tokens` array is valid everywhere

## Why This Boundary

This is the smallest coherent parity unit because it owns the exact failing behavior:

- same symbol stream
- different stage-local ID spaces

It avoids dragging in unrelated complexity such as:

- OpenUtau project state
- editor-side note grouping UI
- expression curve authoring
- full phonemizer parity

## Stage Selection Rules

Do not enable this path by voicebank allowlist.

Use stage config and stage-local inventory presence.

Recommended rules:

1. if a stage declares its own phoneme inventory path:
   - use it for that stage
2. otherwise:
   - fall back to the root voicebank phoneme inventory
3. if the stage-local inventory produces the same symbol-to-ID mapping as root:
   - behavior remains effectively unchanged

This keeps the solution general and low-risk.

## Shared Symbol Stream Principle

Phase 1 should keep one shared phoneme-symbol sequence.

That means:

- phonemization happens once
- syllable alignment happens once
- note/word timing happens once

Only the symbol-to-ID encoding changes per stage.

Why:

- it fixes the concrete PM-31/UFR failure class
- it avoids redoing alignment logic separately for pitch vs duration
- it minimizes regression risk for already-compatible banks

## Failure Semantics

The compatibility layer must fail loudly when a stage cannot encode the canonical symbol stream.

Examples:

- a phoneme symbol exists in root inventory but not in pitch inventory
- a configured stage phoneme inventory file is missing
- a stage inventory file is malformed

Do not silently fall back to root IDs if a stage explicitly declares its own phoneme table.

That would hide real compatibility bugs and produce invalid inference behavior.

## Backward Compatibility

This design is additive.

For existing compatible banks:

- if they only use one effective phoneme ID space
- or if stage configs do not declare different inventories

then the resulting encoded token sequences remain identical to current behavior.

So the compatibility layer should be neutral for:

- Apollo
- Katyusha
- Kohaku Merry
- other already-working banks with shared ID spaces

## Expected Impact

This design should unblock the current known class of failures where:

- duration succeeds
- pitch fails with out-of-range token IDs

Expected beneficiaries:

- `PM-31_Commercial_Indigo`
- `PM-31_Commercial_Scarlet`
- likely `UFR-V1.0/Hitsune_Kumi`

It also creates a clean foundation for future banks that use:

- reduced pitch phoneme classes
- stage-local phoneme compression
- different symbol grouping per submodel

## Risks

### 1. Same symbols, different semantics

A stage-local inventory may reuse the same symbol names but intend different class groupings than root.

Mitigation:

- trust the stage-local `phonemes.json` as the source of truth
- do not mix ID spaces across stages

### 2. Hidden stage-local dictionary differences

Some banks may eventually require different symbol sequences per stage, not just different IDs.

Mitigation:

- Phase 1 explicitly limits scope to stage-local ID remapping
- if a bank still sounds wrong after remapping, that becomes a separate parity problem

### 3. Inference plumbing regressions

Refactoring token routing can accidentally break banks that currently work.

Mitigation:

- keep the canonical symbol sequence unchanged
- add regression coverage for known compatible banks
- default to root inventory when no stage-local override exists

## Rollout Strategy

Recommended order:

1. implement stage-local inventory loading
2. encode stage-specific token bundles
3. route duration and pitch to their own token lists
4. keep acoustic/root routing unchanged except to read the root bundle explicitly
5. test on:
   - a known shared-ID bank
   - `PM-31_Commercial_Indigo`
   - `PM-31_Commercial_Scarlet`
   - `UFR-V1.0/Hitsune_Kumi`

## Acceptance Criteria

The change is successful when:

1. voicebanks with shared ID spaces still synthesize unchanged
2. PM-31 Indigo no longer fails with out-of-range pitch token IDs
3. PM-31 Scarlet no longer fails with out-of-range pitch token IDs
4. stage-local phoneme inventory mismatches fail with explicit diagnostics
5. no stage consumes root token IDs when it declares a different phoneme inventory

## Recommendation

Implement this before any broader OpenUtau phonemizer parity work for the PM-31 family.

Reason:

- the current failure is clearly an ID-space mismatch
- the symbol stream is already good enough to reach the pitch model
- stage-specific remapping is the smallest high-confidence fix
