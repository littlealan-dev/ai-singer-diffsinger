# Static Voicebank Metadata Manifest LLD

## Goal

Remove cold-start metadata warming cost for production voicebank selection.

Today, after a backend restart, the first LLM request that needs rich voicebank metadata can trigger:

1. `list_voicebanks`
2. one `get_voicebank_info` call per returned voicebank
3. on-demand download and extraction of each voicebank archive just to read metadata

This is correct but scales poorly as the voicebank inventory grows.

The Phase 1 fix is to pre-extract the metadata needed for:

- `list_voicebanks`
- `get_voicebank_info`
- orchestrator LLM prompt context

into a static checked-in manifest file.

Production will serve metadata from that manifest. Voicebank tarballs will only be downloaded when synthesis actually needs a specific voicebank.

This revised design also replaces the current voicebank registry file entirely. The manifest becomes the single source of truth for:

- selectable voicebank IDs
- display metadata
- gender / voice type metadata
- enablement status per environment

## Scope

In scope:

- replace the current registry with environment-specific static manifests
- load that manifest at runtime
- make production `list_voicebanks()` use manifest data instead of storage-only ID stubs
- make production `get_voicebank_info()` use manifest data instead of forcing `resolve_voicebank_path()`
- make voicebank enablement explicit via per-entry boolean `enabled`
- preserve existing on-demand archive download for synthesis and any runtime operations that truly need the extracted voicebank

Out of scope:

- automatic manifest generation in CI
- changing synthesis-time cache behavior
- changing the tarball storage layout

## Current Behavior

### Production `list_voicebanks`

In [voicebank.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank.py):

- when `search_path is None and is_prod_env()`
- `list_voicebanks()` returns storage-derived IDs only
- output shape is:
  - `{"id": voicebank_id, "name": voicebank_id, "path": voicebank_id}`

This is lightweight, but too weak for LLM prompting.

### Production `get_voicebank_info`

In [voicebank.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank.py):

- `get_voicebank_info("UFR-V1.0")`
- treats the plain ID as a production voicebank
- calls `resolve_voicebank_path()`
- which calls `_ensure_cached_voicebank()`
- which downloads and extracts the tarball

Only after extraction can it read:

- `character.yaml`
- `dsconfig.yaml`
- `languages`
- `subbanks`
- gender / voice type metadata currently held in the registry

### Orchestrator usage

In [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py):

- `_get_voicebank_details()` calls:
  - `list_voicebanks`
  - then `get_voicebank_info` for each voicebank ID
- the LLM prompt currently uses only:
  - `id`
  - `name`
  - `gender`
  - `voice_type`
  - `voice_colors`
  - `default_voice_color`

So the expensive cold-start path is currently driven mostly by prompt metadata hydration, not synthesis.

## Target Behavior

After backend restart:

1. `list_voicebanks` returns manifest-backed rich entries immediately
2. `get_voicebank_info` returns manifest-backed info immediately
3. LLM can choose `Hitsune Kumi (UFR-V1.0)` and `Core` without downloading `UFR-V1.0.tar.gz`
4. the archive is downloaded only when:
   - synthesize
   - phonemize
   - inference
   - any other runtime path actually needs files inside the voicebank

## Manifest Design

### File location

Add checked-in environment-specific manifest files:

- [voicebank_manifest.dev.json](/Users/alanchan/antigravity/ai-singer-diffsinger/env/voicebank_manifest.dev.json)
- [voicebank_manifest.prod.json](/Users/alanchan/antigravity/ai-singer-diffsinger/env/voicebank_manifest.prod.json)

The old registry file should be removed:

- [voicebank_registry.yaml](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/voicebank_registry.yaml)

JSON is preferred for Phase 1 because:

- it is unambiguous
- fast to load
- easy to generate later with a script

### Top-level shape

```json
{
  "version": 1,
  "generated_at": "2026-03-27T00:00:00Z",
  "voicebanks": [
    {
      "id": "UFR-V1.0",
      "enabled": true,
      "name": "Hitsune Kumi - DiffSinger AI",
      "storage_object": "assets/voicebanks/UFR-V1.0.tar.gz",
      "path_hint": "UFR-V1.0/Hitsune_Kumi",
      "languages": [],
      "has_duration_model": true,
      "has_pitch_model": true,
      "has_variance_model": true,
      "speakers": ["dsacoustic/millefeuille_v001.kumi.emb"],
      "voice_colors": [
        {"name": "Core", "suffix": "dsacoustic/millefeuille_v001.kumi.emb"}
      ],
      "default_voice_color": "Core",
      "sample_rate": 44100,
      "hop_size": 512,
      "use_lang_id": true,
      "gender": "female",
      "voice_type": "soprano"
    }
  ]
}
```

### Manifest fields

The manifest should store the full current `get_voicebank_info()` response shape plus the minimal extra fields useful for storage/debugging.

Required fields per voicebank:

- `id`
- `enabled`
- `name`
- `storage_object`
- `path_hint`
- `languages`
- `has_duration_model`
- `has_pitch_model`
- `has_variance_model`
- `speakers`
- `voice_colors`
- `default_voice_color`
- `sample_rate`
- `hop_size`
- `use_lang_id`
- `gender`
- `voice_type`

Field meaning:

- `id`
  - production-visible voicebank ID
  - must match tarball basename
- `enabled`
  - whether this voicebank is selectable and returned by metadata listing in this environment
  - `false` means present in manifest but hidden from normal selection/prompting
- `name`
  - singer display name from `character.yaml`
- `storage_object`
  - object path inside the voicebank bucket
  - useful for consistency checks and future tooling
- `path_hint`
  - informative nested root hint, e.g. `UFR-V1.0/Hitsune_Kumi`
  - not used for runtime extraction logic in Phase 1
- the remaining fields mirror current `get_voicebank_info()`

## Source of Truth Rules

Phase 1 source of truth:

- production metadata API:
  - manifest
- dev metadata API when manifest mode is enabled:
  - dev manifest
- production archive content:
  - tarball in storage
- synthesis/inference runtime:
  - extracted tarball content

This means:

- metadata in manifest must be updated whenever a new voicebank is added or its published metadata changes
- the manifest is intentionally a deploy-time snapshot

## Runtime Loading

### New loader

Add a manifest loader in either:

- [voicebank.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank.py)
or
- [voicebank_cache.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank_cache.py)

Recommended functions:

- `_voicebank_manifest_path() -> Path`
- `_load_voicebank_manifest() -> dict`
- `get_manifest_voicebank_ids() -> list[str]`
- `get_manifest_voicebank_entry(voicebank_id: str) -> dict | None`
- `list_enabled_manifest_voicebanks() -> list[dict]`

Recommended path selection rule:

- if `APP_ENV` / `ENV` is production-like:
  - use `env/voicebank_manifest.prod.json`
- otherwise:
  - use `env/voicebank_manifest.dev.json`

Optional override for testing:

- `VOICEBANK_MANIFEST_PATH`

Caching:

- use `@lru_cache(maxsize=1)` for the parsed manifest
- process-local cache is acceptable because manifest only changes on redeploy

## API Changes

### `list_voicebanks()`

Current prod behavior:

- returns storage-derived IDs with `name=id`

New manifest-backed behavior:

- if the environment manifest exists and is valid, return manifest-backed entries:
  - `id`
  - `name`
  - `path`
- only include entries where `enabled == true`

Recommended returned `path` in prod:

- `path` should remain the voicebank ID for compatibility
- do not expose `path_hint` as `path`
- this avoids implying the nested filesystem path exists locally before extraction

So prod `list_voicebanks()` should return:

```json
{"id": "UFR-V1.0", "name": "Hitsune Kumi - DiffSinger AI", "path": "UFR-V1.0"}
```

Fallback behavior:

- if manifest is missing or invalid:
  - raise a configuration error
  - do not fall back to storage-derived ID listing

Dev behavior:

- dev should follow the same manifest-backed listing behavior when a dev manifest is present
- this allows local testing of enable/disable behavior before prod rollout

### `get_voicebank_info()`

Current prod behavior:

- plain ID triggers `resolve_voicebank_path()`
- which downloads/extracts the archive

New manifest-backed behavior:

- if `voicebank` is a plain ID:
  - first try manifest lookup
  - if found, return manifest-backed info directly
  - do not call `resolve_voicebank_path()`

Enablement rule:

- if manifest entry exists but `enabled == false`:
  - `get_voicebank_info()` should still be allowed to return metadata for direct/internal calls
  - but `list_voicebanks()` must not expose it as selectable

Fallback behavior:

- if manifest lookup misses:
  - raise a configuration error
  - do not fall back to on-demand extraction for metadata APIs

This preserves compatibility for:

- direct file-path calls in local/dev that are not using manifest-backed ID lookup

## Synthesis Path

No change.

When synthesis receives:

- `voicebank="UFR-V1.0"`

runtime code should still call:

- `resolve_voicebank_path("UFR-V1.0")`

and download/extract on cache miss.

So the only change is:

- metadata-only paths become manifest-backed
- file-dependent paths remain extraction-backed

## Orchestrator Impact

In [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py):

- `_get_voicebank_details()` can remain unchanged in Phase 1
- it still calls:
  - `list_voicebanks`
  - `get_voicebank_info`

But now both are cheap in production.

Expected result:

- first metadata-heavy request after restart is fast
- no bulk tarball warming just for prompt context

## Manifest Maintenance Workflow

Phase 1 manual workflow:

1. add or update voicebank tarball in storage
2. update:
   - [voicebank_manifest.dev.json](/Users/alanchan/antigravity/ai-singer-diffsinger/env/voicebank_manifest.dev.json)
3. test locally with `enabled: true` in dev manifest
4. when ready for production, update:
   - [voicebank_manifest.prod.json](/Users/alanchan/antigravity/ai-singer-diffsinger/env/voicebank_manifest.prod.json)
5. redeploy backend

No automatic generator is required in Phase 1.

## Validation Rules

At startup or first manifest load:

- validate top-level object shape
- validate unique `id`
- validate required fields exist
- validate `enabled` is boolean
- validate `voice_colors` entries have:
  - `name`
  - `suffix`

Failure policy:

- treat missing or invalid manifest as a deployment/configuration error
- raise loudly for manifest-backed metadata APIs
- do not silently substitute storage-derived results

## Backward Compatibility

Preserved behaviors:

- synthesis still downloads tarballs on demand in prod
- synthesis still downloads tarballs on demand in dev when using ID-based archive-backed flow
- manifest absence does not break prod; it only disables the optimization
- current API response shapes remain unchanged

## Risks

### Drift between manifest and tarball

Risk:

- manifest says `Core`
- tarball changes to `Main`

Impact:

- LLM may suggest stale voice color names

Mitigation:

- manifest update is part of voicebank onboarding/release checklist

### Dev/prod manifest drift

Risk:

- a voicebank is enabled in dev but forgotten in prod
- or prod metadata lags behind the dev-tested manifest

Mitigation:

- treat prod manifest as explicit publish control
- dev and prod manifest updates should be part of the voicebank onboarding checklist

### Missing manifest entry for stored tarball

Risk:

- voicebank exists in storage but not in manifest

Mitigation:

- manifest maintenance is part of voicebank publishing
- metadata APIs should fail loudly when an ID is requested but missing from the active manifest

Recommended Phase 1 behavior:

- `list_voicebanks()` returns enabled manifest entries only when manifest exists
- this makes the published set explicit and deploy-controlled

## Tests

### Unit tests

Add tests for:

- manifest loader returns expected entries
- environment-specific manifest path selection works for dev vs prod
- `list_voicebanks()` returns only `enabled=true` entries from the active manifest
- prod `list_voicebanks()` returns manifest-backed names without extraction
- prod `get_voicebank_info()` returns manifest-backed metadata without calling `resolve_voicebank_path()`
- invalid manifest raises a configuration error
- manifest miss raises a configuration error

### Integration tests

Add tests for:

- orchestrator `_get_voicebank_details()` on prod env with manifest does not trigger voicebank download
- synthesize still triggers normal on-demand cache/extraction path

### Regression check

Reproduce current bad case:

- cold process restart
- first request asks for a voice like `Hitsune Kumi`

Expected after fix:

- no `voicebank_downloaded` log lines during metadata selection step
- `voicebank_downloaded` appears only when synthesis actually starts

## Acceptance Criteria

- production `list_voicebanks()` returns rich names from manifest without tarball extraction
- production `list_voicebanks()` returns only `enabled=true` voicebanks from the prod manifest
- dev can expose additional test voicebanks through the dev manifest without exposing them in prod
- production `get_voicebank_info()` returns manifest-backed metadata for known IDs without tarball extraction
- orchestrator can identify `Hitsune Kumi (UFR-V1.0)` and `Core` immediately after restart
- synthesis still downloads and caches `UFR-V1.0.tar.gz` only when requested for rendering
- registry file is no longer needed
