# LLD: OpenUtau DiffSinger Stage-Specific Phoneme Remapping

## Purpose

Implement a minimal compatibility layer for DiffSinger voicebanks whose submodels use different phoneme ID tables across:

- root/acoustic
- duration
- pitch

The implementation goal is:

1. keep one canonical phoneme-symbol sequence from the current phonemizer/alignment path
2. encode that sequence separately for each model stage using that stage’s configured `phonemes.json`
3. route the correct stage-local token IDs into duration, pitch, and acoustic inference

This is a targeted fix for the current failure class seen in:

- `PM-31_Commercial_Indigo`
- `PM-31_Commercial_Scarlet`
- likely `UFR-V1.0/Hitsune_Kumi`

## Problem Statement

Today the pipeline produces one token ID sequence and reuses it everywhere.

In practice:

- `phonemize(...)` and alignment produce `phoneme_ids`
- `predict_durations(...)` consumes those IDs
- `predict_pitch(...)` consumes the same IDs
- `predict_variance(...)` and acoustic-side paths also assume the same ID space

That works only when all stages share one inventory.

Some newer banks do not.

Example:

- root `phonemes.json`: 109 symbols, IDs up to `108`
- pitch `phonemes.json`: 109 symbols, but only 45 unique IDs, max `45`

So the symbol stream can still be valid, but the stage-local numeric encoding must change.

Observed failure:

- pitch linguistic model receives token ID `80`
- pitch embedding table only supports IDs up to `45`
- ONNX fails with out-of-range gather

## Source of Truth

Per-stage `dsconfig.yaml` is the source of truth for which phoneme inventory a stage should use.

Primary locations:

- root/acoustic:
  - [dsconfig.yaml](/Users/alanchan/antigravity/ai-singer-diffsinger/assets/voicebanks/PM-31_Commercial_Indigo/dsconfig.yaml)
- duration:
  - [dsdur/dsconfig.yaml](/Users/alanchan/antigravity/ai-singer-diffsinger/assets/voicebanks/PM-31_Commercial_Indigo/dsdur/dsconfig.yaml)
- pitch:
  - [dspitch/dsconfig.yaml](/Users/alanchan/antigravity/ai-singer-diffsinger/assets/voicebanks/PM-31_Commercial_Indigo/dspitch/dsconfig.yaml)

Phase 1 stage discovery rules:

1. if a stage config explicitly declares `phonemes`, resolve that file relative to the stage config directory
2. otherwise, if a `phonemes.json` file exists directly next to the stage config or ONNX payload in that stage folder, use it
3. otherwise, fall back to the root voicebank phoneme inventory

This fallback rule addresses the review comment about banks that colocate `phonemes.json` beside the stage assets without a separate override yaml.

## Design Overview

Add a new shared module:

- `src/api/diffsinger_stage_tokens.py`

This module owns:

1. stage-local phoneme inventory loading
2. stage-local symbol-to-ID encoding
3. cached token/inventory lookup for duration, pitch, and root/acoustic

The existing phonemizer and aligner continue to own:

- phoneme symbol generation
- language IDs
- word boundaries
- phoneme durations

## New Types

### `StageName`

Enum-like string values:

- `root`
- `dur`
- `pitch`

Phase 1 does not add a separate `variance` stage because current failing banks do not require it.

### `StagePhonemeInventory`

Represents one stage-local phoneme inventory.

Suggested fields:

- `stage: str`
- `path: Path`
- `symbol_to_id: Dict[str, int]`
- `unique_id_count: int`
- `max_id: int`

Notes:

- inventory values are normalized to `int`
- duplicate IDs are allowed
- duplicate symbol keys are not

### `StageTokenBundle`

Represents the stage-specific tokenized form of one canonical symbol stream.

Suggested fields:

- `symbols: List[str]`
- `root_ids: List[int]`
- `dur_ids: List[int]`
- `pitch_ids: List[int]`

Important type rule:

- this bundle stores raw Python `list[int]`
- padding/batching into `np.ndarray` remains the responsibility of the existing inference entrypoints

This addresses the review comment about structural types and avoids changing every downstream tensor call site at once.

## Cache Integration

Stage inventories must be cached alongside other voicebank-local artifacts.

Recommended approach:

### Option chosen for Phase 1

Add permanent process-level caching inside:

- [voicebank_cache.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank_cache.py)

Suggested API:

- `get_stage_phoneme_inventory(voicebank_path: Path, stage: str) -> StagePhonemeInventory`

Implementation strategy:

- use `@lru_cache` keyed by:
  - resolved voicebank path
  - stage name
  - resolved inventory path

Why:

- parsing `phonemes.json` is disk/JSON overhead that should not repeat every synth request
- this keeps inventory lifecycle aligned with other voicebank-level caching without requiring a broader cache redesign

## Module API

### `resolve_stage_phoneme_inventory_path(voicebank_path: Path, stage: str) -> Path`

Responsibilities:

1. resolve stage config:
   - root: `voicebank_path / "dsconfig.yaml"`
   - dur: `voicebank_path / "dsdur" / "dsconfig.yaml"`
   - pitch: `voicebank_path / "dspitch" / "dsconfig.yaml"`
2. load stage config when it exists
3. resolve `phonemes` relative to that config directory
4. if no explicit `phonemes` key exists:
   - look for `phonemes.json` in the stage directory
   - then near stage-local `dsmain`
5. if still unresolved:
   - fall back to root `phonemes`

Failure behavior:

- if the resolved path does not exist, raise `FileNotFoundError`

### `load_stage_phoneme_inventory(voicebank_path: Path, stage: str) -> StagePhonemeInventory`

Responsibilities:

1. resolve the stage inventory path
2. parse JSON into `symbol_to_id`
3. compute metadata such as `unique_id_count` and `max_id`

Validation:

- file must parse as a dictionary
- all values must be integer-convertible
- all symbols must be non-empty strings

### `encode_stage_symbols(symbols: Sequence[str], inventory: StagePhonemeInventory) -> List[int]`

Responsibilities:

- convert the canonical symbol sequence into stage-local IDs

Primary rule:

- use direct `symbol -> id` lookup only

Failure behavior:

- if a symbol is missing, raise `ValueError`

### Structural symbol rule: `SP` / `AP`

Review note considered:

- structural tokens may legitimately be compressed or treated specially by some stages

Phase 1 rule:

1. if `SP` or `AP` exists in the stage inventory, use the declared mapping
2. if missing, allow a safe framework-level fallback only for:
   - `SP`
   - `AP`
3. fallback resolution order:
   - reuse `SP` mapping for missing `AP` if `SP` exists
   - reuse `AP` mapping for missing `SP` if `AP` exists
   - otherwise use root-stage mapping for that same structural symbol only
4. for all non-structural symbols:
   - fail loudly

Reason:

- this avoids breaking pitch stages that intentionally compress silence/breath handling
- while still preserving strictness for actual singing phonemes

This fallback is intentionally narrow and does not apply to lyric-bearing phonemes.

### `build_stage_token_bundle(voicebank_path: Path, symbols: Sequence[str]) -> StageTokenBundle`

Responsibilities:

1. load cached inventories for:
   - root
   - dur
   - pitch
2. encode the same symbol list separately for each stage
3. return raw stage-local ID sequences

## Integration Changes

### 1. `src/api/synthesize.py`

Current behavior:

- alignment result carries:
  - `phoneme_ids`
  - `language_ids`
  - `word_boundaries`
- later stages reuse the same `phoneme_ids`

Change:

After alignment produces the canonical phoneme symbol stream, build a stage token bundle once.

Phase 1 requirement:

- preserve or expose the canonical phoneme symbol list in the alignment payload

If the current alignment payload does not yet retain symbols after ID conversion, add:

- `phoneme_symbols: List[str]`

to the alignment result.

Resulting flow:

1. phonemize/alignment produces:
   - `phoneme_symbols`
   - `language_ids`
   - `word_boundaries`
   - durations / note data
2. `build_stage_token_bundle(...)`
3. downstream stages consume stage-local IDs

### 2. `src/api/inference.py`

#### `predict_durations(...)`

Current signature already accepts:

- `phoneme_ids`

Phase 1 integration:

- keep the function signature unchanged if possible
- pass `stage_tokens.dur_ids` from `synthesize.py`

No other logic changes required here beyond using the correct ID sequence.

#### `predict_pitch(...)`

Current issue:

- pitch linguistic model is receiving root-style token IDs

Phase 1 integration:

- pass `stage_tokens.pitch_ids`

This is the expected fix for the PM-31 failure.

#### `predict_variance(...)`

Phase 1 recommendation:

- keep using root IDs unless a failing bank later proves variance-stage remapping is required

Why:

- current known failures are in pitch
- this keeps the scope smaller

If variance later requires its own stage inventory, extend the same pattern rather than inventing a new one.

#### Acoustic/root inference

Phase 1:

- continue using root-stage IDs
- make the routing explicit instead of implicit

This avoids hidden dependence on “whichever IDs happened to be generated first.”

## Canonical Symbol Preservation

This design depends on retaining a canonical phoneme symbol sequence after phonemization/alignment.

Phase 1 rule:

- canonical symbol order is authoritative
- all stage token sequences must have the same length as:
  - `language_ids`
  - `phoneme_durations`
  - aligned note/word structures

Validation:

- `len(root_ids) == len(dur_ids) == len(pitch_ids) == len(symbols)`

## Error Handling

Raise explicit errors for:

- missing stage inventory file
- malformed stage inventory JSON
- non-integer inventory value
- unsupported symbol in a stage inventory
- inconsistent encoded lengths across stages

Example messages:

- `Pitch phoneme inventory cannot encode symbol 'vcl'.`
- `Stage phoneme inventory missing: .../dspitch/dsmain/phonemes.json`
- `Stage token bundle length mismatch for voicebank ...`

Do not silently fall back from a declared stage inventory to root IDs for normal symbols.

## Backward Compatibility

This design is additive.

Compatibility rules:

1. if a stage resolves to the same phoneme inventory as root, encoded IDs remain unchanged
2. if a bank has no stage-local override, root behavior remains unchanged
3. existing compatible banks must keep synthesizing with no behavior change

Expected unaffected banks:

- `Apollo DS 1.0`
- `Katyusha_v170`
- `Kohaku Merry DiffSinger V2.0`

## Test Plan

### Unit tests

Add focused tests for:

1. inventory loading
- root-only inventory
- stage-local override inventory
- adjacent-stage fallback discovery

2. compressed stage inventories
- same symbol count, smaller unique ID count
- verify mapping like `aa -> 1`, `ah -> 1`

3. structural token fallback
- missing `AP` but present `SP`
- missing `SP` but present `AP`
- both missing should fail unless root-stage structural fallback is configured

4. strict symbol failures
- missing non-structural symbol must raise

### Integration tests

Add inference-routing tests that prove:

1. duration receives `dur_ids`
2. pitch receives `pitch_ids`
3. acoustic/root receives `root_ids`

### Smoke tests

Run real synth smoke tests for:

1. `PM-31_Commercial_Indigo`
2. `PM-31_Commercial_Scarlet`
3. one known-good shared-ID bank

Acceptance target:

- PM-31 banks no longer fail with pitch gather out-of-range errors

## Rollout Order

1. add cached stage inventory loader
2. preserve canonical phoneme symbols through alignment
3. build stage token bundle
4. route duration/pitch/root stages to their own IDs
5. add unit and smoke coverage

## Acceptance Criteria

The change is successful when:

1. PM-31 Indigo synthesizes past the current pitch-token out-of-range failure
2. PM-31 Scarlet synthesizes past the current pitch-token out-of-range failure
3. known shared-ID banks still synthesize unchanged
4. stage-local structural token gaps are handled by the narrow `SP/AP` fallback only
5. non-structural mapping gaps fail explicitly

## Recommendation

Implement this before broader OpenUtau phonemizer parity for the PM-31 family.

Reason:

- the current blocker is clearly stage-local token encoding
- this fix is small, deterministic, and easy to validate
