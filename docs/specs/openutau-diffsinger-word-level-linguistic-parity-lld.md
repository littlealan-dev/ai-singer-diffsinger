# LLD: OpenUtau DiffSinger Word-Level Linguistic Parity

## Purpose

Implement a minimal, model-signature-aware compatibility layer for newer DiffSinger voicebanks whose linguistic encoders require:

- `word_div`
- `word_dur`
- `languages`

This change targets the failing model family seen in:

- `Mairu_Maishi_v2_0_0 2`
- `KITANE_DS_2.0.0`
- `UFR-V1.0/Hitsune_Kumi`

The goal is to reproduce the OpenUtau-owned behavior that prepares these tensors, without porting the full OpenUtau renderer.

## Problem Statement

Today the codebase already has most of the necessary data:

- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `durations`

However, support is still fragmented across layers:

- some paths assemble word-level linguistic inputs
- some model families still end up running with token-only inputs
- failures surface only at ONNX runtime

Observed runtime failures:

- `Required inputs (['word_div', 'word_dur']) are missing from input feed (['tokens'])`

The missing behavior is not a full new renderer. It is a missing shared contract layer between alignment output and linguistic ONNX execution.

## Source of Truth

OpenUtau reference points:

- [DiffSingerBasePhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs)
- [DiffSingerVariance.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerVariance.cs)
- [DiffSingerPitch.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerPitch.cs)

These files define the behavior we need parity with:

- how `word_div` is derived
- how `word_dur` is derived
- when `languages` is required

## Design Overview

Add a new shared module:

- `src/api/diffsinger_linguistic_inputs.py`

This module will:

1. inspect a linguistic ONNX model’s required input names
2. classify its input contract
3. build the correct input tensor set from existing backend artifacts

It will be used by:

- duration-side linguistic inference
- pitch-side linguistic inference
- variance-side linguistic inference

## New Types

### `DiffSingerLinguisticContract`

Represents the required input family for a linguistic model.

Suggested enum-like values:

- `TOKENS_ONLY`
- `TOKENS_LANG`
- `TOKENS_WORD`
- `TOKENS_WORD_LANG`
- `TOKENS_PHDUR`
- `TOKENS_PHDUR_LANG`

Rule:
- contract classification is based on exact ONNX input names
- unknown input combinations should raise a clear error

### `DiffSingerLinguisticFeatures`

Small normalized feature object that carries everything the builder may need.

Suggested fields:

- `phoneme_ids: List[int]`
- `language_ids: List[int]`
- `word_boundaries: List[int]`
- `word_durations: List[int]`
- `phoneme_durations: Optional[List[int]]`
- `language_map: Optional[Dict[str, int]]`
- `default_language_id: Optional[int]`
- `active_language: Optional[str]`

This is the bridge object passed from the synthesis pipeline into the new runner.

## New Module API

### `classify_linguistic_contract(input_names: List[str]) -> DiffSingerLinguisticContract`

Input:
- actual ONNX input names from the linguistic model wrapper

Behavior:
- convert the input-name set into one supported contract enum
- order of names is ignored

Expected mappings:

- `{"tokens"}` -> `TOKENS_ONLY`
- `{"tokens", "languages"}` -> `TOKENS_LANG`
- `{"tokens", "word_div", "word_dur"}` -> `TOKENS_WORD`
- `{"tokens", "languages", "word_div", "word_dur"}` -> `TOKENS_WORD_LANG`
- `{"tokens", "ph_dur"}` -> `TOKENS_PHDUR`
- `{"tokens", "languages", "ph_dur"}` -> `TOKENS_PHDUR_LANG`

Unknown combinations:
- raise `ValueError("Unsupported linguistic input contract: ...")`

### `build_linguistic_inputs(contract, features, *, use_lang_id: bool) -> Dict[str, np.ndarray]`

Behavior:
- always builds `tokens`
- conditionally builds:
  - `languages`
  - `word_div`
  - `word_dur`
  - `ph_dur`

Tensor construction rules:

- `tokens`:
  - shape `[1, n_tokens]`
  - dtype `int64`

- `languages`:
  - shape `[1, n_tokens]`
  - dtype `int64`
  - if contract requires `languages`, the ONNX contract takes precedence over `use_lang_id`
  - preferred source order:
    1. `features.language_ids` when present and length-matched
    2. repeat `features.default_language_id` to token length
  - `default_language_id` should be resolved from `language_map` using:
    1. `other`, if present
    2. else `active_language`, if present and mapped
    3. else the smallest numeric ID in the map
  - only pass zeros if:
    - the contract requires `languages`
    - no explicit `language_ids` were provided
    - and no usable `language_map` exists

- `word_div`:
  - shape `[1, n_words]`
  - dtype `int64`
  - value is the existing backend `word_boundaries`
  - standalone rest/pause groups such as `SP` or `AP` count as their own word with boundary `1`

- `word_dur`:
  - shape `[1, n_words]`
  - dtype `int64`
  - value is the existing backend `word_durations`
  - standalone rest/pause groups must keep a non-zero duration

- `ph_dur`:
  - shape `[1, n_tokens]`
  - dtype `int64`
  - requires `features.phoneme_durations`

Validation:

- `sum(word_boundaries) == len(phoneme_ids)`
- `len(language_ids) == len(phoneme_ids)` when present
- `len(phoneme_durations) == len(phoneme_ids)` when required
- all `word_boundaries >= 1`
- all `word_durations >= 1`
- `len(word_boundaries) == len(word_durations)`

On validation failure:
- raise `ValueError` with a narrow error message

### `run_linguistic_model(model, features, *, use_lang_id: bool) -> List[np.ndarray]`

Behavior:

1. inspect `model.input_names`
2. classify the contract
3. build the required tensors
4. call `model.run(inputs)`

This becomes the single shared execution path for linguistic ONNX models.

### `resolve_default_language_id(language_map: Dict[str, int], active_language: Optional[str]) -> int`

Behavior:

1. if `language_map` contains `other`, return its ID
2. else if `active_language` is present and mapped, return that ID
3. else return the smallest numeric ID in the map

Recommendation:
- use this resolver instead of a blind zero fallback
- many voicebanks do not treat `0` as a safe or meaningful default language embedding

## Integration Changes

### 1. `src/api/inference.py`

Replace direct linguistic input assembly with the shared runner.

#### `predict_durations(...)`

Current behavior:
- manually builds:
  - `tokens`
  - `languages`
  - `word_div`
  - `word_dur`

Change:
- create `DiffSingerLinguisticFeatures`
- call `run_linguistic_model(...)`

Effect:
- older and newer duration-side models are both handled through one contract-aware path

#### `predict_pitch(...)`

Current behavior:
- pitch-side linguistic encoder path only knows:
  - `tokens`
  - `languages`
  - `ph_dur`

Change:
- route pitch-side linguistic encoder through the same contract-aware runner

Features passed:
- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `phoneme_durations=durations`

Effect:
- if a future pitch-side model needs word-level inputs, the path is already prepared
- current `ph_dur` family stays supported

#### `predict_variance(...)`

Same change as pitch-side:
- use shared contract-aware runner
- provide phoneme durations when available

### 2. `src/acoustic/model.py`

No behavioral redesign is required, but add a small helper for clarity:

- `get_input_name_set(self) -> set[str]`

Optional only, but useful for readability in contract classification.

No change to the `run(...)` filtering logic is required.

## OpenUtau Parity Semantics

This change intentionally keeps the parity scope narrow.

### `word_div`

Use the backend’s existing word-group phoneme counts as the initial parity representation.

Reason:
- these are already derived from grouped lyrics/phonemes
- they are the closest existing equivalent to OpenUtau’s phrase word segmentation

### `word_dur`

Use the backend’s aligned word-group frame durations.

Reason:
- these are already timing-aware
- they match the model’s expectation better than ad hoc recomputation

Important note:
- this is parity in interface semantics, not full phrase engine parity
- if a future bank proves that OpenUtau recomputation is required, that should be a Phase 2 refinement

### `languages`

Use phoneme-level language IDs already produced by the current phonemizer as the primary source.

Recommendations:

1. if the ONNX contract explicitly requires `languages`, do not suppress the tensor just because root `use_lang_id` is false
2. prefer explicit phoneme-level language IDs when available
3. otherwise resolve a deterministic default language ID from `languages.json`

Reason:
- this aligns with how current acoustic inference already treats `languages`
- it avoids assuming `0` is a safe default embedding
- it keeps the compatibility layer driven by model contract, not stale root-config assumptions

### Rest-like groups

Recommendation:
- treat `SP`, `AP`, and comparable standalone pause groups as their own word group in Phase 1
- they should produce:
  - `word_div = 1`
  - `word_dur >= 1`

Reason:
- this preserves the one-group-per-phoneme-run invariant
- it avoids zero-width word groups and brittle validation exceptions
- it stays closer to OpenUtau’s phrase-level handling than silently dropping pause groups

## Error Handling

### Unsupported contract

Raise:

```text
ValueError: Unsupported linguistic input contract: ['foo', 'tokens', ...]
```

### Missing `ph_dur`

Raise:

```text
ValueError: Linguistic contract requires phoneme durations, but none were provided.
```

### Invalid word-level features

Raise:

```text
ValueError: word_boundaries do not sum to phoneme count.
```

Or similarly narrow messages for each mismatch.

## Logging

Add debug-only logs in the new module:

- classified contract
- tensor names built
- tensor shapes

Example:

```text
linguistic_contract model=variance.linguistic.onnx contract=TOKENS_WORD
linguistic_inputs keys=['tokens','word_div','word_dur'] shapes={'tokens':[1,42], ...}
```

This is especially useful for onboarding new voicebanks.

## Tests

### Unit tests: new file

- `tests/test_diffsinger_linguistic_inputs.py`

Test cases:

1. classify `TOKENS_ONLY`
2. classify `TOKENS_WORD`
3. classify `TOKENS_WORD_LANG`
4. classify `TOKENS_PHDUR`
5. build `word_div` and `word_dur` tensors correctly
6. build default-language `languages` tensor when required and explicit `language_ids` are absent
7. fail on unsupported input signature
8. fail on invalid `word_boundaries` sum
9. fail when `ph_dur` is required but missing
10. treat standalone rest groups as `word_div=1` with non-zero `word_dur`

### Regression tests in existing files

#### `tests/test_api.py`

Add targeted tests for:

- a mocked `LinguisticModel` with input names:
  - `tokens`, `word_div`, `word_dur`
- a mocked `LinguisticModel` with:
  - `tokens`, `languages`, `word_div`, `word_dur`

Assert:
- the correct tensors are passed
- contract-required `languages` use resolved default IDs when explicit `language_ids` are absent

#### `tests/test_diffsinger_linguistic_inputs.py`

Add explicit cases for:

1. `language_map={"other": 1}` resolves default to `1`
2. `language_map={"en": 2, "ja": 7}` with active language `en` resolves to `2`
3. no `other`, no active-language hit -> resolves to the smallest ID
4. rest-only group produces `word_div=[1]` and non-zero `word_dur`

#### `tests/test_end_to_end.py`

If lightweight enough, add explicit smoke tests for one bank from the failing family once packaging blockers are resolved.

Recommended future candidates:

- `KITANE_DS_2.0.0`
- `Mairu_Maishi_v2_0_0 2`

For `UFR-V1.0/Hitsune_Kumi`, keep runtime validation separate until the `.kumi.emb` resolution issue is addressed.

## Implementation Order

1. Add `src/api/diffsinger_linguistic_inputs.py`
2. Add unit tests for contract classification and tensor building
3. Refactor `predict_durations(...)` to use the shared runner
4. Refactor `predict_pitch(...)` to use the shared runner
5. Refactor `predict_variance(...)` to use the shared runner
6. Add regression tests for new supported signatures
7. Re-run smoke tests on:
   - `KITANE_DS_2.0.0`
   - `Mairu_Maishi_v2_0_0 2`

## Out of Scope

This LLD does not include:

- dotted speaker-embedding suffix resolution fixes
- vocoder alias resolution fixes
- OpenUtau phonemizer parity
- multilingual dictionary-selection redesign

Those may still be required for some banks after this compatibility layer lands.

## Acceptance Criteria

1. Duration-side linguistic models requiring `word_div` and `word_dur` no longer fail due to missing inputs.
2. Language-aware word-level linguistic models can receive `languages` when required.
3. Existing compatible banks remain working.
4. The new shared runner becomes the only place that decides which linguistic input contract to use.
5. Contract-required `languages` no longer depend on blindly hardcoded zero tensors when a usable `languages.json` is present.
