# SIG-6: Split Multiple Parts in One Clef

## Linear Reference
- Issue: `SIG-6`
- Title: `Split multiple parts in one clef`
- URL: https://linear.app/sightsinger/issue/SIG-6/split-multiple-parts-in-one-clef

## Validated Problem (Using Reference Score)
Reference score analyzed: `assets/test_data/amazing-grace-satb-verse1.xml`.

Observed structure:
- Score has 2 printed parts: `P1 = SOPRANO ALTO`, `P2 = TENOR BASS`.
- Each printed part contains two independent voices (`<voice>1</voice>`, `<voice>2</voice>`) active across all 19 measures.
- In `P1`, lyrics are attached only to voice `1` notes (28 lyric-bearing notes), while voice `2` has no lyric tokens.
- In `P2`, no lyric tokens are present (all 67 notes lyricless).

Current pipeline behavior that creates the bug:
- `parse_score(...)` returns one selected part by default, not split SATB voice parts.
- `synthesize(..., voice_id=None)` currently prefers voice `"1"` when present (`_select_voice_notes`), so secondary voices are ignored unless explicitly requested.
- Because lyrics are only present on one voice in compact notation, non-lyric voices lose text needed for phonemization.

Refined problem statement:
- This is not only a "single clef chord" problem; it is a "compact multi-voice engraving" problem where musical voices and lyric ownership are encoded asymmetrically.
- We need deterministic voice part extraction and lyric propagation so each target singing voice part has both notes and singable lyric tokens.

## Goals
- Support compact SATB/SAB/TB-style MusicXML where multiple singing voice parts share one printed part.
- Produce per-voice part note streams suitable for synthesis (`soprano`, `alto`, `tenor`, `bass` or generic voice part IDs).
- Ensure each synthesized voice part has lyric continuity even if source lyrics appear only on one sibling voice.

## Non-Goals
- Full notation re-engraving or visual score editing.
- Polyphonic singing by a single synthesized voice in one pass.
- Perfectly inferring intended choral naming for all ambiguous files without user override.

## High-Level Solution Plan

### Phase 1: Score Analysis + Voice Part Detection
- Add a score-shape analyzer to detect compact multi-voice parts.
- Detection signal:
  - part has multiple voice IDs with overlapping offsets, and
  - lyric concentration is heavily skewed toward one voice.
- Output per part:
  - `voice_ids`, `avg_pitch_by_voice`, `lyric_coverage_by_voice`, `overlap_ratio`, `candidate_voice_part_count`.
  - `missing_lyric_voice_parts`, `lyric_source_candidates`.

### Phase 2: Deterministic Voice Part Split
- Introduce a voice part-splitting transform before synthesis alignment.
- Default split strategy:
  - within each printed part, map each voice ID to a voice part.
  - stable ordering by average pitch (high to low) for named mapping (`soprano/alto` and `tenor/bass`).
- Preserve timing/rest/tie/slur metadata while splitting.
- For nonstandard names, use fallback labels: `voice part 1`, `voice part 2`, etc.

### Phase 3: Lyric Propagation Rules
- Add lyric propagation from a lyric-rich source voice to sibling lyric-poor voice(s) in the same printed part.
- Propagation rules (high level):
  - align by note onset grid and extension/tie chains,
  - copy lexical tokens at syllable starts,
  - copy continuation markers (`+` / extension state) across sustained notes,
  - never overwrite explicit lyric tokens already present.
- Emit trace metadata for debugging: `lyric_source_voice`, `propagated_token_count`, `conflicts`.

### Phase 4: API and Selection Surface
- Extend parse/synthesis selection model to include explicit voice part identity:
  - `part_index` + `voice_part_id` (preferred),
  - keep `voice_id` as backward-compatible alias.
- `parse_score` should return only signals for LLM/user decisioning:
  - compact multi-voice part detected or not
  - missing-lyric voice parts for the requested target
  - available lyric source voice parts
- LLM/user interaction decides whether to proceed with split/propagation.
- Voice part split and lyric propagation are preprocessing steps before existing `synthesize`.
- Preprocessing output should be persisted as transformed score data for reuse.

### Phase 5: Verification and Guardrails
- Add parser and synthesis tests using `amazing-grace-satb-verse1.xml`:
  - voice part detection returns 4 voice parts across 2 printed parts,
  - soprano and alto voice parts each have singable lyric streams,
  - tenor/bass behavior is explicit (either propagated or reported depending on policy),
  - no regression on existing single-voice scores.
- Add debug logging around split/propagation decisions for production triage.

## Acceptance Criteria
- Given compact SATB XML, caller can synthesize `soprano`, `alto`, `tenor`, `bass` voice parts independently without manual XML editing.
- Voice Part note counts and offsets remain monotonic and musically consistent with source timing.
- Lyric propagation produces phonemizable text for lyric-poor voice parts without corrupting voice parts with explicit lyrics.
- Existing single-voice and already-separated-part flows remain backward compatible.

## Resolved Decisions
- Assist mode only for lyric replication:
  - require explicit user/LLM confirmation before propagation.
  - require explicit user selection of source voice part for now.
  - future enhancement: auto-default source voice part by highest note synchronization rate with target voice part.
- Nonstandard voice part naming:
  - fallback labels are `voice part 1`, `voice part 2`, etc.
- `parse_score` behavior:
  - return only multi-voice/missing-lyric signals and source options.
  - do not auto-split or auto-propagate at parse time.
- Split and propagation execution:
  - execute as pre-steps before current synthesis pipeline.
  - persist transformed score for reuse.
- Cost-aware prompting:
  - only request split/propagation when needed for the user-requested target voice part(s).
  - do not prompt for unrelated combined parts.
  - if requested target already has lyrics and no split dependency, proceed directly without prompt.

## Implementation Notes (Pragmatic)
- Prefer implementing split/propagation in parser/normalization layer, not in phonemizer, to keep phonemizer language-focused.
- Keep the slur/tie logic in `synthesize.py` voice part-agnostic by feeding it clean monophonic voice part streams.
- Ship behind a feature flag first, then make default once regression tests are stable.
