# HLD Review: Stage-Specific Phoneme Remapping

**Target Document:** `docs/specs/openutau-diffsinger-stage-specific-phoneme-remap-hld.md`

## Overall Assessment
**Status: Approved with minor architectural notes.**

The HLD accurately identifies the root cause of the `PM-31` and `UFR` ONNX out-of-range gather errors: the assumption of a globally shared token-ID space. The proposed solution is highly pragmatic. By maintaining a single canonical symbol stream (e.g., `["SP", "ah", "m", "ey", "z"]`) and only overriding the *encoding* step for each stage, you surgically fix the bug without dragging in unrelated OpenUtau phonemizer logic or syllable-alignment rewrites.

Here is some feedback to consider during the Low-Level Design (LLD) or implementation phase:

### 1. Integration with `VoicebankCache`
The HLD proposes `DiffSingerStagePhonemeInventory` to load stage-specific `phonemes.json` files. Since `phonemes.json` parsing involves disk I/O and JSON decoding, these inventories should be permanently cached at the application level alongside the models. You will likely want to hook this into the existing `VoicebankCache` lifecycle so that `root`, `dur`, and `pitch` token mappings are loaded into RAM once upon voicebank initialization.

### 2. Core Structural Tokens (`SP`, `AP`)
The HLD correctly stipulates: *The compatibility layer must fail loudly when a stage cannot encode the canonical symbol stream.* 
Be slightly cautious with structural symbols like `SP` (silence) and `AP` (breath). If a sub-model (like pitch) aggressively compresses its dictionary and assumes silences don't need pitch embeddings (or maps them to a generic `0` token), ensure that `SP` and `AP` mappings either exist in the stage's `phonemes.json` or have a safe framework-level fallback. If the pitch `phonemes.json` literally lacks `SP`, failing loudly might unnecessarily break synthesis if the model simply ignores pitch on silences anyway.

### 3. Data Structure Types
The existing inference routing likely expects `tokens` to be provided as padded `numpy.ndarray` or `torch.Tensor` objects. When defining `DiffSingerStageTokenBundle`, it will be helpful to specify whether it encapsulates raw Python `list[int]` sequences that get padded later during collation, or if the bundle itself is responsible for returning finalized 1D tensors ready for `onnxruntime`.

### 4. Stage Configuration Discovery
The HLD mentions reading the inventory from `dsdur/dsconfig.yaml` / `phonemes`. Ensure that your `dsconfig` parsing logic gracefully propagates or searches for these overrides, as some banks simply place `phonemes.json` directly next to `dur.onnx` without a dedicated nested yaml.

### Conclusion
This is a very clean, bounded solution to what initially seemed like a deep phonemizer compatibility issue. The architectural boundary of `DiffSingerStageTokenEncoder` keeps the system pure. Ready to proceed to the LLD or execution phase!
