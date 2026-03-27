# Code Review: Stage-Specific Phoneme Remap Implementation

**Changed Files:**
- `src/api/diffsinger_stage_tokens.py` (NEW)
- `src/api/voicebank_cache.py` (MODIFIED)
- `src/api/synthesize.py` (MODIFIED)
- `tests/test_diffsinger_stage_tokens.py` (NEW)

## Overall Assessment
**Status: Approved with 2 minor items to consider ✅**

The implementation precisely follows the approved LLD and cleanly solves the PM-31 / UFR out-of-range pitch token crash. The architecture is sound and well-separated. Here is the detailed feedback:

---

### 1. `diffsinger_stage_tokens.py` — Clean and Correct ✅

The new module does exactly what the LLD specifies. The `StagePhonemeInventory` and `DiffSingerStageTokenBundle` dataclasses are frozen, immutable, and hold raw `list[int]` as designed. The `SP`↔`AP` structural fallback in `_encode_structural_symbol` is correctly narrow.

**One observation (minor):** The bundle now includes `variance_ids` (Line 34, 97, 103), which extends beyond the LLD's Phase 1 scope (the LLD said "keep using root IDs" for variance). This is actually a *good* forward-looking decision since it makes the routing explicit. Just double-check that current voicebanks without a `dsvariance/` directory correctly fall back to root IDs—which they should via `_resolve_stage_phoneme_inventory_path_cached`'s final fallback.

---

### 2. `voicebank_cache.py` — Excellent caching layer ✅

The `@lru_cache` pattern with `str` keys for hashability is exactly the right approach. The multi-tier discovery in `_resolve_stage_phoneme_inventory_path_cached` is robust:
- Explicit `phonemes` key in `dsconfig.yaml` → use it
- Adjacent `phonemes.json` → use it
- Adjacent `dsmain/phonemes.json` → use it
- Duration always falls back to root
- Other stages fall back to root only if no stage subdirectory exists

**One item to watch (P2 / non-blocking):** The `@lru_cache` is process-level and never invalidated. This is fine for production (voicebanks don't change at runtime), but in the test suite, `tempfile.TemporaryDirectory()` creates unique paths each time so cache poisoning across tests won't happen. However, if someone later writes a test that reuses the same path with different inventory contents, this could produce confusing failures. Consider adding a `_clear_stage_caches()` test helper or calling `_resolve_stage_phoneme_inventory_path_cached.cache_clear()` in test teardown. This is non-blocking for now.

---

### 3. `synthesize.py` — Clean integration ✅

The refactoring of `_apply_articulation_gaps` from `phoneme_ids` → `phoneme_symbols` is smart. By keeping the symbol-level representation through the articulation gap insertion (where new `SP`/`AP` symbols get inserted), you guarantee that the final `build_stage_token_bundle(...)` call after articulation correctly encodes all phonemes—including the newly inserted silence gaps.

The conditional rebuild pattern is also efficient:
```python
stage_tokens = (
    build_stage_token_bundle(voicebank_path, phoneme_symbols)
    if phoneme_symbols != alignment["phonemes"]
    else initial_stage_tokens
)
```
This avoids redundant encoding when articulation doesn't mutate the symbol stream.

**One question:** The alignment call now uses `include_phonemes=True` (Line 1567). Is this a new flag you added to the aligner, or was it already supported but unused? If the aligner previously didn't return a `phonemes` key, this could break non-stage-remap code paths that don't expect it. Worth a quick sanity check.

---

### 4. `test_diffsinger_stage_tokens.py` — Good coverage ✅

The test suite covers the key scenarios from the LLD:
- Stage-specific pitch IDs vs root IDs
- Duration fallback to root
- `AP` → `SP` structural fallback
- Missing non-structural symbol raises `ValueError`
- Compressed pitch inventory (duplicate IDs)

**Suggestion (non-blocking):** Consider adding one test for the `variance` stage fallback to root when no `dsvariance/` directory exists, since you've now wired it up in the bundle.

---

### Conclusion

The implementation is solid, well-structured, and follows the LLD faithfully. The two minor items (variance stage forward-porting and `lru_cache` test isolation) are non-blocking. Ready to commit and smoke-test on PM-31 Indigo!
