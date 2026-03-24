# LLD: OpenUtau DiffSinger Energy Variance Parity

## Purpose

Add minimal OpenUtau-parity support for DiffSinger voicebanks whose acoustic model requires `energy`.

The implementation goal is:

1. extend the current variance path so it returns predicted `energy`
2. pass that predicted `energy` into the acoustic model when the voicebank requires it
3. preserve current behavior for voicebanks that do not use `energy`

This is a narrow compatibility change, not a broader redesign of expression control.

## Problem Statement

Some newer DiffSinger voicebanks, such as:

- `KITANE_DS_2.0.0`

declare:

- root acoustic config: `use_energy_embed: true`
- variance config: `predict_energy: true`

Today our pipeline already sends zero-filled `energy` into the variance ONNX model when `predict_energy: true`, but it drops the predicted energy output before the acoustic stage.

As a result, the acoustic model later fails with:

- `Required inputs (['energy']) are missing from input feed ...`

## OpenUtau Source of Truth

Reference files:

- [DiffSingerVariance.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerVariance.cs)
- [DiffSingerRenderer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerRenderer.cs)

Relevant OpenUtau behavior:

1. Variance model input:
   - when `predict_energy` is enabled, OpenUtau seeds variance inference with a zero-filled `energy` curve

2. Variance model output:
   - OpenUtau reads `energy_pred` from the variance model outputs

3. Acoustic model input:
   - when `useEnergyEmbed` is enabled, OpenUtau passes:
     - `predictedEnergy + userEnergyDelta`
   - if the user did not draw an energy curve, the user delta defaults to `0`

Phase 1 parity target for this repo:

- support `predictedEnergy`
- do not add user-editable energy deltas yet

So our Phase 1 acoustic behavior becomes:

- `energy = predictedEnergy`

This is already much closer to OpenUtau than the current missing-input failure.

## Current Codepath

### Variance stage

In [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py):

- `predict_variance(...)` already reads:
  - `predict_energy`
  - `predict_breathiness`
  - `predict_voicing`
  - `predict_tension`
- it already adds zero-filled `energy` to `variance_inputs` when `predict_energy` is enabled

But it currently returns only:

- `breathiness`
- `tension`
- `voicing`

### Acoustic stage

In [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py):

- `synthesize_mel(...)` always builds acoustic inputs for:
  - `breathiness`
  - `voicing`
  - `tension`
  - `gender`
  - `velocity`
- it does not add `energy`

So the missing behavior is a simple dropped field between these two stages.

## Design Overview

Implement a narrow end-to-end energy path:

1. variance prediction returns `energy` when available
2. synth pipeline carries `energy` alongside other variance curves
3. acoustic inference passes `energy` only when the voicebank requires it

No other voicebank behavior changes.

## Source of Truth Rules

The root acoustic config is the source of truth for whether acoustic inference requires `energy`.

Rules:

- if root config has `use_energy_embed: false`
  - acoustic inference must not pass `energy`
- if root config has `use_energy_embed: true`
  - acoustic inference must require a valid `energy` curve
- if root config requires `energy`, but the variance config reports `predict_energy: false`
  - this is a fatal voicebank/config mismatch
  - raise `ValueError("The acoustic model requires energy (use_energy_embed=True), but the variance config does not enable predict_energy.")`

Config access must use `.get(..., False)` for compatibility with older voicebanks:

- `config.get("use_energy_embed", False)`
- `variance_conf.get("predict_energy", False)`

## Detailed Design

### 1. Extend `predict_variance(...)` to return `energy`

File:

- [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py)

Current return shape:

```python
{
    "breathiness": ...,
    "tension": ...,
    "voicing": ...,
}
```

New return shape:

```python
{
    "energy": ...,
    "breathiness": ...,
    "tension": ...,
    "voicing": ...,
}
```

Rules:

- when `predict_energy` is false:
  - return `energy` as `[0.0] * n_frames`
- when the model is absent or no encoder output exists:
  - return zeros for all supported variance fields, including `energy`
- when the variance model returns an energy head:
  - extract it and convert to `List[float]`

Output parsing rules:

- do not rely on output index ordering for `energy`
- inspect the ONNX session outputs and map tensor indices by output name
- expected names:
  - `energy_pred`
  - `breathiness_pred`
  - `voicing_pred`
  - `tension_pred`
- if `predict_energy` is enabled but `energy_pred` is missing:
  - raise `ValueError("Variance config requires energy prediction, but the variance model did not return 'energy_pred'.")`
- if any other enabled head is missing:
  - raise `ValueError` naming the missing head
- if `predict_energy` is disabled:
  - keep current non-energy behavior

### 2. Extend `synthesize_mel(...)` to accept optional `energy`

File:

- [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py)

Signature change:

```python
def synthesize_mel(
    ...,
    energy: Optional[List[float]] = None,
    breathiness: Optional[List[float]] = None,
    tension: Optional[List[float]] = None,
    voicing: Optional[List[float]] = None,
    ...
)
```

Rules:

- only include the acoustic input named `energy` when the root voicebank config has:
  - `use_energy_embed: true`
- if `use_energy_embed` is false:
  - do not pass the `energy` tensor at all
- if `use_energy_embed` is true and `energy is None`:
  - raise `ValueError("The acoustic model requires 'energy' (use_energy_embed=True), but the variance stage did not provide it.")`
- if `use_energy_embed` is true and the frame-aligned `energy` curve length does not equal `n_frames`:
  - raise `ValueError` rather than silently repairing it inside `synthesize_mel(...)`

This mirrors the existing conditional behavior already used for language IDs and avoids breaking old voicebanks with simpler acoustic contracts.

### 3. Extend `synthesize_audio(...)` to accept optional `energy`

File:

- [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py)

Signature change:

```python
def synthesize_audio(
    ...,
    energy: Optional[List[float]] = None,
    breathiness: Optional[List[float]] = None,
    tension: Optional[List[float]] = None,
    voicing: Optional[List[float]] = None,
    ...
)
```

Behavior:

- simply forward `energy` into `synthesize_mel(...)`

### 4. Carry `energy` through `synthesize(...)`

File:

- [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)

After:

- `var_result = predict_variance(...)`

add:

```python
energy = var_result["energy"]
```

No new user-facing control is introduced in this change.

Phase 1 rule:

- `energy` is passed through exactly as predicted by the variance model
- there is no `energy` scaling knob yet

Then pass `energy` into:

- `synthesize_audio(...)`

### 5. Preserve frame-alignment guarantees

File:

- [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)

Current logic already aligns:

- `f0`
- `breathiness`
- `tension`
- `voicing`

Phase 1 extension:

- include `energy` in the same frame-alignment step

Rules:

- if expected frame count differs from `len(energy)`, pad/trim `energy` exactly the same way as other variance curves
- use trailing-value padding, matching current `_pad_curve_to_length(...)`

### 6. Logging

Files:

- [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)
- [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py)

Add `energy` to debug summaries where other variance fields are already logged:

- variance output summary
- frame length mismatch summary
- acoustic input summary

This helps diagnose future voicebanks that require `energy`.

## Backward Compatibility Rules

This change must not break existing voicebanks.

### Rule 1: Voicebanks without `predict_energy`

If the variance config does not enable `predict_energy`:

- `predict_variance(...)` returns zero `energy`
- acoustic stage should still omit `energy` unless `use_energy_embed: true`

Result:

- current compatible banks remain unchanged

### Rule 2: Voicebanks without `use_energy_embed`

If the root acoustic config does not enable `use_energy_embed`:

- do not add `energy` to acoustic inputs

Result:

- older acoustic models keep receiving the exact same input signature as today

### Rule 3: Voicebanks with `use_energy_embed: true` and `predict_energy: true`

For banks like `KITANE_DS_2.0.0`:

- predicted `energy` is passed through to acoustic inference

Result:

- missing-input failure should be resolved

### Rule 4: Voicebanks with `use_energy_embed: true` but no usable predicted energy

If a bank requires acoustic `energy`, but variance output is missing or malformed:

- hard-fail with `ValueError`

Recommended error:

- `ValueError("The acoustic model requires 'energy' (use_energy_embed=True), but the variance stage did not provide it.")`

Rationale:

- zero-filling a required acoustic energy contour is not a neutral fallback
- it is likely to produce severely degraded or broken audio
- the failure should stay explicit and debuggable

## Non-Goals

This change does not:

- add user-editable `energy` curves
- add an `energy` API knob in `synthesize(...)`
- implement full OpenUtau-style `predicted + user delta` composition
- redesign variance control ranges

Those can be layered later without invalidating this implementation.

## Test Plan

### Unit tests

Add tests for [inference.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py):

1. `predict_variance` returns zero `energy` when no variance model exists
2. `predict_variance` returns parsed `energy` when `predict_energy: true`
3. `predict_variance` raises when config enables `predict_energy` but the variance model does not expose `energy_pred`
4. `synthesize_mel` omits `energy` when `use_energy_embed: false`
5. `synthesize_mel` includes `energy` when `use_energy_embed: true`
6. `synthesize_mel` raises when `use_energy_embed: true` and `energy=None`
7. pipeline raises when root acoustic config requires `energy` but variance config disables `predict_energy`

### Regression tests

Re-run existing compatible cases:

- `Kohaku Merry DiffSinger V2.0`
- `Mairu_Maishi_v2_0_0 2`

Expected:

- no behavior change

### Compatibility smoke test

Re-run:

- `KITANE_DS_2.0.0`

Expected:

- old failure:
  - missing `energy`
- should be replaced by:
  - successful render, or a later-stage separate blocker

## Acceptance Criteria

The implementation is complete when:

1. `predict_variance(...)` returns `energy` alongside existing variance outputs
2. `synthesize_audio(...)` and `synthesize_mel(...)` can carry `energy`
3. acoustic input includes `energy` only when `use_energy_embed: true`
4. required `energy` mismatches fail loudly with `ValueError`
5. existing compatible voicebanks still render unchanged
6. `KITANE_DS_2.0.0` no longer fails on missing `energy`

## Recommended Implementation Order

1. extend `predict_variance(...)` return shape
2. extend `synthesize_mel(...)` / `synthesize_audio(...)` signatures
3. wire `energy` through `synthesize(...)`
4. add frame alignment for `energy`
5. add unit tests
6. run compatibility smoke tests
