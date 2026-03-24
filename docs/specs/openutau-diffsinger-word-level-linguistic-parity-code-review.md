# Code Review: OpenUtau DiffSinger Word-Level Linguistic Parity

## Overview
This code review covers the implementation of the `OpenUtau DiffSinger Word-Level Linguistic Parity` feature, which adds support for deriving and providing `word_div` and `word_dur` to DiffSinger ONNX linguistic models that require them.

## Files Reviewed
- **New Modules:** `src/api/diffsinger_linguistic_inputs.py`, `tests/test_diffsinger_linguistic_inputs.py`
- **Modified Modules:** `src/api/inference.py`, `src/api/synthesize.py`

## Feedback & Findings

### 1. `src/api/diffsinger_linguistic_inputs.py` (Linguistic Input Builder)
- **Contract Classification:** Perfect mirroring of the LLD. Using `set` for name comparison correctly ignores ONNX input ordering.
- **`use_lang_id` Guard:** The module properly checks `use_lang_id`. If `False`, it safely short-circuits to passing `[0] * token_count` as required by the LLD, completely avoiding unnecessary map parsing.
- **Safe Fallbacks & Loud Failures:** If `use_lang_id` is True but the voicebank doesn't supply a valid `language` mapping, the code safely raises a `ValueError` rather than silently swallowing the configuration gap. It also features a highly visible `logger.warning` when leaning on a fallback language ID.
- **Tensor Building Validation:** The validation logic surrounding boundary constraints (`word_boundaries do not sum to phoneme count`, length mismatches, negative check) is thorough and safe.

### 2. `tests/test_diffsinger_linguistic_inputs.py` (Unit Tests)
- Highly comprehensive test suite perfectly capturing edge cases.
- Tests reliably verify that `use_lang_id=False` triggers the padding shortcut, and that `use_lang_id=True` without a valid configuration gracefully raises `ValueError`.

### 3. `src/api/inference.py` & `src/api/synthesize.py` (Integration)
- **Stage Config/Language Maps:** Uses an elegant recursive checking strategy for language maps via `_load_stage_language_map`. Checks the child stage (e.g., `dsdur/dsconfig.yaml`) first before properly falling back to the voicebank's global `languages.json`.
- **Unified Routing:** The routing for `predict_durations`, `predict_pitch`, and `predict_variance` have securely been moved behind the contract-aware `run_linguistic_model`.
- **Feature Passing:** Correctly provides `word_boundaries` and `word_durations` sourced cleanly from the front-end alignment maps.

## OpenUtau Parity Validation
As investigated during the spec review, natively exposing `word_boundaries` in this interface maps structurally perfectly to the OpenUtau `predict_dur=false` acoustic model behavior. Additionally, because the backend effectively computes true boundaries directly via alignment and maintains them, we robustly solve this parameter for both Acoustic models and Pitch/Variance models without reverting to OpenUtau's vowel-guessing (`g2p.IsVowel`) heuristics.

## Conclusion
**Status: Approved.**  
The revised feature branch solidly resolves early configuration fallback bugs and appropriately protects the contract builder against invalid maps. The integration cleanly unlocks support for ONNX models demanding `word_div` / `word_dur` inputs while guaranteeing `tokens_only` and `ph_dur` models continue operating without regressions. You can safely merge.
