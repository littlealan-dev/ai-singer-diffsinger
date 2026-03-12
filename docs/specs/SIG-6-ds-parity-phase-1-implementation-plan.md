# SIG-6 Implementation Plan: Phase 1 Parity Fixes

This plan details the technical steps to achieve OpenUtau/DiffSinger parity for V2 timing, specifically addressing prefix consonant grouping, segment-ratio alignment, and proper slur/retake conditioning.

## Goal
Achieve bit-exact (or perceptually identical) timing parity with OpenUtau DiffSinger by fixing the "note-centric" grouping flaw and adopting the segment-ratio alignment strategy.

## Proposed Changes

### [Component] V2 Aligner Contract & Grouping
#### [MODIFY] [syllable_alignment.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/api/syllable_alignment.py)
- **Prefix Shift**: Modify `align()` to shift onset consonants (all phonemes before the first vowel/glide) into the *preceding* anchor window.
- **Initial Silence**: Ensure sentence-initial consonants are shifted into the initial `SP` padding window instead of the first note.
- **Deduplication**: Move `_group_notes`, `_resolve_group_lyric`, and `_split_phonemize_result` to a shared utility or consolidate in `syllable_alignment.py`.

### [Component] Synthesis Timing Flow
#### [MODIFY] [synthesize.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/api/synthesize.py)
- **Segment-Ratio Alignment**: Replace the proportional rescale in `_rescale_group_durations` with the segment-ratio alignment algorithm.
- **Stretch Partitioning**: Implement the logic where consonants are constrained to a narrower stretch range than vowels.
- **Contract Cleanup**: Remove legacy `_build_phoneme_groups` code path once V2 is stabilized.

### [Component] Inference & Model Inputs
#### [MODIFY] [inference.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/api/inference.py)
- **Slur Conditioning**: Update `predict_pitch` and `predict_variance` to accept a `slur` mask derived from the contract.
- **Dynamic Retake**: Plumb the `retake` mask to the models (where slurred transitions have `retake=0` to prevent re-attack artifacts).
- **AP Support**: Allow `AP` (aspiration) silence type for breaths to trigger correct model response.

## Verification Plan

### Automated Tests
- `pytest tests/test_syllabic_grouping.py`: New unit tests specifically for prefix shift correctness.
- `pytest tests/test_syllabic_phoneme_distribution.py`: New unit tests for segment-ratio math vs. proportional rescale.
- `pytest tests/test_voice_parts_e2e.py`: Verify no regressions in "My Tribute" Female Part 1.

### Manual Verification
- **A/B Listening**: Verify "k" in "key" or "st" in "star" timing on long notes.
- **Duration Audit**: Inspect `durations_debug` to ensure onset consonants do not grow proportionally with note length.
