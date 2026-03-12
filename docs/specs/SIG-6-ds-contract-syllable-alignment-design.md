# SIG-6: DiffSinger Contract + SyllableBased Alignment Design

## 1. Goal
Improve lyric-to-note alignment quality (especially split words and continuation notes) by adopting:
- DiffSinger input contract semantics as the hard interface.
- OpenUtau `SyllableBasedPhonemizer` alignment strategy as the note/syllable mapping algorithm.

This targets failures like:
- split words drifting off-beat (`voic-es`, `mil-lion`, `grat-i-tude`)
- wrong continuation handling (`+`, `middle`, `end`)
- unstable per-note phoneme assignment in long lyric chains.

## 2. Non-Goals
- Replacing DiffSinger model inference code.
- Changing voice-part planning logic.
- Rewriting score parser schema.
- Full parity with all OpenUtau language plugins in one phase.

## 3. Current Problem Summary
Current `src/api/synthesize.py` does mixed responsibilities:
- groups notes and continuation chains
- builds word-level phonemes
- heuristically splits word phonemes to notes
- builds DS arrays

Main weakness: split logic is mostly post-phonemize chunking. It lacks an explicit syllable object model (`prevV`, `cc`, `v`, per-note container) used in OpenUtau’s robust strategy.

## 4. Target Architecture
Introduce a dedicated alignment module:
- `src/api/syllable_alignment.py`

Pipeline:
1. Parse selected notes into note groups (lyrical note + continuation notes).
2. Convert lyric input to phoneme symbol sequence per lyrical token.
3. Build explicit syllable units from symbols.
4. Assign syllables to syllabic carrier notes (single/begin/middle/end notes).
5. Expand continuation notes (`+`, tie continuation) according to continuation policy.
6. Emit DS-ready contract payload.

`synthesize.py` should consume the new module output and stop doing custom chunk heuristics.

### 4.1 API boundary (explicit)
`syllable_alignment.align(...)` returns fully flattened DS-contract arrays, not intermediate-only objects.

`align(...)` ownership decision:
- `align(...)` receives a `Phonemizer` instance from caller.
- `align(...)` does not create its own phonemizer.
- Rationale: reuse caller-configured language/voicebank context and avoid duplicate init/cache behavior.

Proposed input signature:
- `notes: List[Dict[str, Any]]`
- `start_frames: List[int]`
- `end_frames: List[int]`
- `timing_midi: List[float]`
- `phonemizer: Phonemizer`
- `voicebank_path: Path`
- `include_phonemes: bool = False`
- `use_v2: bool = True`

Proposed return payload:
- `phoneme_ids`
- `language_ids`
- `durations`
- `word_boundaries`
- `phonemes` (debug)
- `positions`
- `tones`
- `note_phonemes`
- `note_slur`
- `coda_tails` (for downstream tail shaping)

`positions` contract:
- `positions` is per-phoneme group start frame offsets (same flattening axis as `phoneme_ids`).
- It is not per-word-group and not per-note-only.
- Length rule: `len(positions) == len(phoneme_ids) == len(durations)`.

`synthesize.py` keeps:
- timing extraction (`start_frames`, `end_frames`, `note_durations`)
- articulation-gap post-processing (`_apply_articulation_gaps`)
- duration/pitch/variance model calls
- slur velocity envelope (`_build_slur_velocity_envelope`)

### 4.2 Responsibility split
Moved into `syllable_alignment.py`:
- note grouping (replaces `_group_notes` internally)
- split-word reconstruction (`begin/middle/end`)
- syllable construction and note assignment
- slur/tie decision for phoneme distribution
- consonant-cluster partition
- rest/SP group emission in contract output
- contract invariant validation function

Left in `synthesize.py`:
- frame/timing conversion and model orchestration
- articulation-gap insertion
- audio synthesis call chain

V2 dead-code cleanup candidates (phase 3/4):
- `_group_notes`
- `_resolve_group_lyric`
- `_split_phonemize_result`
- `_split_phonemes_into_syllable_chunks`
- any V1-only branches inside `_build_phoneme_groups`

## 5. DiffSinger Contract (Source of Truth)
The alignment output must satisfy:
- `phoneme_ids`: flattened phoneme token sequence.
- `language_ids`: same length as `phoneme_ids`.
- `durations`: frame counts, same length as `phoneme_ids`.
- `word_boundaries`: per-word phoneme counts.
- `note_durations`: frame durations for notes.
- `note_slur`: one entry per note (`1` for extension/slur note, else `0`).
- `note_phonemes`: note index to phoneme list mapping for validation/debug.

Invariant checks:
- sum(`durations`) equals sum(`note_durations`) after any articulation-gap adjustments.
- no empty phoneme group for sung notes.
- extension notes never create an extra lexical word boundary.

## 6. SyllableBased Strategy to Adopt
Borrow these concepts from OpenUtau:
- identify continuation notes early (`+`, `+~`, `+*`, tie continuation)
- derive symbols then detect vowel anchors
- build syllable spans between vowel anchors
- distribute syllables to syllabic carriers (`single`, `begin`, `middle`, `end`)
- keep continuation notes as sustain carriers, not new lexical starts

Implementation model:
- `SyllableUnit`
  - `prev_vowel`
  - `consonant_cluster`
  - `vowel_core`
  - `start_note_index`
  - `container_frames`
- `NoteGroup`
  - `lyric_note`
  - `continuation_note_indices`
  - `syllabic_note_indices`
  - `is_rest`

### 6.1 Note type classification
This resolves the ambiguity in current behavior:
- `syllabic carrier note`: note with lyric syllable content (`single`, `begin`, `middle`, `end`).
- `continuation note`: sustain extension (`+`, explicit tie continuation, explicit `lyric_is_extended`).

Rule:
- `middle/end` are not treated as sustain continuation; they are syllable carriers and must receive phonemes.
- Only continuation notes are marked `note_slur=1`.

### 6.2 Existing `distribute_slur()` integration
The new module should delegate to `phonemizer.distribute_slur()` when all are true:
- group is a real slur/melisma (not pure tie sustain),
- phonemizer provides a distribution,
- output length matches target note count.

Fallback:
- if unavailable or invalid, use syllable-unit distribution.

### 6.3 Consonant cluster partition rule
Use vowel-anchor span model (OpenUtau style):
- between consecutive vowel anchors, consonants are attached to the next syllable onset by default.
- if language plugin provides stronger rule (via phonemizer hook in future), allow override.
- no cross-word boundary merge unless explicitly marked by continuation semantics.

## 7. Handling Rules
### 7.1 Split words
- Reconstruct lexical token from syllabic chain when MusicXML indicates `begin/middle/end`.
- Run G2P on reconstructed lexical token.
- Distribute phoneme symbols by syllable boundaries, not by equal slicing.
- This preserves current lexical-G2P behavior; it is not a new lexical policy.

### 7.2 Extension notes
- If note is extension/slur: it inherits continuation semantics from previous lyrical anchor.
- Do not open a new lexical word boundary on extension notes.
- For pure sustain ties, avoid consonant re-attack.

### 7.3 Single-note multi-syllable fallback
- If only one non-extension note exists for a multi-syllable token:
  - keep current trimming fallback in phase 1
  - mark warning signal for post-validation.

## 8. Integration Changes
In `src/api/synthesize.py`:
- keep timing and DS inference orchestration.
- replace `_build_phoneme_groups` path with `syllable_alignment.align(...)`.
- retain existing slur velocity envelope and coda-tail logic initially.
- gate new aligner behind env flag:
  - `SYLLABLE_ALIGNER_V2=true`
- keep `_apply_articulation_gaps` downstream unchanged.

### 8.1 `container_frames` ownership
`container_frames` is set inside alignment from provided note-frame boundaries.
Therefore `align(...)` input includes:
- grouped notes
- `start_frames`
- `end_frames`
- timing-midi list
This keeps syllable assignment and container sizing in one place.

## 9. Validation
### 9.1 Pre-execution
- Unit tests for syllable mapping edge cases.
- Contract invariants on alignment output.
- New helper: `validate_ds_contract(payload) -> List[str]`.

`validate_ds_contract` minimum checks:
- length equality:
  - `len(phoneme_ids) == len(language_ids) == len(durations) == len(positions)`
- minimum duration:
  - every entry in `durations` must be `>= 1`
- boundary sums:
  - `sum(word_boundaries) == len(phoneme_ids)`
- note-slur length:
  - `len(note_slur) == len(note_durations)`
- total frame conservation:
  - `sum(durations) == sum(note_durations)` before articulation-gap insertion
  - and equality preserved after downstream adjustments
- optional debug coherence:
  - flattened `note_phonemes` token count equals `len(phoneme_ids)` for non-rest spans

### 9.2 Post-execution
- Existing lyric coverage checks remain.
- Add per-measure split-word sanity check:
  - if source has lexical words and derived output is mostly `+`, flag warning.

## 10. Test Plan
### 10.1 Unit
- `tests/test_syllable_alignment_contract.py`
- `tests/test_syllable_alignment_split_words.py`
- `tests/test_syllable_alignment_extensions.py`
- `tests/test_syllable_alignment_tie_vs_slur.py`

Cases:
- `voic-es` over 2 notes
- `grat-i-tude` over 3 notes
- `mil-lion` over 2 notes
- tie continuation and mixed `+` markers
- tie sustain vs pitched slur decision
- rest boundaries and phrase reset

### 10.2 Integration
- `tests/test_syllabic_grouping.py`
- `tests/test_syllabic_phoneme_distribution.py`
- `tests/test_voice_parts_e2e.py -k my_tribute`

### 10.3 Audio spot checks
- Amazing Grace female line split syllables.
- My Tribute women part and men part sections with split words.

## 11. Rollout Plan
Phase 1:
- Add new module and parity tests.
- Keep old path as fallback.

Phase 2:
- Enable `SYLLABLE_ALIGNER_V2` in local/testing.
- Compare outputs against baseline artifacts.

Phase 3:
- Default to V2.
- Keep fallback for one release cycle.

Phase 4:
- Remove deprecated chunk-splitting code path.

## 12. Risks and Mitigations
- Risk: language-specific G2P edge cases.
  - Mitigation: keep language map pluggable and preserve old fallback.
- Risk: duration mismatch after remapping.
  - Mitigation: strict invariants + duration normalization pass.
- Risk: regressions in songs with heavy melisma.
  - Mitigation: targeted melisma fixtures and post-validation warnings.

## 13. Acceptance Criteria
- Split-word syllables no longer drift beats in target fixtures.
- No regression in note/phoneme count contract for existing tests.
- My Tribute and Amazing Grace checks pass with V2 enabled.
- Derived audio timing for split words is musically aligned to note durations.

## 14. Review Answers (Resolved)
1. Return type/API: `align(...)` returns flattened DS-contract arrays plus debug fields.
2. `_group_notes`: replaced by module-internal grouping; `synthesize.py` no longer owns grouping logic.
3. `distribute_slur()`: used as first-choice slur distributor when valid; fallback to syllable strategy.
4. Cluster partition: vowel-anchor span model, default onset attachment to next syllable, with future hook for language overrides.
5. `container_frames`: computed inside aligner from `start_frames/end_frames` inputs.
6. Rest/SP handling: handled in aligner output.
7. `middle/end` classification: they are syllable carriers (not sustain extensions).
