# Review: OpenUtau DiffSinger Word-Level Linguistic Parity (HLD & LLD)

## Overview

This review covers the High-Level Design (HLD) and Low-Level Design (LLD) for the `OpenUtau DiffSinger Word-Level Linguistic Parity` feature.

The proposed design is highly pragmatic and well-architected. By shifting from a voicebank-specific allowlist approach to a model-signature-aware contract classification, the system will become much more resilient and future-proof as newer DiffSinger model families are introduced. Centralizing the linguistic input preparation is a much-needed cleanup for the inference pipeline.

---

## High-Level Design (HLD) Review

### Strengths
* **Dynamic Contract Classification:** Selecting the compatibility path based on ONNX input signatures (`word_div`, `languages`, etc.) rather than hardcoded voicebank names is an excellent, scalable architectural decision. Core compatibility should indeed be driven by structure, not naming.
* **Centralized Runner:** Introducing `DiffSingerLinguisticRunner` to unify how `predict_durations`, `predict_pitch`, and `predict_variance` invoke linguistic encoders will eliminate a significant source of fragmented, ad hoc code.
* **Pragmatic Parity Scope:** Choosing to reuse the backend's existing `word_boundaries` and `word_durations` as the Phase 1 representation of OpenUtau's phrase segmentation is exactly the right call. It keeps the feature focused on the missing integration layer rather than triggering a cascading rewrite of the timing engine.

### Areas for Clarification
* **Open Questions:** The HLD lists a few open questions at the end (e.g., whether to recompute `word_div`/`word_dur` vs. using current alignment outputs). Given that the LLD clearly decides to use the backend's current alignment outputs for Phase 1, it might be beneficial to formally close these questions in the HLD, confirming that backend reuse is the official Phase 1 strategy, with recomputations deferred to Phase 2 if needed.

---

## Low-Level Design (LLD) Review

### Strengths
* **Clear Input Definitions:** The exact shape, dtype (`int64`), and derivation rules for each new tensor (`word_div`, `word_dur`, `languages`) are explicitly defined.
* **Robust Validation:** Enforcing invariants like `sum(word_boundaries) == len(phoneme_ids)` right before ONNX execution is a great way to catch subtle alignment bugs early with clear error messages.
* **Exhaustive Testing Plan:** The unit testing plan covers all the contract matrices (`TOKENS_ONLY`, `TOKENS_WORD`, `TOKENS_WORD_LANG`, etc.), which is critical for a routing layer like this.

### Areas for Clarification / Potential Edge Cases
* **Language ID Fallback Logic:** The LLD states: *"if contract requires languages but use_lang_id is false: pass zeros with the same token length"*. 
  * *Question:* For DiffSinger models, does passing an array of `0`s safely default to the model's primary language? In some models, `0` might map to a specific language embedding (like Chinese), which could distort the phoneme latent space if the actual lyrics are Japanese. It may be worth confirming if we should read a "default language ID" from the voicebank's `languages.json` instead of hardcoding `0`.
* **Rest Handling in Word Boundaries:** The LLD notes `word_div` is shape `[1, n_words]` and its values must be `>= 1`. 
  * *Question:* How do standalone rests (like `<AP>` or `<SP>`) map into `word_div`? Do they count as their own "word" with a boundary of `1`, or are they merged? It would be helpful to explicitly state the expected behavior for rest tokens to ensure developers don't trip over a `0` boundary validation error.

---

## Conclusion

The architecture proposed in these documents is mature, well-reasoned, and correctly scoped. The approach to dynamically inspect ONNX models and build the required input tensors will definitively resolve the "missing input" errors for newer voicebanks like `KITANE_DS_2.0.0` and `Mairu_Maishi_v2_0_0 2`. Aside from minor clarifications regarding default language IDs and rest token counting, this specification is ready for implementation.
