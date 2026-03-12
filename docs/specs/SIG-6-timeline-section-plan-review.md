# Review: SIG-6 Timeline-Section Plan Specs

## Overall Assessment

Both documents are **well-structured and technically sound**. The core motivation — replacing the global-first `base source + section_overrides` model with an explicit, linear timeline — is a clear improvement for auditability and LLM output quality. The documents are internally consistent, and the low-level design aligns well with the implementation blueprint.

---

## ✅ Strengths

1. **Reuse-first approach is pragmatic** — mapping `rest` → clear/keep-rest, `derive` melody → existing duplicate/copy helpers, `derive` lyric → `_propagate_lyrics()` with range bounds avoids new primitives.
2. **Contiguity constraint from `1..max_measure` is correctly strict** — since derived parts are written back as full MusicXML parts, they must align measure-for-measure with all other existing parts. Requiring explicit `rest` sections for non-singing intro/outro measures guarantees the derived part has complete measure coverage and produces valid MusicXML output.
3. **Clean dispatch design** — detecting `sections` vs `actions` in `parse_voice_part_plan()` is backward-compatible and trivial to implement.
4. **Rollout is staged and safe** — keeping legacy `actions` plans fully supported while landing the new path behind a feature flag is the right call.
5. **Diagnostics plan is forward-looking** — `section_results[]` with per-section note/lyric counts is a big debugging upgrade over the current opaque output.

---

## 🟡 Clarifications Needed

### 1. `rest` section — what does "clear/keep-rest in range" mean exactly?

> Implementation §5 line 61: `rest: clear/keep-rest in range`

Two interpretations:
- **Option A**: If the target already has sung notes in the rest range, *remove* them (make the range silent).
- **Option B**: Assert the target *already is* silent in that range; error if not.

The low-level design (§5.3) says "ensure target has no sung notes in range (alignment preserved with rests)." This implies **Option A** (actively removing notes). If so, this is a **destructive operation** on the derived part — worth calling out explicitly that notes in rest ranges get stripped.

> [!IMPORTANT]
> Recommend adding a sentence like: "If the target part has sung notes within a `rest` section range, those notes are removed from the derived output. Only rest events are retained."

### 2. `derive` with only `melody_source` — what happens to lyrics?

The contract says `derive` requires "at least one of `melody_source` or `lyric_source`." But what's the intended behavior when only `melody_source` is specified?

- Copy melody notes from the source but leave lyrics empty/as-is on the target?
- Copy melody notes AND carry over any lyrics attached to the source notes?

### 3. Melody copy — which existing helper maps to this?

Implementation §5.2 says "reuse existing duplicate/copy helpers, but invoked per section." Currently:
- `_duplicate_section_to_all_voice_parts()` copies source notes to *all other* voice IDs within the same part
- `_select_part_notes_for_voice()` filters/extracts notes for one voice

Neither is a direct "copy notes from source part/voice to target part/voice within a measure range." Is the plan to build a thin adapter, or is there an existing helper I missed?

### 4. Stitch step — tie/slur/melisma at section boundaries

Since sections are contiguous and non-overlapping, concatenation is straightforward. But:
- What about **tie/slur events** that cross a section boundary? E.g., a tie starts in a `derive` section and ends in a `rest` section. Does the stitch step handle tie truncation?
- What about **melisma extension tokens** (`+`) that span a section boundary?

### 5. Multi-target execution order

`_execute_preprocess_plan()` currently only processes `targets[0]`. The implementation blueprint says "Per target" — is multi-target support part of this change, or still limited to `targets[0]`?

---

## 🔴 Issues to Address

### 1. Missing: what `max_measure` is derived from

Implementation §4 line 38 says "contiguous from `1..max_measure` (where `max_measure` is derived from target part span)." But the low-level design doesn't specify whether `max_measure` comes from:
- `voice_part_measure_spans` (the span of the specific target voice part)
- The total measure count of the entire part (required for valid MusicXML output)

Since the derived part must align with all parts in the MusicXML, `max_measure` should likely be the total measure count of the score (or at minimum the target part), not just the voice part's span.

### 2. `split_voice_part` action is absent from the timeline contract

The legacy plan has an explicit `split_voice_part` action type. The new timeline contract only has `sections` with `rest`/`derive` modes. But splitting is *prerequisite* to derivation — you need to isolate the target voice's notes before you can operate section-by-section.

Is the assumption that splitting is **implicit** when a timeline plan is used? If so, worth documenting: "Timeline execution always performs an implicit split of the target voice part before iterating sections."

### 3. No `verse_number` / `copy_all_verses` at target or section level

The current `propagate_lyrics` action carries `verse_number`. The new `sections` contract omits this. Where does verse info live in the new schema — global on the target, or per-section?

---

## 📝 Minor Suggestions

| Item | Suggestion |
|------|-----------|
| **Section diagnostics** | Consider adding `section_mode` to `section_results[]` so logs clearly show which sections were rest vs derive |
| **Feature flag** | Low-level design §10 mentions a feature flag. Implementation blueprint §8 doesn't. Align these. |
| **Error code naming** | `invalid_section_source` is ambiguous — consider splitting into `malformed_section_source` vs `empty_section_source`. |
| **Test gap** | Neither spec mentions testing tie/melisma handling at section boundaries. |
| **`measure_annotations`** | Low-level §4 lists this as a parser fact, but it doesn't appear in current `analyze_score_voice_parts()` output. Clarify if this is new or existing. |

---

## Summary Questions

1. What is the exact behavior of `rest` sections when the target has existing sung notes in that range?
2. What does `derive` with only `melody_source` (no `lyric_source`) produce?
3. Is `split_voice_part` implicitly performed, or does it need an explicit action alongside `sections`?
4. Where does `verse_number` live in the new timeline contract?
5. Is multi-target support (`targets[1+]`) in scope for this change?
6. How should tie/slur/melisma events be handled at section boundaries?
