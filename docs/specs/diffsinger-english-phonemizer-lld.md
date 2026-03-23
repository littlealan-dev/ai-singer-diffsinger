# DiffSinger English Phonemizer LLD

## Summary

Implement a dedicated `DiffSingerEnglishPhonemizer` path for English DiffSinger voicebanks that explicitly declare OpenUtau's English DiffSinger phonemizer in `character.yaml`.

This change is additive:
- keep the current generic phonemizer and alignment path
- add a new specialized path
- route only selected banks to it

Primary target:
- `assets/voicebanks/Hoshino Hanami v1.0`

## Problem Statement

Current synthesis uses a generic phonemizer/alignment path for English singing.

For Hoshino:
- synthesis completes
- the predicted pitch is unstable before vocoding
- V2 timing/alignment is already enabled and does not remove the issue
- the voicebank explicitly declares:
  - `default_phonemizer: OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`

This strongly suggests a bank-specific parity mismatch in:
- phoneme normalization
- phoneme-to-note distribution
- DiffSinger English dictionary behavior

## Scope

In scope:
- read voicebank-declared default phonemizer metadata
- add a new internal `DiffSingerEnglishPhonemizer`
- add strategy selection logic
- build alignment payloads using the new path
- add targeted tests

Out of scope:
- full OpenUtau phrase renderer parity
- non-English dedicated phonemizers
- ARPA+ parity in this first pass
- replacing current generic phonemizer globally

## Current Relevant Files

- phonemizer core:
  - [phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/phonemizer.py)
- phonemize API:
  - [phonemize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/phonemize.py)
- synthesis orchestration:
  - [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)
- voicebank metadata:
  - [voicebank.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank.py)
- OpenUtau references:
  - [DiffSingerEnglishPhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/Phonemizers/DiffSingerEnglishPhonemizer.cs)
  - [DiffSingerG2pPhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/Phonemizers/DiffSingerG2pPhonemizer.cs)
  - [DiffSingerBasePhonemizer.cs](/Users/alanchan/antigravity/ai-singer-diffsinger/third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs)

## Proposed Design

### 1. Add phonemizer strategy selection

Add a new internal selector in synthesis/phonemize flow:

```python
def _select_phonemizer_strategy(voicebank_path: Path, language: str) -> str:
    ...
```

Initial return values:
- `"generic"`
- `"diffsinger_english"`

Selection rule for v1:
- if `language == "en"`
- and `character.yaml` declares:
  - `OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`
- return `"diffsinger_english"`
- otherwise return `"generic"`

### 2. Add voicebank phonemizer metadata reader

Extend voicebank metadata loading with a helper such as:

```python
def load_voicebank_character_metadata(voicebank_path: Path) -> Dict[str, Any]:
    ...
```

Needed fields:
- `default_phonemizer`

This should read:
- `character.yaml` when present

If absent:
- return empty/default metadata

### 3. Add new dedicated phonemizer class

Add a new internal class, likely under:
- [phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/phonemizer.py)
- or a new file such as:
  - `src/phonemizer/diffsinger_english_phonemizer.py`

Recommended class:

```python
class DiffSingerEnglishPhonemizer(Phonemizer):
    ...
```

Responsibilities:
- use English-specific dictionary selection
- preserve DiffSinger-English-compatible phoneme forms
- apply dedicated remapping / normalization rules
- expose phoneme results in the same shape as `Phonemizer`

### 4. Add dedicated English dictionary handling

The dedicated path should use:
1. `dsdict-en.yaml`
2. fallback `dsdict.yaml`

This path should use the current language-aware dictionary lookup, but also allow dedicated normalization rules on top.

### 5. Add DiffSinger-English remapping layer

Implement a small remapping stage modeled after OpenUtau's `DiffSingerG2pPhonemizer`.

V1 behavior:
- respect dictionary output
- preserve bank-specific symbols that already exist in inventory
- allow simple replacements/remapping data when present in dictionary payload

Do not attempt full OpenUtau phonemizer-class parity immediately.

Minimum viable parity target:
- produce bank-expected English phoneme forms more faithfully than the generic path

### 6. Add vowel-centered note distribution path

Implement a dedicated phoneme-to-note distribution function for the new path.

Recommended helper:

```python
def _split_diffsinger_english_phonemes_into_note_chunks(...):
    ...
```

Behavior:
- vowels define note anchors
- consonant-glide-vowel patterns keep the glide with the vowel onset
- consonants before/after vowel are grouped around the vowel-centered note chunk

This should be used instead of the current generic split helper when the dedicated path is active.

### 7. Keep downstream alignment contract unchanged

The dedicated path must still produce downstream data in the same shape as current synthesis expects.

Required fields:
- `phoneme_ids`
- `language_ids`
- `word_boundaries`
- `word_durations`
- `word_pitches`
- `note_durations`
- `note_pitches`
- `note_rests`
- `phonemes` when requested

### 8. Do not replace current generic path

The existing generic path stays as-is for:
- current compatible English banks
- non-English banks
- banks that do not declare the dedicated OpenUtau phonemizer

## Detailed File Plan

### File 1
- [voicebank.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/voicebank.py)

Add:
- helper to load `character.yaml` metadata
- helper to return `default_phonemizer`

### File 2
- [phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/phonemizer.py)
- or new file:
  - `src/phonemizer/diffsinger_english_phonemizer.py`

Add:
- `DiffSingerEnglishPhonemizer`
- dedicated English remapping helpers

### File 3
- [phonemize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/phonemize.py)

Add:
- strategy selection
- construction of dedicated phonemizer when selected

### File 4
- [synthesize.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)

Add:
- strategy selection in `_init_phonemizer(...)`
- dedicated phoneme splitting / note distribution helper path
- preserve current downstream result shape

## Strategy Selection Logic

Pseudo-code:

```python
def _select_phonemizer_strategy(voicebank_path: Path, language: str) -> str:
    if language != "en":
        return "generic"
    meta = load_voicebank_character_metadata(voicebank_path)
    if meta.get("default_phonemizer") == "OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer":
        return "diffsinger_english"
    return "generic"
```

Future extension:
- support `DiffSingerARPAPlusEnglishPhonemizer`

## Tests

### Unit tests

Add tests for:
- strategy selection by `character.yaml`
- fallback to generic when no declared phonemizer exists
- dedicated English phonemizer loads `dsdict-en.yaml`
- dedicated note splitting is vowel-centered

Likely files:
- [test_phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_phonemizer.py)
- [test_api.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_api.py)

### Integration tests

Add targeted comparison test:
- same word sequence through:
  - generic English path
  - `DiffSingerEnglishPhonemizer`
- verify the dedicated path output matches the expected Hoshino-style symbol conventions more closely

### End-to-end tests

Extend:
- [test_end_to_end.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_end_to_end.py)

Cases:
1. Hoshino Amazing Grace soprano still synthesizes
2. dedicated phonemizer path is actually selected
3. note-level pitch diagnostic for the first note improves relative to the baseline generic path

## Acceptance Criteria

1. The system can detect `default_phonemizer: OpenUtau.Core.DiffSinger.DiffSingerEnglishPhonemizer`.
2. Hoshino is routed to the new dedicated path automatically for English synthesis.
3. Existing compatible banks can still use the generic path unchanged.
4. The dedicated path preserves the current downstream synthesis contract.
5. The codebase has automated tests covering strategy selection and Hoshino end-to-end invocation.

## Open Questions

1. Should ARPA+ English be supported in the same change or a follow-up?
- recommendation:
  - follow-up

2. Should dedicated phonemizer selection also consider explicit user language?
- recommendation:
  - yes later, but not required for the first Hoshino-focused implementation

3. Should the dedicated path eventually own phrase-level timing too?
- recommendation:
  - maybe, but only if phonemizer/note-distribution parity alone is insufficient
