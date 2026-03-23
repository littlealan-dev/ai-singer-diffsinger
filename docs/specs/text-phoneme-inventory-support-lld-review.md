# Text Phoneme Inventory Support LLD — Review

Reviewed: 2026-03-23


## Critical

### `yaml.safe_load` will throw, not return a non-dict

The detection strategy says:

> 2. try `yaml.safe_load`
> 3. if parsed value is a `dict`, use mapping loader
> 4. otherwise, parse raw text as line inventory

The target `phonemes.txt` starts with `<PAD>`, which is **invalid YAML**. `yaml.safe_load` will raise `yaml.YAMLError`, not return a non-dict. The spec and recommended code structure must catch this exception:

```python
try:
    parsed = yaml.safe_load(raw_text)
except yaml.YAMLError:
    parsed = None
if isinstance(parsed, dict):
    return {str(k): int(v) for k, v in parsed.items()}
return Phonemizer._parse_text_phoneme_inventory(raw_text, path)
```

Without this, the real target voicebank will still raise instead of falling through.


## Minor

### 1. Example should include `<PAD>`

The example inventory (section "Problem") shows `SP, AP, A, E, hh, aw` but the real file starts with `<PAD>`. Add it to the example to clarify that angle-bracket symbols are expected and must be preserved. This confirms the "preserve symbols containing punctuation" rule covers it.

`<PAD>` is the neural network padding token (always ID 0). The text parser must preserve it — stripping it would shift every symbol's ID by one and break inference against the trained checkpoint.


### 2. Error message update

The current `FileNotFoundError` message says:

> Expected a phonemes.json from the voicebank.

Since both formats will be accepted, update to:

> Expected a phonemes.json or phonemes.txt from the voicebank.

Similarly, update the method docstring from `"Load phoneme inventory from phonemes.json."` to reflect both formats.


### 3. Inventory path resolution

Non-goal line says `"no changes to dsdict-*.yaml selection logic yet"` but doesn't explain how the inventory path is determined. The target bank's `dsconfig.yaml` doesn't appear to have a `phonemes` key. Worth a sentence on what drives the inventory path (convention vs config) to bound the scope.


### 4. `int()` cast on mapping values

The mapping branch does `int(v)`. If a mapping file has non-numeric values, this raises `ValueError`. This is fine as fail-fast behavior, but worth a sentence acknowledging it won't silently fall through to text parsing.


## Looks Good

- Content-based detection over extension — correct, real-world banks are inconsistent with naming.
- Non-goals are clear and well-scoped.
- Duplicate symbol rejection — ambiguous IDs should fail loudly.
- Risk analysis is thorough. Risk 1 mitigation is sound.
- Test plan covers the right cases, especially the `en/hh → hh` bare fallback with a text inventory.
- Leaving `_resolve_inventory_phoneme` unchanged is the right call — inventory format is orthogonal to language-prefix semantics.
