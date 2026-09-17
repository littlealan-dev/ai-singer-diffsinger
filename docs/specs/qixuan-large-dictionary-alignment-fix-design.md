# Manifest-Declared Phonemizer Dictionary Design

## Status

- Implemented
- Target deployment: synthesis backend
- No data migration required

## Problem

Qixuan English synthesis spent approximately five minutes in the reported `align` phase for a score that LIEE aligned in approximately one second.

The compared jobs had equivalent alignment workload:

| Metric | Qixuan | LIEE |
| --- | ---: | ---: |
| Phonemes | 467 | 467 |
| Words | 222 | 222 |
| Frames | 14,605 | 14,605 |
| Estimated duration | 169.56 seconds | 169.56 seconds |
| Alignment time | 298,706 ms | 1,145.73 ms |

Qixuan's English dictionary is approximately 27.6 MB, 2,246,016 lines, and 268,388 grapheme entries.

## Root Cause

`_find_dictionary()` discovers a dictionary by trying candidate paths in order. For every existing candidate, it runs `yaml.safe_load()` over the complete file to determine whether the YAML is parseable.

For Qixuan, the current flow was:

```text
_find_dictionary()
    -> fully parse the 27.6 MB dictionary
    -> discard the parsed object
    -> return only the path

Phonemizer
    -> open the same path again
    -> use the large-file selective loader
    -> retain only entries needed by the score
```

The discarded full parse defeated the existing adaptive loading optimization.

The parseability check remains useful for unconfigured voicebanks whose dictionary paths are not curated. The fix bypasses discovery for known voicebanks rather than changing legacy discovery behavior.

## Design Decision

Curated voicebanks may declare a complete language-to-dictionary mapping in the application voicebank manifest.

The decision is all-or-nothing at the voicebank level:

```text
phonemizer_dictionaries absent
    -> use _find_dictionary()

phonemizer_dictionaries present
    -> require one entry for every declared supported language
    -> resolve the configured path directly
    -> never call _find_dictionary() for that voicebank
```

There is no per-language discovery fallback for a configured voicebank. A partial mapping is an invalid manifest.

The manifest remains the source of truth for supported languages through its existing `languages` field. `phonemizer_dictionaries` only declares which dictionary file to use for each already-supported language.

## Manifest Schema

Example:

```json
{
  "id": "Qixuan_v2.7.0_DiffSinger_OpenUtau",
  "languages": ["en", "es", "ja", "zh"],
  "phonemizer_dictionaries": {
    "en": "dsdur/dsdict-en.yaml",
    "es": "dsdur/dsdict-es.yaml",
    "ja": "dsdur/dsdict-ja.yaml",
    "zh": "dsdur/dsdict-zh.yaml"
  }
}
```

Multiple languages may explicitly reference the same generic dictionary. A missing mapping is not interpreted as permission to discover a file.

Example for languages that intentionally share one generic dictionary:

```json
{
  "languages": ["en", "zh-yue"],
  "phonemizer_dictionaries": {
    "en": "dsdur/dsdict-en.yaml",
    "zh-yue": "dsdur/dsdict.yaml"
  }
}
```

The field is named `phonemizer_dictionaries` to distinguish it from the `dictionaries` field found in some upstream `dsconfig.yaml` files. The upstream field can refer to tab-separated phoneme mapping files and is not equivalent to an OpenUtau `dsdict*.yaml` pronunciation dictionary.

## Manifest Validation

When `phonemizer_dictionaries` is present, manifest loading requires:

1. `languages` is a non-empty list of unique normalized language codes.
2. `phonemizer_dictionaries` is a non-empty object.
3. Dictionary keys exactly equal the values in `languages`.
4. Every dictionary value is a non-empty relative path.
5. Absolute paths, parent traversal, surrounding whitespace, and backslash separators are rejected.

The manifest is validated before voicebank use. File existence cannot always be validated at manifest-load time because production voicebanks may not have been downloaded yet.

## Runtime Resolution

`resolve_manifest_phonemizer_dictionary()` performs runtime resolution:

1. Resolve the manifest voicebank ID from the local voicebank path.
2. Return `None` if the voicebank has no `phonemizer_dictionaries` field.
3. Look up the normalized requested language in the complete mapping.
4. Resolve the configured relative path under the actual voicebank root.
5. Verify the resolved path remains under the voicebank root.
6. Verify the file exists and is a regular file.
7. Return the path without pre-parsing the YAML.

If a configured mapping is incomplete, missing on disk, or escapes the voicebank root, resolution fails. It does not silently invoke `_find_dictionary()`.

`_resolve_dictionary_path()` provides the common API rule:

```python
configured = resolve_manifest_phonemizer_dictionary(voicebank_path, language)
if configured is not None:
    return configured
return _find_dictionary(voicebank_path, language=language)
```

Both direct phonemization and synthesis initialization use this resolver.

## Parsing Contract

Trusting the configured path removes only the separate discovery-time format validation.

`Phonemizer` must still parse the file once to build its pronunciation lookup:

- Dictionaries below the configured size threshold use the eager YAML loader.
- Large dictionaries with requested graphemes use the selective line-oriented loader.

For Qixuan English, the final flow is:

```text
manifest mapping
    -> dsdur/dsdict-en.yaml
    -> Phonemizer adaptive loader
    -> selective large-file scan
```

If a trusted configured dictionary is malformed, loading fails as a voicebank configuration error. Discovery does not conceal the manifest defect.

## Configured Voicebanks

The development and production manifests configure only the four enabled voicebanks.

### Printto Magicbeat Indigo

| Language | Dictionary |
| --- | --- |
| `en` | `dsdur/dsdict-en.yaml` |
| `es` | `dsdur/dsdict-es.yaml` |
| `ja` | `dsdur/dsdict-ja.yaml` |
| `th` | `dsdur/dsdict-th.yaml` |

### Printto Magicbeat Scarlet

| Language | Dictionary |
| --- | --- |
| `en` | `dsdur/dsdict-en.yaml` |
| `es` | `dsdur/dsdict-es.yaml` |
| `ja` | `dsdur/dsdict-ja.yaml` |
| `th` | `dsdur/dsdict-th.yaml` |

### Qixuan

| Language | Dictionary |
| --- | --- |
| `en` | `dsdur/dsdict-en.yaml` |
| `es` | `dsdur/dsdict-es.yaml` |
| `ja` | `dsdur/dsdict-ja.yaml` |
| `zh` | `dsdur/dsdict-zh.yaml` |

### LIEE Immortal Idol

| Language | Dictionary |
| --- | --- |
| `en` | `dsdur/dsdict-en.yaml` |
| `es` | `dsdur/dsdict-es.yaml` |
| `fr` | `dsdur/dsdict-fr.yaml` |
| `it` | `dsdur/dsdict-it.yaml` |
| `ja` | `dsdur/dsdict-ja.yaml` |
| `pt` | `dsdur/dsdict-pt.yaml` |
| `pt-eu` | `dsdur/dsdict-pt-eu.yaml` |
| `zh` | `dsdur/dsdict-zh.yaml` |
| `zh-yue` | `dsdur/dsdict-zh-yue.yaml` |

LIEE Cantonese deliberately uses its language-specific dictionary because its replacement rules are required to produce the expected model-phone sequence.

## Component and Function Changes

| Component | Module / Function | Change |
| --- | --- | --- |
| Manifest validation | `src/api/voicebank_cache.py::_load_voicebank_manifest_for_path` | Validate complete `phonemizer_dictionaries` mappings and safe relative paths. |
| Manifest resolution | `src/api/voicebank_cache.py::resolve_manifest_phonemizer_dictionary` | Resolve and verify the configured dictionary file without parsing it. |
| Common selection | `src/api/phonemize.py::_resolve_dictionary_path` | Use the manifest mapping when present; otherwise call `_find_dictionary()`. |
| Direct phonemization | `src/api/phonemize.py::phonemize` | Use the common resolver. |
| Synthesis | `src/api/synthesize.py::_init_phonemizer` | Use the common resolver. |
| Production configuration | `env/voicebank_manifest.prod.json` | Add complete mappings for the four enabled voicebanks. |
| Development configuration | `env/voicebank_manifest.dev.json` | Add the same complete mappings. |

## Error Handling

| Condition | Behavior |
| --- | --- |
| Mapping absent | Use legacy `_find_dictionary()` discovery. |
| Mapping present but incomplete | Reject the manifest. |
| Configured path is unsafe | Reject the manifest or runtime resolution. |
| Configured file is missing | Raise a configuration-focused `FileNotFoundError`; do not discover another file. |
| Configured file is malformed | Let `Phonemizer` loading fail; do not discover another file. |
| Valid dictionary lacks a lyric word | Continue using existing G2P fallback. |

## Test Coverage

1. Reject a mapping missing a declared language.
2. Reject unsafe parent-directory traversal.
3. Resolve a configured dictionary to its expected file.
4. Return `None` when a voicebank has no mapping.
5. Confirm `_resolve_dictionary_path()` does not call `_find_dictionary()` when a configured path exists.
6. Confirm legacy discovery is called only when the complete mapping field is absent.
7. Load both development and production manifests and verify exactly the four enabled voicebanks have mappings.
8. Verify every configured local development path exists.
9. Run focused Qixuan, LIEE, and Printto phonemization regression tests.

## Deployment and Rollback

This is a synthesis-backend change. No frontend, Firestore, billing, or stored-session migration is required.

The voicebank archives must contain the configured relative paths. Local assets already contain the corresponding files; production archive contents should be verified before deployment.

Rollback consists of reverting the backend code and manifest fields. Stored jobs and voicebank assets are unaffected.

## Acceptance Criteria

- Both environment manifests pass schema validation.
- Exactly the four enabled voicebanks declare complete mappings.
- Every supported language of those voicebanks has an explicit dictionary path.
- LIEE `zh-yue` resolves directly to `dsdur/dsdict-zh-yue.yaml`.
- Qixuan English resolves directly to `dsdur/dsdict-en.yaml`.
- `_find_dictionary()` is not called for any configured voicebank.
- Legacy voicebanks without the field preserve current discovery behavior.
- Qixuan's large dictionary is not fully parsed and discarded before adaptive loading.
- Existing phoneme and G2P behavior remains unchanged.
