# Adaptive Voicebank Dictionary Loading LLD

## 1. Purpose

Define the minimal but complete implementation required to support oversized OpenUtau `dsdict*.yaml` files without blocking phonemizer initialization.

This design targets voicebanks such as `Qixuan_v2.7.0_DiffSinger_OpenUtau`, whose duration dictionary ships a full English lexicon with hundreds of thousands of entries. The goal is to preserve dictionary-based pronunciation when available, while avoiding eager parsing of the full YAML file for every render.

## 2. Scope

This change covers:

- adaptive dictionary loading in the phonemizer path
- selective extraction of only the lyric words needed for one render when the dictionary is oversized
- preserving current eager-load behavior for small dictionaries
- preserving `g2p_en` fallback for out-of-dictionary words
- focused tests for large-dictionary lookup and compatibility with current small-dictionary banks

This change does not cover:

- changing pitch, variance, acoustic, or vocoder behavior
- replacing phonemization with grapheme-level model inputs
- changing voicebank package layout on disk
- reformatting existing Qixuan dictionary files
- implementing a global lexicon cache across processes

## 3. Problem Statement

Current behavior:

- phonemizer initialization loads the entire dictionary via `yaml.safe_load(...)`
- this works for small banks such as `Hoshino Hanami v1.0` and `PM-31_Commercial_Scarlet`
- it stalls for Qixuan because `dsdur/dsdict.yaml` and `dsdur/dsdict-en.yaml` are each about `27.6 MB`

Observed Qixuan dictionary characteristics:

- about `2,246,016` lines
- about `268,388` grapheme entries
- entries are unique, not repeated internally
- `dsdur/dsdict.yaml` and `dsdur/dsdict-en.yaml` are byte-for-byte identical

The render hang occurs before duration prediction, during phonemizer initialization, because `_load_dictionary(...)` eagerly parses the full YAML lexicon.

## 4. Current Workflow

Current phonemizer flow:

1. resolve `phonemes.json`
2. resolve `languages.json`
3. resolve dictionary path with `_find_dictionary(...)`
4. initialize `Phonemizer(...)`
5. `Phonemizer.__init__` calls `_load_dictionary(...)`
6. `_load_dictionary(...)` performs `yaml.safe_load(...)`
7. phonemize lyric tokens using dictionary lookup, then fall back to `g2p_en` if needed

Relevant code:

- [`src/api/phonemize.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/api/phonemize.py)
- [`src/api/synthesize.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/api/synthesize.py)
- [`src/phonemizer/phonemizer.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/phonemizer/phonemizer.py)

## 5. Design Summary

Use one adaptive dictionary-loading path with two internal strategies:

1. Small dictionary strategy
- keep current eager `yaml.safe_load(...)`
- used for existing small voicebanks

2. Large dictionary strategy
- collect the normalized lyric words needed for the current render
- line-scan the dictionary file once
- extract only matching grapheme entries plus symbol metadata
- initialize the phonemizer with that reduced dictionary subset

Shared behavior:

- dictionary pronunciations are preferred when present
- `g2p_en` remains fallback for missing words
- the caller-facing phonemizer API remains unchanged

This is intentionally adaptive rather than always-selective:

- small banks already work and should stay simple
- large lexicons need selective loading to remain usable

## 6. Functional Requirements

### 6.1 Adaptive strategy selection

The system must choose dictionary loading strategy automatically based on dictionary size.

Recommended initial rule:

- if dictionary file size is less than or equal to `LARGE_DICT_THRESHOLD_BYTES`, use eager load
- otherwise use selective extraction

Recommended initial threshold:

- `5 MB`

The threshold must be configurable in code as a constant so it can be tuned later without redesign.

### 6.2 Needed-word collection

For selective loading, the system must collect the lyric words required for the current render before phonemizer initialization.

The collected tokens must:

- use the same normalization logic as dictionary lookup
- ignore empty tokens
- deduplicate normalized forms

For the current score path, this means:

- group notes into lyric groups
- resolve group lyric text
- normalize each lyric using the phonemizer’s grapheme normalization rule

### 6.3 Selective dictionary extraction

For oversized dictionaries, the system must:

- line-scan the YAML file without `yaml.safe_load(...)` on the entire file
- detect entry boundaries under the `entries:` section
- keep only entries whose normalized grapheme is in the needed-word set
- preserve each matched entry’s phoneme list in order

The selective path must stop scanning early if:

- all required normalized words have been found

### 6.4 Symbol metadata handling

The system must still populate vowel and glide metadata used by:

- `is_vowel(...)`
- `is_glide(...)`
- slur distribution logic

For oversized dictionaries, symbol metadata must be extracted from the `symbols:` section without full YAML parse.

### 6.5 Fallback behavior

If a lyric word is not found in the selectively loaded dictionary subset:

- phonemization must continue using existing `g2p_en` fallback

If the selective loader encounters an unrecognized line structure:

- it must raise a clear error rather than silently return corrupted entries

## 7. Detailed Design

### 7.1 New loader abstraction

Add a dictionary-loading layer in [`src/phonemizer/phonemizer.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/phonemizer/phonemizer.py) that supports:

```python
def load_dictionary_bundle(
    path: Path,
    *,
    language: str,
    needed_graphemes: Optional[set[str]] = None,
) -> DictionaryBundle:
    ...
```

Where `DictionaryBundle` contains:

- `dictionary: Dict[str, List[str]]`
- `vowels: set[str]`
- `glides: set[str]`
- `load_strategy: Literal["eager", "selective"]`

Reason:

- current `Phonemizer` loads dictionary entries and symbol metadata separately
- the large-dictionary path needs one shared file pass to avoid duplicated scanning

### 7.2 Phonemizer constructor extension

Extend `Phonemizer(...)` to accept an optional needed-word set:

```python
Phonemizer(
    phonemes_path=...,
    dictionary_path=...,
    languages_path=...,
    language="en",
    allow_g2p=True,
    needed_graphemes=None,
)
```

Behavior:

- if `needed_graphemes is None`, preserve current behavior
- if `needed_graphemes` is provided and the dictionary is oversized, use selective loading

This keeps the API backward-compatible for existing tests and callers.

### 7.3 Selective YAML scanning algorithm

For oversized `dsdict*.yaml`, parse the file as a stream:

1. Read until `symbols:`
2. Collect `- symbol:` blocks and `type:` fields
3. Read until `entries:`
4. For each `- grapheme:` block:
- capture raw grapheme
- normalize grapheme using the same normalization function used at lookup time
- if normalized grapheme is needed, collect its `phonemes:` list
- otherwise skip the block without storing it
5. Stop early when all needed graphemes are found

Assumptions supported by current Qixuan file shape:

- `symbols:` appears before `entries:`
- each entry block begins with `- grapheme:`
- phoneme lines are listed beneath `phonemes:`

The parser does not need to support arbitrary YAML features. It only needs to support the OpenUtau dictionary structure we ship.

### 7.4 Needed-word derivation in API path

In [`src/api/synthesize.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/api/synthesize.py), compute required graphemes before `_init_phonemizer(...)`.

Recommended helper:

```python
def _collect_needed_graphemes(notes: List[Dict[str, Any]]) -> set[str]:
    ...
```

Inputs:

- grouped note lyrics for the selected part and voice

Output:

- normalized grapheme set for the current render

Then call:

```python
phonemizer = _init_phonemizer(
    voicebank_path,
    language="en",
    needed_graphemes=needed_graphemes,
)
```

### 7.5 Reuse in `phonemize(...)`

The standalone [`phonemize(...)`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/src/api/phonemize.py) API should also use the adaptive path.

Recommended behavior:

- derive `needed_graphemes` directly from the `lyrics` argument
- pass that set into `Phonemizer(...)`

This keeps direct API calls and synth-path calls consistent.

## 8. Compatibility Strategy

### 8.1 Small dictionaries

Small dictionaries continue to use eager loading.

Expected impact:

- no behavior change for Scarlet, Hoshino, and similar banks
- no new complexity in their normal path

### 8.2 Large dictionaries

Large dictionaries use selective extraction.

Expected impact:

- Qixuan can initialize phonemization without parsing the full YAML lexicon
- dictionary pronunciations are retained for words used in the current score

### 8.3 Missing entries

If the dictionary subset does not contain a word:

- the phonemizer must behave exactly as it does today and fall back to `g2p_en`

## 9. Testing Plan

### 9.1 Unit tests

Add unit tests covering:

- strategy selection by file size threshold
- selective loader extracts only requested graphemes
- selective loader preserves phoneme order
- selective loader preserves vowel/glide symbol metadata
- selective loader stops early once all requested graphemes are found
- missing selective entries still use `g2p_en`

Recommended file:

- [`tests/test_phonemizer.py`](/Users/alanchan/antigravity/ai-singer-diffsinger-integration/tests/test_phonemizer.py)

### 9.2 Integration tests

Add a focused test that:

- derives the Amazing Grace soprano lyric set
- selective-loads entries from Qixuan’s dictionary
- confirms all required words are found
- confirms the loader completes within a reasonable time budget

This test should not require full audio synthesis.

### 9.3 End-to-end validation

After implementation:

- rerun Qixuan Amazing Grace soprano render
- confirm synthesis progresses beyond phonemizer initialization
- confirm output WAV is produced

## 10. Risks

### 10.1 YAML shape assumptions

Risk:

- selective scanning assumes a stable OpenUtau `dsdict` layout

Mitigation:

- fail fast on unsupported structure
- keep eager path for small dictionaries

### 10.2 Normalization mismatch

Risk:

- the needed-word collector and dictionary loader could normalize graphemes differently

Mitigation:

- centralize normalization in one helper owned by `Phonemizer`

### 10.3 Symbol metadata drift

Risk:

- if symbol metadata is parsed incorrectly, vowel/glide logic can regress

Mitigation:

- explicitly test `is_vowel(...)` and `is_glide(...)` behavior on selective bundles

## 11. Rollout Plan

1. Add selective dictionary loader and tests
2. Wire `needed_graphemes` through `phonemize(...)` and `_init_phonemizer(...)`
3. Validate small-dictionary banks still pass current phonemizer tests
4. Validate Qixuan Amazing Grace soprano render completes

## 12. Expected Outcome

After this change:

- small voicebanks keep current behavior
- oversized lexicon voicebanks no longer stall during phonemizer initialization
- dictionary-based pronunciation is preserved for the words actually used in a render
- out-of-dictionary words still work through `g2p_en`
