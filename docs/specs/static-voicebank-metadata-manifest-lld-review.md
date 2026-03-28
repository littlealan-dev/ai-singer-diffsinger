# LLD Review: Static Voicebank Metadata Manifest

**Target Document:** `docs/specs/static-voicebank-metadata-manifest-lld.md`

## Overall Assessment
**Status: Approved with minor notes ✅**

This is a well-targeted optimization that directly eliminates the cold-start tarball extraction bottleneck for metadata-only operations. The LLD correctly identifies that the current `get_voicebank_info()` path unnecessarily triggers `resolve_voicebank_path()` → `_ensure_cached_voicebank()` → full tarball download, just to read `character.yaml` and `dsconfig.yaml` for prompt context. Pre-baking this into a checked-in JSON manifest is the right move.

---

### 1. Registry Replacement Strategy — Clean ✅

Replacing `voicebank_registry.yaml` entirely with the manifest is the correct call. The registry currently only carries `id`, `gender`, and `voice_type` (confirmed by reading the file). Rolling those into the manifest eliminates a redundant config file and the `get_registered_voicebank_metadata()` lookup chain in `voicebank.py` (Lines 360-365).

---

### 2. Manifest Field Completeness — Excellent ✅

The manifest schema captures everything the orchestrator's `_get_voicebank_details()` needs:
- `name`, `gender`, `voice_type` for LLM prompt context
- `voice_colors`, `default_voice_color` for voice selection
- `speakers`, `sample_rate`, `hop_size`, `use_lang_id` for synthesis config

This means zero tarball extraction needed for the entire voicebank selection workflow.

---

### 3. `enabled` Field — Smart Design ✅

The `enabled` boolean per entry is a simple but powerful mechanism for controlling voicebank visibility per environment. The rule that `get_voicebank_info()` still returns metadata for disabled entries (for internal/debug calls) while `list_voicebanks()` hides them is exactly right.

---

### 4. Fail-Loud Policy — Correct ✅

> "if manifest is missing or invalid: raise a configuration error. Do not fall back to storage-derived ID listing."

This is the right call. Silent fallback would mask deployment errors where someone forgot to update the manifest after adding a new voicebank tarball. Failing loudly at startup forces the manifest to stay in sync with the actual voicebank inventory.

---

### 5. Minor Notes for Implementation

#### 5a. `path_hint` Utility
The `path_hint` field (e.g., `UFR-V1.0/Hitsune_Kumi`) is described as "not used for runtime extraction logic in Phase 1." This is fine, but consider whether `resolve_voicebank_path()` could eventually use it as a hint to skip the nested directory discovery step (`discover_voicebank_root()`). Not needed now, but worth keeping in mind as a Phase 2 optimization.

#### 5b. Manifest Staleness Detection
The `generated_at` timestamp is a nice touch for human debugging. For Phase 2, consider adding a lightweight startup check that compares the manifest's voicebank IDs against the storage bucket contents and logs a warning (not an error) if there's drift. This would catch the "tarball exists but manifest wasn't updated" scenario early.

#### 5c. Dev Manifest Bootstrapping
The LLD says dev also uses a manifest when present. For local development where voicebanks live on the filesystem, make sure the dev manifest's `list_voicebanks()` integration doesn't accidentally prevent the current filesystem-scan path from working when someone has local voicebanks not in the manifest. The LLD's current rule (manifest-backed listing when manifest exists, error on miss) might be too strict for local dev where developers add experimental voicebanks frequently. Consider allowing dev to fall through to filesystem scan if the manifest lookup misses, or document that dev must update the manifest for any new local bank.

---

### Conclusion

The design is clean, well-scoped, and directly solves the cold-start performance problem. The registry replacement simplifies the config surface. Ready to proceed to implementation!
