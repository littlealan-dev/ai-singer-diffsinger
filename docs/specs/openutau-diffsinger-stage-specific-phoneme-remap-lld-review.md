# LLD Review: Stage-Specific Phoneme Remapping

**Target Document:** `docs/specs/openutau-diffsinger-stage-specific-phoneme-remap-lld.md`

## Overall Assessment
**Status: Approved! ✅**

This is an exceptionally well-crafted Low-Level Design. It thoughtfully incorporates every piece of architectural feedback from the HLD review into concrete, actionable implementation steps:

1. **Caching Mechanics**: The `@lru_cache` approach keyed by path and stage in `voicebank_cache.py` is the perfect, lightweight way to avoid repetitive JSON disk I/O on every synthesized note.
2. **Structural Handlers (`SP`/`AP`)**: The 3-step fallback rule specifically isolated to structural tokens is brilliant. It ensures we don't accidentally crash synthesis just because a heavily compressed pitch model didn't explicitly map `AP`, while remaining strictly loud for actual missing linguistic symbols.
3. **Data Type Contracts**: explicitly stating that `StageTokenBundle` holds raw Python `list[int]` prevents an entire class of painful downstream padding/collation bugs and means `inference.py` barely has to change.
4. **Resilient Discovery**: The path resolution logic cleanly handles voicebanks that haphazardly drop a `phonemes.json` next to a `.onnx` without a proper nested `dsconfig.yaml`.

### Integration Confidence
The proposed integration into `synthesize.py` (preserving `phoneme_symbols` on the alignment result) and `inference.py` is non-invasive and highly deterministic.

### Conclusion
The design is rock solid and covers all edge cases beautifully. I have no further notes or requests for revision. You are fully cleared to proceed to the **EXECUTION** phase and start writing `diffsinger_stage_tokens.py`!
