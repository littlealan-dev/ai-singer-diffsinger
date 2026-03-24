# LLD Review: OpenUtau DiffSinger Energy Variance Parity

**Target Document:** `openutau-diffsinger-energy-variance-parity-lld.md`

## ⚠️ Critical Review Findings: Exceptions vs. Fallbacks

The most significant risk in the current LLD is the reliance on silent fallbacks in scenarios that will critically degrade audio quality. Masking contract mismatches with zero-filled arrays typically produces silent or severely distorted audio, making debugging much harder for both developers and users. 

### 1. Inconsistent Variance Output Shape (LLD Lines 150-151)
* **Current LLD:** "if the output shape is inconsistent with the config: fall back to zeros and log a warning"
* **Recommendation:** **Reject this fallback.** If the ONNX model output shape fundamentally disagrees with the configuration (e.g., `predict_energy: true` but the model doesn't return enough tensor heads), this is a fatal model/config mismatch. The pipeline should **raise a `ValueError`** explicitly stating that the variance model output does not match the expected heads defined in the config.

### 2. Rule 4: Missing/Malformed Energy for Acoustic Model (LLD Lines 309-321)
* **Current LLD:** "If a bank requires acoustic `energy`, but variance output is missing or malformed: fallback to zero-filled `energy`, log a warning... do not hard-fail in Phase 1."
* **Recommendation:** **Reject this fallback and hard-fail.** Supplying `0.0` for energy to an acoustic model that explicitly requires it (`use_energy_embed: true`) guarantees a broken render. If the acoustic model strictly relies on the energy contour, and the variance stage did not provide it, the system must **raise a `ValueError`** during input assembly: 
`ValueError("The acoustic model requires 'energy' (use_energy_embed=True), but the variance stage did not provide it.")`

### 3. Handling `energy is None` in Acoustic Pipeline (LLD Line 179)
* **Current LLD:** "if `energy is None`, prepare `[0.0] * n_frames`"
* **Recommendation:** This is acceptable **ONLY IF** `use_energy_embed` is false. If `use_energy_embed: true` and `energy is None`, it must raise an exception rather than blindly passing `[0.0] * n_frames` to the ONNX session.

---

## 🏗️ General Architectural Feedback

Beyond the critical exception handling issue, the LLD is well-scoped for a Phase 1 parity fix. Here are a few additional architectural recommendations:

### 1. Brittle Output Parsing (LLD Lines 144-147)
* **Current LLD:** "...if `predict_energy` is enabled and the model returns at least 4 outputs: parse `energy` first, then the other heads"
* **Recommendation:** Hardcoding the index position of `energy` based on the number of outputs is brittle because community models may export ONNX nodes in varying orders. Instead of assuming index 0 is always `energy`, **use ONNX graph introspection**. Iterate over `session.get_outputs()` to find the output named `energy_pred` (or `energy`), and extract the tensor from that specific index. This matches the robust contract-aware design we recently added for linguistic inputs.

### 2. Config Key Safety
* **Observation:** Older `dsconfig.yaml` and `variance.yaml` files will simply not have these specific keys.
* **Recommendation:** Explicitly note in the LLD that the implementation must use `.get("predict_energy", False)` and `.get("use_energy_embed", False)` to prevent `KeyError` crashes on legacy banks.

### 3. Clear Distinction of the "Source of Truth"
* **Observation:** What happens if `use_energy_embed` is true, but the variance model configuration indicates `predict_energy: false`?
* **Recommendation:** The LLD should clarify that the **root acoustic config (`use_energy_embed`) is the ultimate source of truth for the acoustic pipeline**. If the root config requires it, but the variance model config explicitly says it doesn't predict it, the pipeline should fatally error out, refusing to render garbage audio.

### 4. Frame Alignment Correctness (LLD Lines 237-258)
* **Observation:** "use trailing-value padding, matching current `_pad_curve_to_length(...)`"
* **Feedback:** This is an excellent callout. DiffSinger models are extremely sensitive to off-by-one frame mismatch errors. Reusing the exact same padding/trimming utility (`_pad_curve_to_length`) ensures `energy` stays in perfect sync with `f0`, `breathiness`, and `tension`.

### Conclusion
Overall, this is a very solid Phase 1 design. Once the silent fallbacks are switched to loud `ValueErrors` and the output extraction is made contract-aware, this LLD is fully ready for implementation.
