# Text Phoneme Inventory Support LLD

## Summary

Add support for DiffSinger / OpenUtau voicebanks that ship a plain text phoneme inventory file such as `dsmain/phonemes.txt`, instead of the dict-style `phonemes.json` currently expected by the phonemizer.

Primary target:
- `assets/voicebanks/Hoshino Hanami v1.0/dsmain/phonemes.txt`

Current failure mode:
- `Phonemizer._load_phoneme_inventory(...)` expects YAML/JSON mapping data and raises:
  - `ValueError: Invalid phonemes.json format at .../phonemes.txt`

Goal:
- accept both inventory formats without regressing the current `phonemes.json` flow
- keep the rest of the synthesis pipeline unchanged


## Problem

Current phoneme inventory loading assumes:
- the path from `dsconfig.yaml` points to a mapping file
- that mapping file parses as a dictionary of:
  - `phoneme -> integer id`

This works for current compatible banks such as:
- `Raine_Rena_2.01`
- `Raine_Reizo_2.01`
- `Katyusha_v170`
- `Keiro_Revenant_v170`
- `Liam_Thorne_v170`
- `SAiFA_v170`

It fails for banks whose inventory is a line-based symbol list:
- one symbol per line
- implicit integer id = line number

Example:
- `phonemes.txt`
  - `<PAD>`
  - `SP`
  - `AP`
  - `A`
  - `E`
  - `hh`
  - `aw`


## Non-Goals

- no multi-language phonemizer redesign
- no new non-English runtime support in this change
- no automatic inference of per-language prefixes from text inventories
- no changes to `dsdict-*.yaml` selection logic yet
- no registry or UI changes


## Current Design Constraints

Relevant implementation points:
- inventory loading:
  - [phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/phonemizer.py)
- tests:
  - [test_phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_phonemizer.py)

Current behavior:
- `Phonemizer.__init__()` calls `_load_phoneme_inventory(self.phonemes_path)`
- inventory path is determined before that in synthesizer / pipeline setup:
  - use `dsconfig.yaml` `phonemes` field if present
  - otherwise default to `phonemes.json`
  - this change does not alter that path-resolution rule
- `_load_phoneme_inventory(...)`:
  - reads the file with `yaml.safe_load`
  - requires the result to be a `dict`
  - returns `{symbol: id}`

This is too strict for text inventories.


## Proposed Design

### 1. Support two inventory formats

`_load_phoneme_inventory(path)` will support:

1. Mapping format
- current behavior
- parse YAML/JSON mapping object
- example:
```yaml
SP: 0
AP: 1
en/aa: 2
```
- mapping values are still cast with `int(...)`
- if a mapping file contains non-numeric ids, loader should fail fast with `ValueError`
- it must not silently fall through to text parsing in that case

2. Text-list format
- new behavior
- if YAML parsing raises, or if the parsed structure is not a `dict`, fall back to plain-text line parsing
- each non-empty, non-comment line becomes one inventory symbol
- index is assigned by file order starting from `0`

Example:
```text
<PAD>
SP
AP
hh
aw
```

becomes:
```python
{
    "<PAD>": 0,
    "SP": 1,
    "AP": 2,
    "hh": 3,
    "aw": 4,
}
```


### 2. Text inventory parsing rules

For text-based inventories:
- trim whitespace
- ignore empty lines
- ignore comment lines starting with:
  - `#`
  - `;`
- preserve symbol case exactly
- preserve symbols containing punctuation or digits
- reject duplicate symbols

If duplicates exist:
- raise `ValueError`
- include the duplicate symbol in the error

Reason:
- duplicate symbols would make id assignment ambiguous


### 3. Keep validation behavior unchanged

After loading, the resulting inventory is still just:
- `Dict[str, int]`

This means existing validation logic remains unchanged:
- direct symbol pass-through still works
- narrow fallback from `en/x` to bare `x` still works
- language id resolution logic still works the same as before

This is important for mixed inventories like:
- `en/aw`
- `hh`


### 4. File type detection strategy

Do not branch by file extension alone.

Reason:
- some banks use:
  - `phonemes.json`
  - `phonemes.yaml`
  - `phonemes.txt`
- content shape is more reliable than extension

Proposed order:
1. read raw text
2. try `yaml.safe_load`
3. if YAML parsing raises `yaml.YAMLError`, treat parsed value as unavailable
4. if parsed value is a `dict`, use mapping loader
5. otherwise, parse raw text as line inventory

This allows:
- `.txt` line inventories
- `.json` or `.yaml` mapping inventories
- future banks with unusual extensions but valid mapping contents


## Implementation Plan

### File 1
- [phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/phonemizer.py)

Changes:
- revise `_load_phoneme_inventory(path)` to:
  - read raw text once
  - detect dict-style mapping vs line-based list
  - return `Dict[str, int]` for either format
- add a small helper:
  - `_parse_text_phoneme_inventory(raw_text: str, path: Path) -> Dict[str, int]`

Recommended structure:
```python
@staticmethod
def _load_phoneme_inventory(path: Path) -> Dict[str, int]:
    ...
    raw_text = path.read_text(encoding="utf8")
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        ...
    return Phonemizer._parse_text_phoneme_inventory(raw_text, path)
```

Related cleanup in the same change:
- update the method docstring from:
  - `Load phoneme inventory from phonemes.json.`
- to language that reflects both accepted formats
- update missing-file error wording from:
  - `Expected a phonemes.json from the voicebank.`
- to:
  - `Expected a phonemes.json or phonemes.txt from the voicebank.`


### File 2
- [test_phonemizer.py](/Users/alanchan/antigravity/ai-singer-diffsinger/tests/test_phonemizer.py)

Add tests for:
- line-based inventory loads correctly
- blank lines and comments are ignored
- duplicate symbol raises error
- text inventory still works with existing bare fallback:
  - inventory contains `hh`
  - dictionary emits `en/hh`
  - validation resolves to bare `hh`


### Optional verification test

Add or run a targeted end-to-end smoke test using:
- `assets/voicebanks/Hoshino Hanami v1.0`
- a simple single-part score

This does not need to be a permanent heavy test if runtime cost is too high.


## Detailed Behavior

### Mapping inventory examples

Input:
```yaml
SP: 0
AP: 1
hh: 2
en/aw: 3
```

Output:
```python
{"SP": 0, "AP": 1, "hh": 2, "en/aw": 3}
```


### Text inventory examples

Input:
```text
# base symbols
<PAD>
SP
AP

hh
aw
```

Output:
```python
{"<PAD>": 0, "SP": 1, "AP": 2, "hh": 3, "aw": 4}
```


### Duplicate error example

Input:
```text
SP
AP
SP
```

Error:
```text
ValueError: Duplicate phoneme 'SP' in phoneme inventory ...
```


## Risks

### Risk 1
Some existing mapping files might parse as a non-dict YAML structure by mistake.

Mitigation:
- this is acceptable
- falling back to text-line parsing will still fail loudly if the file content is nonsense


### Risk 2
Some text inventories may contain metadata lines not starting with `#` or `;`.

Mitigation:
- v1 intentionally supports only simple symbol-per-line inventories
- if a future bank uses richer text formatting, extend the parser then


### Risk 3
Language-prefixed and non-prefixed symbols may still be mixed in text inventories.

Mitigation:
- existing `_resolve_inventory_phoneme(...)` behavior remains unchanged
- this change only addresses inventory loading format, not language semantics


## Test Plan

Unit tests:
- mapping inventory still loads exactly as before
- text inventory loads with sequential ids
- comments and blank lines ignored
- duplicate lines raise `ValueError`
- `en/hh -> hh` fallback still works with text inventory

Integration verification:
- `get_voicebank_info()` on `Hoshino Hanami v1.0` still succeeds
- synthesis with a simple monophonic score should proceed past phonemizer initialization


## Rollout

1. implement loader support in `phonemizer.py`
2. add unit tests in `tests/test_phonemizer.py`
3. smoke test `Hoshino Hanami v1.0`
4. if successful, optionally add this voicebank to the curated registry later


## Acceptance Criteria

- `Phonemizer` accepts both mapping-style and text-list phoneme inventories
- existing compatible banks keep working with no behavior change
- `Hoshino Hanami v1.0` no longer fails at phoneme inventory loading due to `phonemes.txt`
- failures for malformed inventories remain explicit and actionable
