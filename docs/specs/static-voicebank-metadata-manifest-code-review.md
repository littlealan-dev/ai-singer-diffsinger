# Code Review: Static Voicebank Metadata Manifest Implementation

**Changed Files:**
- `env/voicebank_manifest.dev.json` (NEW — 12 entries)
- `env/voicebank_manifest.prod.json` (NEW — 6 entries)
- `src/backend/config/voicebank_registry.yaml` (DELETED)
- `src/api/voicebank_cache.py` (MODIFIED)
- `src/api/voicebank.py` (MODIFIED)
- `tests/test_api.py` (MODIFIED)
- `tests/test_voicebank_cache.py` (MODIFIED)

## Overall Assessment
**Status: Approved with 3 items to address ✅**

The implementation is clean, well-aligned with the LLD, and achieves the core goal: metadata-only API calls no longer trigger tarball downloads. The registry→manifest migration is surgically complete.

---

### 1. `voicebank_cache.py` — Solid ✅

**Manifest loader & validation:** The `_load_voicebank_manifest_for_path()` function is excellent. Strict validation with clear error messages for every failure mode: missing file, unparseable JSON, wrong shape, missing `id`, non-boolean `enabled`, duplicates. This is exactly the fail-loud policy the LLD demands.

**GCS listing eliminated:** `_list_voicebank_ids_gcs()` now just returns `get_enabled_manifest_voicebanks()` IDs — no more `list_blobs()` calls for metadata. This is the single biggest latency win.

**`_ensure_cached_voicebank` uses `storage_object`:** The archive download path now reads `storage_object` from the manifest entry instead of constructing the archive path from the voicebank ID + prefix. This is correct and decouples the archive naming convention from the voicebank ID.

**Environment routing:** `_voicebank_manifest_path()` correctly routes to `dev` or `prod` manifest based on `_app_env()`, with `VOICEBANK_MANIFEST_PATH` override for tests.

---

### 2. `voicebank.py` — Clean integration ✅

**`list_voicebanks()` refactored well.** The `search_path is None` branch now returns manifest-backed entries for both dev and prod, which is correct per the LLD's unified design. The old `is_prod_env()` gating is gone.

**`get_voicebank_info()` short-circuit is correct.** When given a plain ID, it first checks the manifest and returns immediately without `resolve_voicebank_path()`. The fail-loud `raise FileNotFoundError(...)` on manifest miss matches the LLD.

**Filesystem path fallthrough preserved.** When `voicebank` is a `Path` that already exists on disk, the original filesystem-based logic still runs (reads `dsconfig.yaml`, `character.yaml`, etc.). This means local dev with explicit paths is unaffected.

---

### 3. Items to Address

#### 3a. `list_voicebanks(search_path=...)` — filesystem path broken (P1 — must fix)

When `list_voicebanks` is called with an explicit `search_path` argument (e.g., `list_voicebanks(ROOT_DIR / "assets/voicebanks")`), the function now falls through to the `search_path = Path(search_path)` block below. But the old `search_path is None` default-to-`assets/voicebanks` code was removed:

```python
# Old code (removed):
if search_path is None:
    search_path = root_dir / "assets" / "voicebanks"
```

This means if someone calls `list_voicebanks()` with no arguments, it correctly uses the manifest. But the existing test `test_list_voicebanks_only_returns_enabled_manifest_voicebanks` calls `list_voicebanks(ROOT_DIR / "assets/voicebanks")` — this takes the **filesystem scan path**, not the manifest path. It passes today because the filesystem voicebanks happen to match the dev manifest entries.

The problem surfaces when a voicebank is `enabled: true` in the dev manifest but doesn't exist on the local filesystem. `list_voicebanks(some_path)` will not find it, while `list_voicebanks()` will.

**Suggestion:** This is acceptable behavior since explicit `search_path` is a dev/test convenience. But the test name `test_list_voicebanks_only_returns_enabled_manifest_voicebanks` is misleading when it's really testing the filesystem scan path. Consider renaming to `test_list_voicebanks_with_explicit_search_path_uses_filesystem` or changing the test to call `list_voicebanks()` (no args) to actually test the manifest path.

#### 3b. `get_voicebank_info()` for plain ID now returns `"path": voicebank_id` (P2 — minor)

In the manifest short-circuit path (Line ~341):
```python
result = {
    "name": manifest_entry.get("name") or voicebank,
    "path": voicebank,  # ← returns "UFR-V1.0" as path
    ...
}
```

This returns the voicebank ID string as `path`, which is consistent with what `list_voicebanks()` returns for prod. However, the old filesystem-based path returned `str(path.resolve())` — a full absolute path. Any downstream code that calls `get_voicebank_info(some_id)["path"]` and then uses it as a filesystem path will break.

**Quick check:** Does anything in `orchestrator.py` or `tools.py` use the `path` field from `get_voicebank_info()` as a filesystem path? If it's only used for display/identification, this is fine. If it's passed to `Path(...)` for file operations, it needs the resolved path.

#### 3c. Stale "registry" wording in `tools.py` (P3 — cosmetic)

`src/mcp/tools.py` Lines 1222 and 1226 still reference "backend registry" in the API field descriptions for `gender` and `voice_type`. These should be updated to say "manifest" for consistency.

---

### 4. Manifest Data Files — Correct ✅

**Dev manifest (12 voicebanks):** All local development voicebanks are present, including the newer PM-31 Indigo/Scarlet and UFR entries. 

**Prod manifest (6 voicebanks):** Production subset is tightly controlled. Notably excludes voicebanks pending licensing approval (Raine, Hoshino Hanami, Kohaku Merry, Mairu Maishi, KITANE).

**PM-31_Commercial_Scarlet missing from prod:** The compatibility matrix shows both PM-31 Indigo and Scarlet as "Pass" with licensing requests sent. But only Indigo is in the prod manifest. If this is intentional (Scarlet not yet approved), that's fine — just confirming.

---

### 5. Tests — Good Coverage ✅

- `test_list_voicebanks_only_returns_enabled_manifest_voicebanks` — validates the enabled filter
- `test_get_voicebank_info_errors_for_voicebank_missing_from_manifest` — validates fail-loud on miss
- `test_get_voicebank_info_includes_manifest_gender_and_voice_type` — validates manifest metadata propagation
- `test_prod_env_errors_when_manifest_is_missing` — validates startup error on missing manifest file
- All `test_voicebank_cache.py` fixtures correctly updated from YAML to JSON manifests

---

### Conclusion

The core optimization is well-executed and will eliminate cold-start tarball extraction for metadata operations. The three items flagged are minor: a misleading test name (3a), a `path` field semantic change to verify (3b), and cosmetic doc strings (3c). Ready to commit after confirming 3b.
