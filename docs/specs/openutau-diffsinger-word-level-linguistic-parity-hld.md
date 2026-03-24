# HLD: OpenUtau DiffSinger Word-Level Linguistic Parity

## Purpose

Define a high-level design for supporting newer DiffSinger voicebanks whose linguistic encoders require word-level inputs such as:

- `word_div`
- `word_dur`
- `languages`

The design follows the same direction as the English phonemizer parity work:

- port the smallest coherent OpenUtau behavior-owning unit
- do not reimplement the full OpenUtau renderer
- keep the existing pipeline where possible

Primary drivers:

- `Mairu_Maishi_v2_0_0 2`
- `UFR-V1.0/Hitsune_Kumi`
- `KITANE_DS_2.0.0`

These banks fail today because they belong to a newer DiffSinger model family that expects richer linguistic inputs than the legacy token-only path.

## Problem

The current backend supports multiple DiffSinger model families unevenly.

What already works:

- lower-level inference code in [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py) can build and pass:
  - `word_div`
  - `word_dur`
  - `languages`
- some voicebanks with newer model layouts already synthesize successfully

What still fails:

- several newer banks require OpenUtau-style word-level linguistic inputs in stages where the current synthesis path still behaves like an older family
- failures surface as missing ONNX inputs like:
  - `Required inputs (['word_div', 'word_dur']) are missing from input feed (['tokens'])`

This is not just a packaging issue.

It is a model-family compatibility issue:

- older banks are token-centric
- newer banks are phrase/word-aware and often language-aware

## Goal

Add a small, explicit compatibility layer that reproduces the OpenUtau behavior needed to prepare linguistic model inputs for newer DiffSinger voicebanks.

This layer must:

1. detect which linguistic input contract a model expects
2. build the required tensors from the backend’s existing alignment/phonemization outputs
3. do so using OpenUtau-compatible semantics for:
   - word segmentation
   - word duration derivation
   - language ID routing

## Non-Goals

This change should not:

1. port the full OpenUtau renderer
2. replace the existing generic phonemizer path
3. add user-facing controls for linguistic model selection
4. solve every packaging issue in third-party voicebanks
5. redesign the downstream duration, pitch, variance, or acoustic models

## Key Observation

The true incompatibility seam is not “phonemizer vs non-phonemizer.”

It is:

- how the backend builds linguistic encoder inputs for different DiffSinger model families

OpenUtau already owns this behavior in a small set of places:

- [DiffSingerBasePhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs)
- [DiffSingerVariance.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerVariance.cs)
- [DiffSingerPitch.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerPitch.cs)

Those files own the logic for:

- deriving `word_div`
- deriving `word_dur`
- loading and passing `languages`

That is the smallest coherent OpenUtau unit for this compatibility problem.

## Proposed Architecture

Add a new internal subsystem:

- `DiffSingerLinguisticInputBuilder`

This builder is not a phonemizer and not a model wrapper.

It is a bridge between:

- phonemization/alignment results from our backend
- the exact tensor contract expected by each linguistic ONNX model

Recommended internal structure:

1. `DiffSingerLinguisticContract`
- inspects ONNX input names
- classifies the model into one of a few supported input families

2. `OpenUtauParityWordInputs`
- builds `word_div` and `word_dur` using OpenUtau-style semantics
- uses phrase/word boundaries already present in backend alignment output

3. `OpenUtauParityLanguageInputs`
- resolves language IDs from the bank’s `languages.json`
- maps phoneme-level language identity into the tensor form expected by the model

4. `DiffSingerLinguisticRunner`
- central place that assembles inputs and runs the linguistic model
- used consistently by duration-side, pitch-side, and variance-side linguistic encoders

## Why This Boundary

This boundary is the smallest coherent parity unit because it owns the exact failing behavior:

- older family: token-only or token + `ph_dur`
- newer family: token + word-level inputs, sometimes plus language IDs

It avoids pulling in unrelated OpenUtau concerns such as:

- project/session state
- note editor UI
- expression curves
- renderer scheduling

## Supported Linguistic Input Families

The builder should support a small explicit set of model contracts.

### Family A: Legacy token-only

Inputs:

- `tokens`

Used by older banks or simple linguistic encoders.

### Family B: Token + language-aware

Inputs:

- `tokens`
- `languages`

### Family C: Token + word-level

Inputs:

- `tokens`
- `word_div`
- `word_dur`

### Family D: Token + word-level + language-aware

Inputs:

- `tokens`
- `languages`
- `word_div`
- `word_dur`

### Family E: Duration-conditioned pitch/variance linguistic

Inputs:

- `tokens`
- `ph_dur`
- optional `languages`

This family already exists in the codebase and should continue to work through the same unified builder/runner boundary.

## Strategy Selection

Do not select this path by hard-coded voicebank allowlist.

Select it by ONNX input signature.

Why:

- the real compatibility boundary is the model contract, not the bank name
- more new banks will likely share the same family
- it keeps the solution general without guessing per voicebank

Recommended order:

1. inspect linguistic model input names
2. classify into supported family
3. build the exact required tensors
4. fail with a clear unsupported-contract error if the input family is unknown

## OpenUtau Parity Scope

The parity target is not “bitwise identical to OpenUtau.”

It is:

- semantically equivalent preparation of linguistic inputs

For this change, parity should cover:

1. `word_div` semantics
- built from word/phrase grouping in the same spirit as OpenUtau’s phrase phoneme grouping

2. `word_dur` semantics
- derived from word-level timing spans rather than ad hoc guessed values

3. `languages` semantics
- resolved from the bank’s declared `languages.json`
- passed only when the model contract requires it

This is intentionally narrower than the phonemizer parity project.

## Phase 1 Recommendations

The open design choices for Phase 1 are closed as follows:

1. `word_div` source
- Use the backend's current alignment output as the official Phase 1 source.
- Do not recompute word groups from raw phrase phonemes in Phase 1.
- Revisit recomputation only if a specific bank proves the current grouped-word output is insufficient.

2. `word_dur` source
- Use the backend's current aligned word-group frame durations as the official Phase 1 source.
- Do not introduce a second timing derivation path yet.

3. `languages` source
- Use phoneme-level language IDs already produced by the backend phonemizer when available.
- If a model contract explicitly requires `languages`, that contract takes precedence over the root `use_lang_id` flag.
- If explicit phoneme-level language IDs are unavailable, derive a deterministic default language ID from the voicebank's `languages.json`.

4. Rest handling
- Standalone pause/rest-style tokens such as `SP` or `AP` count as their own word group in Phase 1.
- They must contribute a `word_div` entry of `1` and a non-zero `word_dur`.
- Do not merge them away or allow zero-sized word groups.

## Integration Points

The new builder/runner should be used in:

1. duration-side linguistic encoder path
2. pitch-side linguistic encoder path
3. variance-side linguistic encoder path

It should replace scattered ad hoc input assembly with one shared flow.

This matters because some banks mix contracts across stages:

- duration-side may require `word_div` and `word_dur`
- pitch-side may require `ph_dur`
- variance-side may differ again

One central runner makes these differences explicit and testable.

## Reuse From Existing Backend

The current backend already has most of the raw ingredients:

- phoneme IDs
- language IDs
- word boundaries
- word durations
- phoneme durations

The HLD does not propose recomputing all of that from scratch.

Instead:

- normalize those artifacts into a shared linguistic-input contract
- then apply OpenUtau-inspired shaping where needed

This keeps the change smaller than a full OpenUtau port.

## Benefits

1. Solves a broader class of incompatible newer voicebanks than one-off local fixes.
2. Aligns the codebase around model families instead of special-casing bank names.
3. Reuses OpenUtau semantics in the exact area where compatibility is failing.
4. Keeps the change smaller and safer than porting the full renderer stack.

## Risks

1. Word-level inputs may depend subtly on phrase segmentation assumptions from OpenUtau.
2. Some banks may still have packaging issues after the model-contract issue is solved.
3. Some banks may require both this work and the planned OpenUtau phonemizer parity work.
4. If the builder is too generic too early, it may hide unsupported edge cases instead of surfacing them clearly.

## Rollout Plan

### Phase 1

Implement model-signature-aware linguistic input building for the supported families.

Success criteria:

- `Mairu_Maishi_v2_0_0 2` advances past the current missing `word_div` / `word_dur` failure
- `KITANE_DS_2.0.0` advances past the same failure
- `UFR-V1.0/Hitsune_Kumi` can use the correct word-level and language-aware linguistic inputs once packaging issues are resolved

### Phase 2

Combine with targeted packaging fixes where necessary:

- dotted/extended speaker embedding suffix resolution
- vocoder alias/fallback cleanup

### Phase 3

Combine with dedicated OpenUtau phonemizer parity where a bank’s output quality still depends on closer phrase-phonemization behavior.

## Acceptance Criteria

1. The backend can classify linguistic ONNX models by input signature.
2. Duration, pitch, and variance-side linguistic encoders all use one shared input-building path.
3. Banks requiring `word_div` and `word_dur` no longer fail because of missing linguistic inputs.
4. Banks requiring `languages` no longer fail because of missing language tensors when the voicebank provides `languages.json`.
5. Existing compatible banks remain compatible.

## Follow-Up Questions

These are deferred follow-up questions, not blockers for Phase 1:

1. Should a later Phase 2 add phrase-level recomputation of `word_div` and `word_dur` for banks whose timing still diverges from OpenUtau after contract parity lands?
2. Should future model families support additional language tensor shapes beyond the current phoneme-level representation?
3. Should the debug logging proposed in this design be surfaced behind a dedicated env flag for easier bank-onboarding diagnostics?
