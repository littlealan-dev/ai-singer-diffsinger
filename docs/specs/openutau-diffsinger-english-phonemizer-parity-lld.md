# OpenUtau DiffSinger English Phonemizer Parity LLD

## Summary

Implement the smallest coherent OpenUtau parity unit that owns English DiffSinger phonemization behavior.

This change is intentionally larger than a one-class port and intentionally smaller than a full renderer port.

Port target:
- OpenUtau English DiffSinger phonemizer stack semantics

Not port target:
- renderer/UI/state/editor curves

## Scope

In scope:
- `DiffSingerEnglishPhonemizer` behavior
- `DiffSingerG2pPhonemizer` behavior needed by English
- the relevant subset of `DiffSingerBasePhonemizer`

Out of scope:
- `DiffSingerRenderer`
- `DiffSingerPitch`
- `DiffSingerVariance`
- `DiffSingerVocoder`
- OpenUtau GUI preferences

## Current Problem

The current backend path already supports:
- valid phoneme inventory loading
- language-aware dictionary lookup
- V2 alignment/timing

But that is still not enough for Hoshino-quality parity.

Why:
- OpenUtau's English DiffSinger path includes remapping and note-distribution semantics that our generic path does not replicate

## Minimal Coherent Port Unit

The port unit for v1 should include:

1. English phonemizer selector
2. dictionary loading order and replacement extraction
3. English G2P fallback semantics
4. phoneme validation / language prefix behavior
5. vowel/glide-aware note distribution

These pieces should be implemented together in Python.

## Proposed Module Layout

Recommended new files:

- `src/phonemizer/openutau_diffsinger_base.py`
- `src/phonemizer/openutau_diffsinger_g2p.py`
- `src/phonemizer/openutau_diffsinger_english.py`

Optional if implementation prefers fewer files:
- collapse the first two into one support module

Recommended responsibilities:

### `openutau_diffsinger_base.py`
- shared dictionary loading
- phoneme symbol validity tracking
- note/word distribution helpers
- phonetic hint parsing helpers

### `openutau_diffsinger_g2p.py`
- replacement/remapper behavior
- base G2P integration
- prefixed/unprefixed symbol handling

### `openutau_diffsinger_english.py`
- English selector
- English vowel/consonant sets
- English-specific configuration such as:
  - `dsdict-en.yaml`
  - language code `en`

## Voicebank Routing

Add a selector helper in synthesis/phonemize flow:

```python
def _select_phonemizer_mode(voicebank_path: Path, language: str) -> str:
    ...
```

Supported modes initially:
- `generic`
- `openutau_diffsinger_english`

Selection rule:
- if `language == "en"`
- and `character.yaml default_phonemizer == OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`
- select `openutau_diffsinger_english`

Otherwise:
- select `generic`

## Dictionary Semantics

The new parity unit must reproduce this behavior:

1. Try `dsdict-en.yaml`
2. Fallback to `dsdict.yaml`
3. Load:
   - entries
   - symbols
   - replacements when present

The parity unit should preserve:
- symbol type awareness
- replacement-driven remapping behavior

Phase 1 explicit limitation:
- no project/user dictionary override support
- only voicebank-local dictionaries are part of the parity unit

This is intentional to keep the implementation aligned with the current backend product model.

## G2P Semantics

The parity unit should not depend on OpenUtau binaries.

Instead:
- use the existing Python English G2P source already used in the codebase, namely `g2p_en` via the current phonemizer stack
- add a remapper layer matching OpenUtau's DiffSinger English expectations

V1 requirement:
- English fallback must be semantically close enough to OpenUtau's English DiffSinger phonemizer for declared banks

Reason for choosing `g2p_en` in v1:
- already present in the current backend phonemizer flow
- already exercised by existing tests
- avoids introducing a second English G2P dependency while we isolate parity differences to remapping and note distribution

## Note Distribution Semantics

This is mandatory in the parity unit.

Implement the OpenUtau-style logic that:
- treats vowels as syllable anchors
- attaches glides to the vowel onset when appropriate
- distributes phonemes note-by-note from those anchors

This is more than generic chunk splitting.

Recommended helper:

```python
def distribute_diffsinger_english_phonemes_to_notes(...):
    ...
```

Inputs:
- note group
- phoneme list
- vowel/glide classification

Outputs:
- per-note phoneme groups

Primary OpenUtau source of truth for this behavior:
- [DiffSingerBasePhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs)

Relevant behavior areas to port semantically:
- `ProcessWord(...)`
- vowel/glide start detection
- note-by-note phoneme distribution

Important implementation rule:
- transcribe the behavior from the referenced OpenUtau methods
- do not replace it with a new approximate algorithm in v1

## Integration Point

### File
- [phonemize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/phonemize.py)

Add:
- mode selector
- dedicated phonemizer construction

### File
- [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)

Add:
- mode-aware `_init_phonemizer(...)`
- mode-aware note-distribution path inside alignment

Important:
- keep the returned alignment payload shape unchanged

## Data Contract

The parity path must still return:

- `phonemes`
- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `word_pitches`
- `note_durations`
- `note_pitches`
- `note_rests`

No downstream schema changes in v1.

## Testing Plan

### Unit tests

Add tests for:
- mode selection from `character.yaml`
- dictionary preference:
  - `dsdict-en.yaml` over `dsdict.yaml`
- replacement/remapping application
- vowel/glide-based note distribution

Likely file:
- [test_phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_phonemizer.py)

### Integration tests

Add direct phonemization comparisons:
- generic path vs parity path for the same English inputs
- Hoshino-specific expected phoneme forms

Likely files:
- [test_api.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_api.py)
- new dedicated parity-focused test file if cleaner

### End-to-end tests

Extend:
- [test_end_to_end.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_end_to_end.py)

Cases:
1. Hoshino Amazing Grace soprano synthesizes through parity path
2. route selection is verified
3. note-level pitch diagnostics improve relative to current generic path baseline

## Diagnostics

Add debug fields/logging for parity mode:
- selected phonemizer mode
- selected dictionary path
- whether replacements were loaded
- first N phonemes for phrase
- note-to-phoneme grouping summary

Also log/cache:
- whether the parity phonemizer instance came from cache
- which voicebank path and language key it is cached under

This is critical because parity bugs will otherwise be hard to distinguish from pitch-model issues.

## Caching / Performance

The parity unit must not rebuild heavy state on every call if the same voicebank is reused.

Minimum expectation:
- cache the constructed parity phonemizer instance by:
  - `voicebank_path`
  - `language`
  - selected parity mode

Cached state may include:
- parsed dictionary entries
- parsed replacement table
- phoneme symbol/type maps
- initialized `g2p_en`-backed helper objects

Do not cache mutable per-request note-distribution outputs.

Reason:
- repeated construction cost would otherwise show up quickly in chat-based repeated synthesis flows
- this keeps parity mode operationally similar to the current phonemizer path

## Migration / Rollout

1. Land parity unit behind automatic bank selection only for declared English DiffSinger banks.
2. Keep generic path as the default for all others.
3. Compare Hoshino diagnostics before and after.
4. Only widen usage if the parity path materially improves quality.

## Risks

1. Partial parity if the remapper logic is underimplemented.
2. Hidden dependency on OpenUtau-specific G2P expectations.
3. More maintenance burden than a minimal custom patch.

## Explicit Non-Goals

1. No full renderer parity.
2. No ARPA+ English support in this change.
3. No new user-facing controls or settings.
4. No replacement of existing generic phonemizer behavior for all banks.

## Acceptance Criteria

1. A declared OpenUtau English DiffSinger voicebank is routed to the parity unit.
2. The parity unit includes the full minimal behavior-owning stack:
   - selector
   - dictionary semantics
   - G2P/remapper semantics
   - note distribution semantics
3. The downstream synthesis interfaces remain unchanged.
4. Tests cover routing, dictionary behavior, and Hoshino end-to-end use.
