# Repeat-aware verse planning and SATB lyric preparation

Status: Draft for review; implementation is not authorized by this document alone.

Date: 2026-09-09

Workspace: `ai-singer-diffsinger-integration`

## 1. Objective and accepted decisions

Support singing different lyric verses on different passes through repeated music, including first/second endings and D.C./D.S./Fine/Coda navigation. Support the same behaviour after extracting SATB voices from shared staves.

The user chooses Automatic verses or a fixed lyric line, independently of With Repeats. In Automatic mode, the LLM authors a verse plan using whole-piece structure and exact score evidence. Deterministic code validates and executes the plan. For a fixed verse or fixed Solfege selection, code constructs the direct plan because the user has already made the choice.

Authoritative plan references are original source-measure ranges and program-generated playback traversal IDs. Section labels are descriptive only. The LLM assigns lyrics to an existing playback order; it never generates a competing musical traversal or timestamps.

Requirements:

- Reuse music21 for supported repeat/navigation expansion.
- Keep original notation available for OSMD preview and preserve original/expanded parsed representations.
- Preserve all relevant lyric alternatives through parsing and SATB preprocessing.
- Use one shared musical playback timeline for vocals, MIDI, highlighting, and duration estimates.
- Resolve required interpretation/alignment issues before charging or starting synthesis.
- Do not implement musical interpretation through invented keyword lists, fuzzy matches, or regex heuristics. Exact schema facts and explicit user decisions are deterministic inputs; interpretation belongs to the LLM/user.
- Do not increase repeat counts merely because more lyric verses exist.
- Do not introduce a note-event traversal during browser playback for verse selection.

## 2. Findings in the current implementation

### 2.1 Library capabilities

Installed music21 is 9.9.1. Its `expandRepeats()` expands the supported navigation forms but retains multiple lyric lines on copied notes. An in-memory experiment adding a second line to the existing repeat fixtures confirmed that it does not select a stanza for each pass.

MusicXML `<lyric time-only="1,2">` explicitly restricts a lyric to specified passes through a repeated section. The installed `xmlToLyric` importer does not preserve this attribute. Retain it from raw MusicXML before any library round-trip. MusicXML lyric `number` identifies a line and is not inherently a repeat-pass number; `name` can describe a lyric type.

`repeat.Expander.measureMap()` provides source mapping but invokes expansion again internally. Prefer provenance from the expansion already produced by the application, avoiding another expansion solely for verse planning.

References:

- [music21 repeat API](https://music21.org/music21docs/moduleReference/moduleRepeat.html)
- [MusicXML lyric attributes](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/lyric/)
- [MusicXML sound/navigation attributes](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/sound/)
- [MusicXML repeat attributes](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/repeat/)

### 2.2 Application constraints

- `src/musicxml/parser.py`: `_extract_lyric_text` selects one lyric line for each parsed note. `_raw_lyric_selections` lists alternatives but does not preserve a complete alternative alignment per note. `build_performance_measure_map` supplies written/expanded measure occurrences.
- `src/api/score.py`: prepares original and expanded score variants and attaches duration and performance-map metadata.
- `src/api/voice_parts.py`: extracts derived voices and propagates/validates selected lyrics. Success for one selected verse does not establish readiness of other verses.
- `src/backend/orchestrator.py`: `_build_verse_change_requires_repreprocess_action` implements a single-verse preparation guard. This must become a per-lyric preparation check for the new representation.
- `src/mcp/tools.py`: synthesis currently requires one `lyric_selection`. Automatic planning requires an additional validated contract.
- `ui/src/MainApp.tsx`: exposes fixed verse selection and With Repeats; master playback consumes completed vocal artifacts.

## 3. Scope and invariants

In scope: plan authoring, validation, compilation, SATB lyric preservation/alignment, storage, prompts, synthesis preflight, UI selection/summary, cache identity, and regression coverage.

Not introduced by this work: arbitrary music notation support beyond the expansion engine, invented lyrics, an alternate MIDI traversal engine, a full graphical verse-plan editor, or automatic note alignment of free-standing stanza text without a separate validated preparation operation.

Invariants:

1. A compiled plan only selects lyrics or explicit vocal silence; it cannot change measure order, tempo, or playback duration.
2. Source indexing remains anchored to the original uploaded notation, even when derived parts are appended or hidden.
3. Every required sung note occurrence is covered by a valid lyric onset/continuation or an explicit permitted silence decision. Genuine rests need no lyric assignment.
4. Explicit score restrictions cannot be overridden merely because an LLM proposes an alternative.
5. Derived voices share playback occurrences but have independent lyric availability and alignment.
6. Preview, upload data, and prepared lyric alternatives remain immutable during rendering.
7. A draft or stale plan cannot authorize billing or GPU work.

## 4. User controls and compatibility

| Control | Behaviour |
| --- | --- |
| Automatic verses | LLM creates or reuses a validated plan for the requested target and current playback order. |
| Fixed verse | Code creates a plan selecting that lyric line throughout the requested order; documented shared passages may be used only through validated relationships. |
| Fixed Solfege | Uses the selected solfege line throughout; never advances into original lyrics. |
| With Repeats enabled | Uses music21's expanded performance order. |
| With Repeats disabled | Uses current written-order semantics, including written endings once; creates no additional passes. |

Default to Automatic verses for new UI sessions. Retain legacy API semantics: an existing request containing only `lyric_selection` remains a fixed selection. Do not reinterpret stored fixed-verse renders or requests as Automatic.

Changing With Repeats selects a different playback-map identity and invalidates that map's compiled plan/quote. It does not reparse the upload. Existing accepted semantic decisions may be reused after validation against the other map. Changing verse mode does not re-run structural extraction when the required lyric alignments already exist.

If a fixed selection conflicts with explicit `time-only`, return a decision-required result. An override must reference explicit user authorization; selecting Fixed does not silently waive score restrictions.

## 5. Source and lyric data model

Names below are proposed additions, not existing production fields.

### 5.1 Stable source identities

During the existing raw XML scan, retain:

- Source revision/fingerprint and canonical source-measure index (zero-based).
- Original printed measure number/suffix for display only.
- Raw part ID, staff, voice, source XML note ordinal, and chord-member identity where applicable.
- Stable source-note IDs independent of pitches, timestamps, or renumbered expanded measures.
- Structural repeat markers, endings, jump targets, and their positions within measures.

Use source XML IDs when valid and unique; otherwise generate revision-scoped IDs from structural addresses. A reupload establishes a new revision. When multiple part measure sequences cannot be aligned to a common source grid unambiguously, report the conflict rather than zip parts by index silently.

### 5.2 Lyric alternatives

Each source note references zero or more lyric events. Retain raw line identity (`number`, `name`, stable line ID), text components/elisions, syllabic state, extension/tie evidence, `time-only`, and provenance. Preserve raw nonnumeric identifiers instead of deriving stanza meaning from their spelling.

A separate lyric inventory records line coverage, samples, explicit language metadata if present, generated-solfege provenance, and evidence references. Numeric ordering may describe line order but must not prove that lines are successive stanzas rather than translations.

The existing selected `lyric` fields may remain as a legacy projection. They are not the authoritative store for Automatic mode.

### 5.3 Derived voice representation

Separate the extracted note skeleton from lyric-specific alignments:

```text
derived lane
  source revision + structural extraction revision
  note skeleton and source-note references
  lyric alignments
    lyric line ID -> aligned syllable/continuation events + validation status
  propagation decisions and source lyric references
```

Each alignment records whether text was copied directly or propagated from another voice. A single surviving derived lyric line is not proof that the original passage had shared lyrics. Track `shared_source_passage`, `not_prepared`, `missing_in_source`, `explicit_pass_restriction`, and `alignment_unresolved` separately.

Lyric-specific splitting of a tied duration, if required by existing supported preparation, must preserve total duration, pitch continuity, and source provenance. It belongs to that alignment, not to a destructive rewrite of other lyric alternatives.

## 6. Playback occurrence and traversal contract

### 6.1 Authoritative occurrences

Extend/reuse the measure expansion mapping. Each entry has an opaque occurrence ID, source-measure index, played index, musical start/end, and repeat/navigation context. Original measure indices, not printed numbers or music21's expanded numbering, identify notation.

Record a repeat-context stack where structural facts establish it: repeat identity, local iteration, associated ending, and D.C./D.S. return context. Different parts resting at different times do not have independent passage counters.

Do not equate a source measure's visit count with its section pass. A second ending can be visited once while belonging to pass 2.

### 6.2 Program-generated traversals

A traversal is a revision-scoped ID with an explicit ordered list of occurrence IDs. It denotes a contiguous forward run within a stable navigation/ending context. Generate boundaries at backward returns, skipped source measures, ending transitions, and relevant context changes. A second ending may have its own traversal ID linked to its parent repeat's second pass.

For nested repeats retain both inner and outer contexts. Which repetition changes the words is an LLM/user assignment, not an implicit global counter. Traversal IDs do not encode musical section names.

Never infer navigation solely from a decreasing printed measure number. Use original indices, expansion provenance, and retained repeat facts. If exact context cannot be established, occurrence IDs can still identify the actual produced route, but any unresolved `time-only` scope must be escalated.

### 6.3 Range addressing

An assignment targets the intersection of:

1. An inclusive original source-measure range.
2. Occurrences belonging to selected traversal IDs, or every occurrence for `kind: all`.

The assignment cannot reorder occurrences. Reject a selector with no matches. The planner receives each traversal's actual occurrence list and source coverage, including skipped endings.

Optional submeasure boundaries use exact quarter-note offsets relative to the source measure. Use rational strings, such as `"3/2"`, rather than floating-point approximate addresses. Start is inclusive; an explicit end offset is exclusive. Omitted offsets cover the complete first/last measure. Reject boundaries cutting through a sung syllable or sustained note unless a prepared alignment explicitly supports that split.

## 7. Verse-plan schema

Example: measures 1–8 repeat twice, followed by a shared chorus. Traversal IDs shown are placeholders for IDs provided by the backend.

```json
{
  "schema_version": 1,
  "score_revision": "rev-7",
  "playback_map_id": "expanded-map-7",
  "expand_repeats": true,
  "target_lane_id": "soprano-derived",
  "target_preparation_revision": "prep-3",
  "mode": "automatic",
  "assignments": [
    {
      "assignment_id": "a1",
      "section_label": "Opening verse",
      "source_measure_range": {"start_index": 0, "end_index": 7},
      "traversals": {"kind": "selected", "ids": ["traversal-1"]},
      "action": {"kind": "sing", "lyric_id": "soprano-verse-1"},
      "evidence_refs": ["decision-stanza-sequence-1"]
    },
    {
      "assignment_id": "a2",
      "section_label": "Opening verse repeated",
      "source_measure_range": {"start_index": 0, "end_index": 7},
      "traversals": {"kind": "selected", "ids": ["traversal-2"]},
      "action": {"kind": "sing", "lyric_id": "soprano-verse-2"},
      "evidence_refs": ["decision-stanza-sequence-1"]
    },
    {
      "assignment_id": "a3",
      "section_label": "Shared chorus",
      "source_measure_range": {"start_index": 8, "end_index": 15},
      "traversals": {"kind": "all"},
      "action": {"kind": "sing", "lyric_id": "soprano-chorus"},
      "evidence_refs": ["shared-chorus-coverage"]
    }
  ]
}
```

`section_label` has no execution meaning. If retained as `section_id` for compatibility, it remains descriptive and cannot replace range/traversal selectors.

Alternative action: `{"kind": "vocal_silence", "decision_ref": "user-decision-4"}`. Silence must be intentional and supported; it cannot be a generic repair for missing words. Pass-excluded lyrics can yield silence when explicit source restrictions establish that result.

Reject unknown fields, invalid ranges, invalid action unions, conflicting/overlapping matched spans, and arbitrary paths or code. In V1, reject overlapping assignments even when they select the same line; there is no last-rule-wins precedence. Adjacent disjoint submeasure spans are allowed.

The server, not the LLM, assigns persisted `plan_id`, canonical hash, validation status, and authorization metadata. Evidence references must resolve to retained source facts, explicit user decisions, or identified interpretive decisions. LLM confidence alone is insufficient to authorize an unsupported choice.

## 8. Verse semantics and exceptions

The LLM receives these guidelines; the executor only follows validated assignments.

| Situation | Required handling |
| --- | --- |
| Clear two-stanza repeated passage | Plan V1 on the first traversal and V2 on the second. |
| First/second endings | Assign using enclosing repeat context. A first visit to the second ending does not imply V1. |
| Passage has one unrestricted shared line | Reuse that complete passage's words on applicable traversals. |
| Isolated missing V2 syllable | Check V2 continuation/tie alignment; otherwise flag a gap. Never select V1 note by note to fill holes. |
| Verse 2 exists in source but was not propagated to Alto | Prepare Alto's V2 alignment; do not classify it as a shared one-verse passage. |
| `time-only` assignments | Enforce against the established repeated-section context. Ambiguous nested scope requires a decision, not a guessed interpretation. |
| D.C./D.S. return | May advance or repeat words depending on piece evidence. The navigation symbol alone does not prescribe next verse. |
| Fine | Ends the route where music21 places it; never creates another verse. |
| To Coda/Coda | Jump does not itself increment a stanza. Assign coda words explicitly, especially when several lines exist. |
| Three traversals, two stanzas | Resolve the third assignment with evidence/user input; no automatic cycle/clamp. |
| Three stanzas, two traversals | Leave one unused and disclose it; do not lengthen the route. |
| Independent repeated passages | Make separate assignments; no global verse counter. |
| Nested repeats or repeats within D.S. returns | Use exact occurrences/context, with explicit assignments for all required passes. |
| Translation/alternative lyric lines | Keep lyric families separate unless user explicitly requests alternation. |
| Fixed/generated Solfege | Stay in its chosen family, with per-voice alignment. |
| Stanza text printed away from notes | Mark unaligned; requires a separate validated alignment operation or user input. |
| Unsupported/malformed navigation | Existing expansion failure remains explicit; planning cannot invent a route. |

For unlabelled evidence that cannot distinguish a shared phrase from missing lyrics, return an ambiguity rather than declaring sharing from single-note availability. Numeric line ordering and a single lyric line are facts; intended stanza progression/sharing can still require interpretation.

## 9. End-to-end workflow and LLM involvement

### 9.1 Upload/reparse

Parse all alternatives and raw restrictions during the existing source scan. Prepare written/expanded variants and navigation indexes. Publish a bounded whole-piece summary, lyric availability, shared-staff signals, and relevant text evidence. Do not generate a verse plan for every possible voice on upload.

### 9.2 Requested performance

1. Resolve requested voice, verse mode, language/family, and With Repeats.
2. Prepare the target note skeleton if needed, against written notation.
3. Preserve/align relevant lyric alternatives; retain results independently per lyric line.
4. For Automatic, reuse a current validated plan or request an LLM-authored candidate. For fixed selection, construct the candidate in code.
5. Validate references, coverage, alignment readiness, and restrictions.
6. If validation discovers additional required lyric preparation, prepare only affected spans/lines and revalidate. Preserve successful extraction and other alignments.
7. Store the validated plan and compile lyric bindings.
8. Produce the existing render quote including verse behaviour; obtain normal user confirmation.
9. Final preflight rechecks revision/hash/selection, then starts the existing billing and synthesis lifecycle.

There is a bounded feedback loop between lyric preparation and plan validation: the LLM may identify a line not yet aligned. Finalization always follows successful preparation. Do not rerun structural SATB extraction solely because another lyric line is requested.

### 9.3 Planner context

Supply whole-piece structure compactly, all traversal descriptions, exact raw directions/evidence IDs, target lyric inventory and coverage, relevant SATB provenance, explicit user intent, and accepted piece-level decisions. Keep full note data in the deterministic layer. Fetch detailed evidence for specific unresolved passages when necessary rather than silently truncating context.

The planner returns either a complete candidate or structured questions/preparation requests. It cannot mark its own plan validated or charge credits. Batch unresolved issues in a single planner turn where practical; do not make one LLM request per note, measure, or verse.

For later SATB renders, reuse accepted piece-level decisions and map them to each voice's validated lyric relationships. Lyric IDs remain per voice; equal numbers across voices do not prove shared text. A later user-requested semantic change does not silently regenerate earlier voices: report if existing takes now use a different verse interpretation.

### 9.4 Validation feedback and escalation

Return issues with stable IDs, target lane, source span, matched occurrences, failed rule, available options, and evidence references. Example codes: `stale_verse_plan`, `unknown_traversal`, `assignment_overlap`, `uncovered_vocal_span`, `lyric_alignment_required`, `time_only_conflict`, `ambiguous_lyric_family`, and `musical_intent_unresolved`.

Allow one initial candidate plus at most two correction attempts per unchanged planning input. Stop earlier when normalized plan/issue hashes show no progress. Treat this as a configurable workflow budget, not a loop until success. Preserve valid assignments and repair affected spans. Genuine intent questions return to the user immediately rather than exhausting retries.

A new user decision or completed targeted preparation creates new planning inputs. Cancellation stops planning work and prevents a late result from activating a stale plan. Source changes during asynchronous work discard activation of outdated candidates.

## 10. Deterministic validation and compilation

Validation stages:

1. Schema and bounds: exact types, maximum payload sizes, rational offsets, valid IDs.
2. Freshness: source revision, map identity, target and preparation revision, lyric inventory, user overrides.
3. Occurrence matching: expand selectors into the already established musical route.
4. Coverage: no overlapping assignments and no unresolved required vocal spans; rests are exempt.
5. Lyric validity: requested lines belong to the target or a validated propagation relationship; required alignments exist.
6. Musical restrictions: enforce `time-only` and explicit pass-specific silence; validate continuation boundaries.
7. Semantic issues: surface unresolved choices recorded by planning; syntactic validity does not prove musical intent.
8. Render eligibility: existing language/voicebank, duration, preprocess-review, and credit checks still apply.

Compile a compact occurrence-to-alignment table, keyed by target and occurrence ID, with optional disjoint beat spans. During the existing synthesis traversal, project the chosen lyric event onto each performed note using source provenance. Reset lyric-continuation state at discontinuous jumps; carry it across an ending boundary only when the chosen alignment supports it. Never carry a syllable from a different verse across a return.

Do not modify cached original/expanded notes in place. Render through an immutable projection or render-local copy. The compiled plan has no generated wall-clock timestamps; existing beat/tempo conversion determines timing.

Performance: index source lyrics during existing parsing, expand music once per needed representation, and compile using measure occurrences plus lyric-coverage indexes. Additional preparation may inspect affected notes, but no scan is added to playback frames. Avoid expanding a score again just to call `measureMap()`.

## 11. API, MCP, persistence, and billing contracts

### 11.1 Proposed tool/API operations

- `get_verse_planning_context`: returns revision-scoped structure, traversals, target inventory, evidence, and accepted decisions; supports targeted detail retrieval.
- `validate_verse_plan`: accepts a candidate, returns normalized validation issues or a server-issued validated plan reference. Standalone MCP use can supply the full candidate; backend orchestration may call the same implementation internally.
- Backend workflow entry `start_verse_planning_workflow`: analogous orchestration to preprocessing; invokes LLM planning and targeted repair before quoting.

Proposed Automatic synthesis request:

```json
{
  "part_id": "Soprano (Derived)",
  "language": "en",
  "expand_repeats": true,
  "verse_mode": "automatic",
  "verse_plan_id": "vp-validated-123",
  "verse_plan_hash": "server-canonical-hash"
}
```

Existing required synthesis arguments outside this illustration remain unchanged. Legacy fixed requests retain exact `lyric_selection`. Replace the unconditional lyric-selection requirement with a validated alternative: fixed + exact selection, or Automatic + validated plan. Reject contradictory combinations. A stateless MCP/API invocation may provide an inline candidate, but must pass the same validator before compilation; it cannot claim a trusted plan ID.

### 11.2 Storage and invalidation

Persist draft/validated plans, accepted decisions, source/preparation/map fingerprints, validation reports, and compiled artifacts through existing session/storage mechanisms. Keep large inventories/compiled tables in artifact storage with compact references rather than inflating every chat or progress response. Ownership checks apply to plan IDs exactly as to score/audio artifacts.

Cache structural extraction separately from lyric alignment and plan compilation. Plan cache inputs include schema/planner-policy version, source revision, extraction/alignment revisions, lyric inventory/family, verse mode, playback-map identity, and explicit decision revision.

Reupload, reparse producing changed facts, solfege regeneration, extraction edits, or accepted semantic changes invalidate dependent items. Token URL refresh does not change a plan or artifact identity. Restore plans together with their source evidence and compiled dependencies after local artifacts are evicted; if dependencies cannot be recovered, require replanning/preparation.

### 11.3 Quotes and audio artifacts

Bind the render quote/confirmation to the validated plan hash and normal synthesis parameters. Reject a materially changed plan after confirmation and issue an updated quote/summary even when the credit amount is unchanged. Perform verse preflight before debit/reservation or GPU work, integrated with the existing billing lifecycle.

Musical duration comes solely from the chosen written/expanded route, subject to the existing five-minute limit. Verse changes alone do not add duration. Include plan hash and target preparation identity in audio render/cache identity and persist human-readable verse behaviour in job metadata.

The player still has one current vocal take per logical part. Replacing a rendition updates URL, job identity, and display metadata together. Historical chat audio stays identifiable by its original plan. Display Automatic/mixed verse behaviour rather than falsely labelling the take V1.

## 12. Required changes by component/module

All new modules below are proposed. Existing unrelated local changes must be preserved when implementing.

| Module/component | Required change |
| --- | --- |
| `src/musicxml/parser.py` | Retain all lyric events/raw restrictions and stable provenance alongside the legacy selection projection. Reuse expanded provenance for occurrence mapping. Avoid deriving next verse from per-measure visit count. |
| `src/musicxml/io.py` | Ensure raw XML access supports preservation of attributes used by lyric planning; do not lose `time-only` during read/round-trip paths. |
| `src/musicxml/part_reference.py` | Extend source/derived identity mapping as needed for staff, voice, measure, note, and chord-member provenance. Preserve original source grid through extraction. |
| New `src/musicxml/lyric_inventory.py` | Own lyric alternatives, coverage and source evidence indexes, explicit restrictions, and identities. No interpretation keyword tables. |
| New `src/musicxml/playback_occurrences.py` | Generate occurrence/traversal IDs and structural context from the existing music21 expansion; handle ending and nested-repeat contexts or return explicit uncertainty. |
| `src/api/score.py` | Attach inventory/map references to both parse variants; expose planning readiness in summary. Reuse parsed artifacts rather than adding further expansion passes for planning. |
| `src/api/voice_parts.py` | Separate structural extraction from lyric-specific preparation; preserve all alternatives/restrictions in materialized derived XML or accompanying artifacts; record propagation provenance; validate each required line; retain supported per-line timing adjustments. |
| `src/api/voice_part_lint_rules.py` | Scope lyric completeness/continuation checks by target and line, distinguish intentionally shared/pass-excluded material from missing alignment, and keep structural checks shared. |
| New `src/api/verse_plan.py` | Central plan schema normalization, validation, issue reporting, selector compilation, and render-local lyric projection. Used identically by backend/API/MCP. |
| `src/api/synthesize.py` | Require fresh valid plan for Automatic; project lyrics before existing grouping/phonemization/timing; preserve route/duration; incorporate plan into render identity. |
| `src/musicxml/solfege.py` | Preserve separate generated-family identity and per-target alignment; invalidate relevant plan dependencies on regeneration/settings changes. |
| `src/musicxml/performance_midi.py` | Consume/verify the same written/expanded route identity. Lyrics do not trigger MIDI regeneration; retain deferred generation on first synthesis. Add consistency assertions/tests rather than a verse-specific MIDI engine. |
| `src/mcp/tools.py` | Add planning-context/validation schemas; update synthesis fixed-vs-plan alternatives and descriptions; expose structured unresolved results. |
| `src/mcp/handlers.py` | Route new operations and enforce common preflight for direct callers. Forward validated plan context to synthesis. |
| `src/backend/orchestrator.py` | Integrate planning after target preparation and before quote; replace single-verse reprepare guard with per-line readiness for new artifacts; bound repair loop; enforce user decisions, revision checks, and cancellation. |
| New `src/backend/verse_planning.py` | Isolate planner context building, LLM request/repair workflow, accepted-decision reuse, and plan lifecycle helpers to limit further orchestrator growth. |
| `src/backend/llm_prompt.py` | Add dedicated verse-planner prompt/context path and token budgeting/detail retrieval support. |
| New `src/backend/config/verse_planning_prompt.txt` | Define exact output contract, whole-piece interpretation, evidence requirements, shared lyrics, endings, nested returns, no invented words/timestamps, and targeted repair behaviour. |
| `src/backend/config/system_prompt.txt` and `system_prompt_lessons.txt` | Align chat instructions with Automatic planning, preparation-before-quote, fixed selection and Solfege, and explicit unresolved questions. Remove conflicting unconditional fixed-verse requirements only where applicable. |
| `src/backend/llm_client.py`, `llm_gemini.py`, `llm_openai.py` | Reuse existing provider interfaces; verify new tool/structured outputs across providers. Extend only contract plumbing where necessary. Add fake-LLM planner scenarios in the fake client. |
| `src/backend/session.py` and existing `storage_client.py` | Store plan lifecycle/revisions and artifact references; restore dependencies; apply ownership and invalidation. Avoid persisting only a transient UI selection. |
| `src/backend/main.py` | Extend chat/request selection fields and progress responses; expose plan summaries/decision-required states; preserve legacy fixed requests. |
| Existing quote/credit/job paths in backend | Bind confirmation to plan hash, block unresolved/stale plans before billing, persist plan metadata in rendered jobs, and keep existing refund/failure rules. |
| `ui/src/api.ts` | Add verse-mode, plan summary/status, progress and audio metadata types; send With Repeats plus selected verse mode. |
| `ui/src/MainApp.tsx` | Add Automatic option; preserve fixed-line choices; show planning/preparation status and compact plan summary in chat/quote; label mixed-verse tracks accurately; retain original score preview and atomic take replacement. |
| Existing score-player/highlight components | Continue consuming the shared performance map; no LLM, lyric selection, or note scan during playback. Revalidate original-measure mapping after SATB extraction. |

## 13. Fixture and regression plan

Keep the eight baseline fixtures intact. Add companion multi-verse fixtures under `tests/fixtures/repeat_navigation/multi_verse/`, with a manifest containing literal expected source order, lyric-line assignments, and sung syllable sequences. Use proper singable words and correctly split multisyllable words. Expectations must be authored independently of the compiler.

Baseline source routes verified during design, shown one-based for readability:

| Existing fixture | Expanded source order | Multi-verse assertion |
| --- | --- | --- |
| `forward_repeat.xml` | 1 2 1 2 3 4 5 | V1 then V2 in repeated block; independent/shared following material. |
| `volta_endings.xml` | 1 2 1 3 4 5 | Second ending uses pass-2 assignment on its first visit. |
| `da_capo.xml` | 1 2 3 4 1 2 3 4 5 | Explicit next-stanza and same-words-on-return variants. |
| `da_capo_al_fine.xml` | 1 2 3 4 1 2 | Return lyrics selected correctly and stop at Fine. |
| `da_capo_al_coda.xml` | 1 2 3 4 1 2 3 5 6 | Return and coda have distinct explicit assignments. |
| `dal_segno.xml` | 1 2 3 4 2 3 4 5 | Intro once; returned range assigned separately. |
| `dal_segno_al_fine.xml` | 1 2 3 4 2 3 | Return assignment ends at Fine. |
| `dal_segno_al_coda.xml` | 1 2 3 4 5 3 4 6 | V2 on returned measures 3–4; coda unchanged unless assigned otherwise. |

Additional fixtures cover three passes, more verses than passes, nested/independent repeats, D.S. plus inner repeats, one-measure repeats, combined endings such as 1–2 versus 3, explicit `time-only`, pickup and tempo changes, nonnumeric lyric IDs, translations, shared chorus/endings, submeasure transitions, different verse melismas, isolated missing syllables, and unaligned stanza text. Explicitly record expansion limitations when the library cannot produce a valid route.

SATB fixtures: separate voices in one staff, chord-split voices, shared lyrics on only one voice, V1 prepared but V2 missing, distinct rhythms requiring separate alignments, and a voice silent on pass 1 but entering in ending 2. Assert that extraction preserves notation/provenance and that no missing derived verse is misclassified as shared.

Test placement:

- `tests/test_lyric_selection_contract.py`, `tests/test_musicxml_parser.py`: legacy compatibility, alternative preservation and raw restrictions.
- `tests/test_repeat_navigation.py` plus new `tests/test_verse_plan.py`: exact occurrence/traversal matching, plan validation and compiled lyric sequence for every fixture.
- `tests/test_voice_parts.py`, `test_voice_parts_guards.py`, `test_voice_parts_e2e.py`, and lint regressions: per-line alignment, partial preparation, materialization/restore, source mapping.
- `tests/test_backend_api.py`, `test_llm_prompt.py` and provider/fake-client tests: planning, bounded repairs, user questions, stale plans, quote binding, cancellation, zero billing on validation failure, restore/cache behaviour.
- `ui/e2e/specs/core-singing-regression.spec.ts` or a focused new verse-planning spec: fake LLM with real parsing, preprocessing, synthesis, MIDI and browser playback. Cover Automatic, fixed lyrics, Solfege, With Repeats on/off, SATB and replacing a previous rendition. Assert chosen syllables through synthesis-input artifacts; playback/source assertions alone do not prove sung verse correctness.

Clean up only test-owned browsers/servers after E2E runs. Never terminate user-owned local services. Include a manual musical review of the short audio fixtures, especially melismas and returns.

## 14. Implementation sequence and acceptance gates

1. Define schemas, source identities and independent fixture expectations. Gate: legacy fixed-selection behaviour remains specified and covered.
2. Preserve lyric alternatives/restrictions and generate occurrence/traversal indexes. Gate: exact routes, endings, pickups and provenance pass without additional playback work.
3. Extend SATB preparation to per-line alignments/materialization. Gate: required lines survive extraction/reload and independently validate.
4. Implement deterministic plan validation/compilation. Gate: exact lyric sequences pass and invalid plans fail before synthesis.
5. Add LLM planner, repair lifecycle, session storage and MCP/API contracts. Gate: bounded corrections, user questions, cancellation/staleness and direct-call validation pass.
6. Integrate quote binding, render identity and synthesis projection. Gate: no credit use on unresolved plans; fixed/Automatic renders use the intended lyrics and same musical timing.
7. Add UI selection/status/metadata and full regression coverage. Gate: all eight navigation forms, shared lyrics and SATB workflows pass with fake LLM; test services are cleaned up.

Completion requires a validated plan for every Automatic render, accurate word/continuation selection across repeat occurrences, stable MIDI/highlight timing, original notation preview, working fixed-mode compatibility, and persistent/recoverable decisions. No production rollout or unrelated code changes are part of this draft.
