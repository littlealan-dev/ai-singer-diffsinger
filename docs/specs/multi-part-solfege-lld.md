# Multi-Part Solfege LLD

**Status:** Agreed design, not yet implemented
**Scope:** Let one `add_solfege_lyric_verse` call add a generated solfege line
to several parts, and give the LLM a rule for which parts a group request such
as "add solfege to all parts" covers.

## 1. Problem

A local turn, "add solfege to all parts" on an SATB + piano score, made eight
LLM calls before reaching the piano and then ran out of its 10-call budget:

```
Sopran → Alt → Sopran (already exists) → Tenor → Bass → Sopran (already exists)
→ Klavier staff 1 (needs splitting) → start preprocess → 2 preprocess calls → limit
```

| Cause | Effect |
|---|---|
| The tool takes exactly one part, and each follow-up call sees only the latest tool result | The LLM rebuilds "which parts are done" from the score summary on every call and loses track |
| No rule says which parts "all parts" covers | The LLM took it literally and included the piano |

The summary mislabelling that misled the LLM (`lyric_verses`) and the signature
change on tied notes are fixed separately (commit `9a84669`).

## 2. Decisions

| Topic | Decision |
|---|---|
| Input | `parts: [part_id, …]` replaces `part_id`. One part is a list of one. |
| Which parts a group request covers | Decided by the LLM from the score summary, using the judging factors in §6. No backend "all vocal" scope: without lyrics, a vocal part's instrument facts often cannot be told apart from an instrument's, and the backend does not infer roles from names. |
| Processing | One pass over one MusicXML file; one new score version. |
| Part that already has a solfege line | Reported as `already_present`; not an error. |
| Part that needs splitting | Skipped and reported. In a group request the LLM asks before preparing it. A single part the user named is handled as today. |
| Calls for a group request | One tool call and one follow-up, whatever the number of parts. |

## 3. Tool interface

```
add_solfege_lyric_verse(
    parts:  [string],     # exact score_summary.parts[].part_id values, at least one
    reason: string,
    # injected by the backend, as today: source_musicxml_path, output_musicxml_path, settings
)
```

The description changes from "exactly one selected clean part" to: adds
deterministic solfege to each listed part, in one call; pass every part the
request covers.

## 4. Processing

### 4.1 Transform (`src/musicxml/solfege.py`)

```
add_solfege_lyric_verses(source, output, *, raw_part_ids, settings):
    root = read(source)
    for each raw part id, in request order, once:
        part not found                         → skipped {code: target_not_found}
        part needs splitting (_part_complexity) → skipped {code: complex_target_requires_preparation, …}
        part already has a generated line      → already_present
        part has no pitched notes              → skipped {code: no_pitched_notes}
        otherwise: append the generated line   → completed
    if completed: write(root, output)
    return {completed, already_present, skipped}
```

The checks and their order are today's, applied per part. The generated line
keeps its number (`SSSolfege`) and name in every part.

### 4.2 API (`src/api/solfege.py`)

```
add_solfege_lyric_verse(source, output, *, part_ids, settings):
    resolve each parser part_id to its raw part id (resolve_part_reference)
        unknown id → skipped {code: target_not_found}
        parser parts sharing a raw part (staves of one instrument) → processed once,
            reported under each requested parser part_id
    result = transform
    if result.completed:
        parse the output once; find each completed part's generated lyric_selection
        derived score: the output parsed with the first completed part's selection
    attach lyric_selection to every completed and already_present entry
```

### 4.3 Result

```
status "ready"            when completed or already_present is non-empty
{
    status, derived_score, score_summary, derived_musicxml_path (when written),
    completed_targets: [{part_id, part_name, raw_part_id, lyric_selection}],
    already_present:   [{part_id, part_name, lyric_selection}],
    skipped:           [{part_id, code, message, diagnostics}],
    settings, warnings
}
status "action_required"  when every requested part was skipped:
    the first skipped part's code and message, plus the full skipped list
```

A single part that needs splitting therefore returns
`complex_target_requires_preparation` exactly as today.

## 5. Backend after the tool (`orchestrator.py`, `TOOL_ADD_SOLFEGE_VERSE`)

```
status action_required → follow-up with the payload (unchanged path)
status ready:
    if completed_targets: persist once (_persist_solfege_result) → one new score version
    selected verse = SSSolfege (as today)
    follow-up (tools allowed):
        { status: "solfege_verses_ready",
          completed_targets, already_present, skipped,
          message: report which parts now have solfege, which already had it, and which
                   were skipped and why; for a part that needs splitting, offer to prepare
                   it instead of preparing it; do not call add_solfege_lyric_verse again
                   for any part listed here in this turn }
    session_state_changed = bool(completed_targets)
```

`operation_scope` and `completed_target` (singular) are removed from the
follow-up. Adding a solfege line no longer changes a part's signature, so the
stale-solfege regeneration hook does not fire after this tool.

## 6. Prompt rules (`system_prompt.txt`)

Replace the two rules that select one part and add parts sequentially with:

```
- For add_solfege_lyric_verse, pass every part the request covers in one call, as `parts`:
  exact score_summary.parts[].part_id values. Never use part_index, raw_part_id or part_name.
- A request naming parts covers exactly those parts, whatever they are.
- A group request ("all parts", "every part", "everyone") covers the vocal parts only.
  Judge each part from the score summary:
    confirmed vocal when any of:
      - a lyric_selections[] entry with is_generated_solfege false (authored lyrics);
        has_lyrics alone is not enough, it also counts generated solfege
      - instruments[].is_explicit_vocal is true (instrument_sound in the voice.* family)
      - instruments[].instrumental_role is "not_instrumental" (present only after an LLM
        role decision for a part with no declared instrument)
      - is_derived_part, and its source part is confirmed or likely vocal: the source is
        preprocess_mapping_context.derived_mapping.targets[] (derived_part_id → source part)
        when present, otherwise the source name inside the derived part_name
    otherwise judge from, in this order of weight:
      - part_name and instruments[].instrument_name: does the name denote a singing voice
        or an instrument? A name that denotes an instrument wins over a voice word in it.
      - instruments[].instrument_sound: an instrument sound ID means an instrument
      - instruments[].is_percussion: not vocal
      - a part_id with a staff suffix (-Staff1, -Staff2): one staff of a multi-staff
        instrument, not vocal
      - pitch_range (tessitura within a human voice range), resolved_gm_program
        (a GM voice program): supporting evidence only, never decisive
    include confirmed and likely vocal parts; leave out parts judged to be instruments;
    leave out parts you cannot judge, and name them in the reply so the user can add them.
- After the result, tell the user what was added, already present and skipped. For a skipped
  part that needs splitting: in a group request, offer to prepare it; when the user named
  that part alone, prepare it as before.
```

The rules that call the tool for one part (`part_has_no_lyrics`, "sing … in
solfege" with no solfege line) are unchanged apart from passing `parts: [id]`,
and keep using the returned `lyric_selection` of that part.

## 7. Other callers

| Caller | Change |
|---|---|
| MCP tool schema (`src/mcp/tools.py`) | `parts` array (minItems 1) replaces `part_id` |
| MCP handler (`src/mcp/handlers.py`) | pass `part_ids` |
| E2E stub LLM (`src/backend/llm_client.py`) | `{"parts": ["Solo"]}` |
| Backend test router (`tests/test_backend_api.py`) | `part_ids` |

## 8. Tests

| Area | Cases |
|---|---|
| Transform | several parts in one pass and one write; already present; needs splitting; no pitched notes; unknown part; nothing written when nothing is completed |
| API | staves of one instrument processed once; each completed and already-present part gets its own lyric_selection; all-skipped returns action_required with the first code |
| Backend | "all parts" stub: one tool call, one persisted version, follow-up lists completed / already_present / skipped; no second tool call; single complex part keeps today's action_required |
| Prompt | the static prompt carries the group-request rule; the tool schema requires `parts` |
| E2E | existing solfege specs pass with the stub's `parts` |

## 9. Build order

1. Transform and API, with their tests.
2. Tool schema, handler, orchestrator follow-up, test router and stub LLM.
3. Prompt rules.
4. Backend and e2e suites; then a local check: "add solfege to all parts" on the
   Amazing Grace demo makes one tool call and leaves the piano out.
