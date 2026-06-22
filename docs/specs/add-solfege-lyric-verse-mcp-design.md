# Design Spec: Add Solfege Lyric Verse MCP Tool

## 1. Status

- Status: Proposed
- Scope: Studio backend, MCP, and LLM orchestration
- MCP tools: `add_solfege_lyric_verse`, `modify_solfege_settings`
- Direct API: `GET/PATCH /sessions/{session_id}/solfege-settings`
- Worker: CPU
- Output: Derived MusicXML with one new lyric verse

## 2. Purpose

Add a deterministic MCP tool that writes solfege syllables into a selected MusicXML part as a new lyric verse. The tool changes score data only; it does not synthesize audio.

The intended conversational workflow is:

```text
User: Sing the soprano part in solfege.
  -> LLM inspects score_summary.parts[].lyric_verses[]
  -> if a suitable solfege verse exists: select it and synthesize
  -> otherwise: call add_solfege_lyric_verse
  -> tool returns a derived score with the new verse selected
  -> LLM calls synthesize with solfege_pronunciation_patch=true
```

The LLM chooses the target and options. The backend computes every solfege syllable deterministically from MusicXML pitch and tonal context.

## 3. Goals

- Preserve every existing part, note, lyric verse, direction, and score attribute.
- Add exactly one new lyric verse to one selected singing line.
- Support movable-do and fixed-do systems.
- Return the new verse number and an updated parsed score summary.
- Make the derived score immediately usable by `synthesize`.
- Expose session-level solfege settings in the Studio UI.
- Recompute every generated solfege verse when those settings change.
- Keep UI-, API-, and LLM-initiated setting changes synchronized.
- Reject ambiguous or structurally complex targets instead of guessing.
- Make retries idempotent within one orchestration operation.

## 4. Non-Goals

- Generating solfege with an LLM.
- Splitting chords, staves, or multiple MusicXML voices.
- Selecting a top note from a chord as a Studio fallback.
- Replacing or editing a user-authored lyric verse.
- Synthesizing or saving audio.
- Inferring an uncertain major/minor mode silently.
- Implementing arbitrary lyric translation.

## 5. Responsibility Boundaries

### LLM

- Interprets user intent.
- Selects the target part or prepared singing line.
- Selects movable-do versus fixed-do when explicitly requested.
- Selects major, minor la-based, or minor do-based mode when requested.
- Calls `modify_solfege_settings` when the user asks to change settings after solfege verses exist.
- Calls `synthesize` after a successful transform when the user requested singing.

### Orchestrator

- Injects the active score and canonical MusicXML path.
- Injects a session-scoped output path and idempotency key.
- Replaces the active session score with the successful derived score.
- Preserves the returned new verse as the explicit selected verse.
- Stores canonical solfege settings and a monotonically increasing settings revision.
- Publishes updated settings and score versions to every connected UI client.
- Does not calculate syllables or semantically choose a verse.

### MCP Tool

- Validates the target and tonal context.
- Allocates the next unused lyric verse number mechanically.
- Performs the deterministic MusicXML mutation.
- Recomputes all machine-marked generated solfege verses when settings change.
- Writes and reparses the derived artifact.
- Returns structured success or `action_required` output.

### Synthesizer

- Receives the derived parsed score.
- Sings the selected new verse.
- Applies `solfege_pronunciation_patch=true` when requested by the LLM.

## 6. User Workflows

### 6.1 Add Only

```text
User: Add movable-do solfege to Alto.
LLM confirms canonical settings are already movable-do + major
LLM -> add_solfege_lyric_verse(part_id="Alto")
Tool -> ready(new_verse_number="3", derived score)
LLM -> reports that the new verse is available for review
```

No synthesis is started because the user asked only to modify the score.

### 6.2 Add And Sing

```text
User: Sing Tenor in solfege.
LLM finds no existing solfege verse
LLM -> add_solfege_lyric_verse(part_id="Tenor")
Tool -> ready(new_verse_number="3", selected verse="3")
LLM -> synthesize(part_id="Tenor", solfege_pronunciation_patch=true)
```

No `reparse` call is required after success because the tool returns and activates a parsed score with the new verse selected.

### 6.3 Existing Solfege Verse

If `score_summary.parts[].lyric_verses[]` already contains a clearly solfege-like verse, the LLM must use the existing selection workflow. It must not call this tool merely to create a duplicate.

### 6.4 Complex Target

```text
User: Sing the alto line inside this combined SA staff in solfege.
LLM -> start_preprocess_voice_part_workflow
Backend -> materializes a clean derived Alto line
LLM -> add_solfege_lyric_verse(targeting the derived Alto part)
LLM -> synthesize
```

The solfege tool does not perform voice extraction.

### 6.5 Change Settings From UI

```text
User selects Fixed Do in SolfegeSettingsControl
UI -> PATCH /sessions/{session_id}/solfege-settings
API -> updates canonical settings and every generated solfege verse
UI -> applies returned settings revision and refreshes the score preview
```

This flow bypasses the LLM completely.

### 6.6 Change Settings Through Chat

```text
User: Change the solfege to minor, la-based.
LLM -> modify_solfege_settings(mode="minor_la_based")
Tool -> updates canonical settings and every generated solfege verse
Backend -> returns updated settings and score version
UI -> hydrates the control and refreshes the score preview
```

The MCP tool calls the same application service as the direct API. It does not simulate a UI event.

## 7. MCP Contract

### 7.1 LLM-Authored Arguments

```json
{
  "part_id": "Soprano",
  "part_index": null,
  "reason": "The user asked to sing the soprano line in solfege."
}
```

Rules:

- Exactly one of `part_id` and `part_index` is required.
- The tool uses the session's canonical solfege settings injected by the backend.
- To change system or mode, the LLM calls `modify_solfege_settings` before adding a verse.
- The LLM does not provide a verse number or filesystem path.

### 7.2 Backend-Injected Arguments

The orchestrator adds these fields before calling MCP:

```json
{
  "score": {},
  "source_musicxml_path": "data/sessions/<session>/score.xml",
  "output_musicxml_path": "data/sessions/<session>/score-solfege-<operation>.xml",
  "operation_id": "<session-scoped idempotency key>",
  "solfege_settings": {
    "system": "movable_do",
    "mode": "major",
    "revision": 1
  }
}
```

These are runtime inputs, not semantic choices. The LLM must not fabricate them.

### 7.3 Input Schema

```json
{
  "type": "object",
  "properties": {
    "score": { "type": "object" },
    "source_musicxml_path": { "type": "string" },
    "output_musicxml_path": { "type": "string" },
    "operation_id": { "type": "string", "minLength": 1 },
    "solfege_settings": {
      "type": "object",
      "properties": {
        "system": {
          "type": "string",
          "enum": ["movable_do", "fixed_do"]
        },
        "mode": {
          "type": "string",
          "enum": ["major", "minor_la_based", "minor_do_based"]
        },
        "revision": { "type": "integer", "minimum": 1 }
      },
      "required": ["system", "mode", "revision"],
      "additionalProperties": false
    },
    "part_id": { "type": ["string", "null"] },
    "part_index": { "type": ["integer", "null"], "minimum": 0 },
    "reason": { "type": "string", "minLength": 1 }
  },
  "required": [
    "score",
    "source_musicxml_path",
    "output_musicxml_path",
    "operation_id",
    "solfege_settings",
    "reason"
  ],
  "oneOf": [
    {
      "required": ["part_id"],
      "properties": {
        "part_id": { "type": "string", "minLength": 1 },
        "part_index": { "type": "null" }
      }
    },
    {
      "required": ["part_index"],
      "properties": {
        "part_index": { "type": "integer", "minimum": 0 },
        "part_id": { "type": "null" }
      }
    }
  ],
  "additionalProperties": false
}
```

### 7.4 Success Output

```json
{
  "status": "ready",
  "message": "A solfege lyric verse was added to the selected part.",
  "derived_score": {},
  "score_summary": {},
  "derived_musicxml_path": "data/sessions/<session>/score-solfege-<operation>.xml",
  "target": {
    "part_id": "Soprano",
    "part_index": 0,
    "part_name": "Soprano"
  },
  "new_verse_number": "3",
  "selected_verse_number": "3",
  "settings": {
    "system": "movable_do",
    "mode": "major",
    "revision": 1
  },
  "resolved_tonal_regions": [],
  "notes_annotated": 38,
  "notes_extended": 2,
  "warnings": []
}
```

Required success guarantees:

- `derived_score.selected_verse_number == new_verse_number`.
- The new verse appears in the target part's `score_summary.lyric_verses` sample.
- The output artifact reparses successfully before `status=ready` is returned.
- The source artifact remains unchanged.

### 7.5 Action-Required Output

```json
{
  "status": "action_required",
  "action": "line_preparation_required",
  "code": "complex_target_requires_preparation",
  "message": "The selected target must be prepared as one clean singing line before solfege can be added.",
  "diagnostics": {
    "part_id": "Women",
    "voices": ["1", "2"],
    "chord_measures": [12, 13]
  }
}
```

User-resolvable conditions return `action_required`; infrastructure and programming failures raise errors.

## 8. MusicXML Mutation Rules

### 8.1 Source Fidelity

- Mutate the XML tree, not the lightweight parsed score JSON.
- Preserve all non-target XML nodes and attributes.
- Preserve all existing `<lyric>` elements exactly.
- Write to a new file using UTF-8 and an XML declaration.
- Never overwrite the uploaded source file.

### 8.2 Generated-Verse Provenance

Every generated verse must be distinguishable from user-authored solfege so settings changes can update only product-owned content.

- Set lyric `name="SightSinger Solfege"` on generated lyric elements.
- Add a score-level MusicXML `<miscellaneous-field>` named `sightsinger.solfege.v1`.
- Store compact JSON containing each generated verse's part ID, verse number, transform ID, and creation settings.
- Treat the embedded marker plus matching lyric name as authoritative provenance.
- Preserve this metadata through download, re-upload, and subsequent transforms.
- Never update a solfege-like verse that lacks SightSinger provenance.

Example provenance payload:

```json
{
  "version": 1,
  "settings": {
    "system": "movable_do",
    "mode": "major"
  },
  "generated_verses": [
    {
      "part_id": "Soprano",
      "verse_number": "3",
      "transform_id": "01J..."
    }
  ]
}
```

### 8.3 Verse Allocation

1. Collect lyric `number` values in the selected part.
2. Treat missing or blank lyric numbers as verse `1`.
3. Parse positive integer numbers.
4. Allocate `max(existing positive integers) + 1`; use `1` when none exist.
5. Do not reuse gaps because stable append semantics are easier to reason about.
6. Emit the allocated value as a string in API output.

This is mechanical allocation, not semantic verse selection.

### 8.4 Lyric Elements

For an articulated pitched note:

```xml
<lyric number="3" name="SightSinger Solfege">
  <syllabic>single</syllabic>
  <text>so</text>
</lyric>
```

For a tie continuation that should sustain the preceding syllable:

```xml
<lyric number="3" name="SightSinger Solfege">
  <extend/>
</lyric>
```

Rules:

- Skip rests.
- Skip grace notes unless a later product requirement explicitly enables them.
- A tie start receives the syllable.
- Tie stop/continue notes receive `<extend/>`, not a repeated syllable.
- Slurred but independently articulated notes each receive a syllable.
- Existing lyric melismas do not control the generated solfege verse.

### 8.5 Clean-Line Validation

The selected target is accepted only when it represents one unambiguous monophonic event stream.

Reject when any selected time span contains:

- simultaneous pitched notes,
- multiple active MusicXML voices,
- multiple staves requiring lane selection,
- a raw combined part identified by voice-part analysis,
- unresolved overlapping ties that produce more than one pitch at an onset.

Return `complex_target_requires_preparation`. Do not choose a top note.

## 9. Solfege Mapping

### 9.1 Canonical Written Syllables

The generated MusicXML stores conventional written solfege, not pronunciation spellings:

```text
do di ra re ri me mi fa fi se so si le la li te ti
```

The existing synthesis flag converts these whole tokens to English singing pronunciations such as `do -> doh`, `fi -> fee`, and `so -> soh`. Pronunciation spellings must not be baked into the score.

### 9.2 Fixed-Do

- C maps to `do`, D to `re`, E to `mi`, F to `fa`, G to `so`, A to `la`, and B to `ti`.
- Accidentals use the canonical chromatic syllable table.
- Octave does not affect the syllable.
- Key signatures and mode do not affect fixed-do mapping.

### 9.3 Movable-Do Major

- Tonic is `do`.
- Map the written pitch against the current tonic and diatonic degree.
- Preserve chromatic direction where spelling is available, for example raised 4 is `fi` and lowered 7 is `te`.

### 9.4 Movable-Do Minor

- `mode=minor_la_based`: relative-major tonic remains `do`; the minor tonic is `la`.
- `mode=minor_do_based`: the minor tonic is `do` and degrees are mapped relative to it.
- The selected policy applies consistently across the score unless a future API introduces region-specific policy.

### 9.5 Tonal Regions And Key Changes

- Track `<key><fifths>` and `<key><mode>` by measure.
- A key declaration remains active until replaced.
- Resolve every key-change region independently.
- `mode=major` treats every fifths region as its corresponding major key.
- `mode=minor_la_based` or `mode=minor_do_based` treats every fifths region as its corresponding relative minor key.
- The selected setting is authoritative; an explicit conflicting MusicXML `<mode>` produces a warning but does not override it.
- Fixed-do does not require mode resolution.

## 10. Idempotency And Concurrency

- The orchestrator generates one `operation_id` per logical transform request.
- Compute a request fingerprint from source SHA-256, target selector, and canonical settings revision.
- If the same `operation_id` and fingerprint already completed, return the existing result.
- If the same `operation_id` is reused with a different fingerprint, return an error.
- Write to a temporary file and atomically rename it to `output_musicxml_path` only after serialization succeeds.
- Reparse and validate the temporary artifact before activating it in session state.
- Hold the existing per-session chat lock while changing the active score.

## 11. Error Model

| Code | Status | Meaning |
| --- | --- | --- |
| `target_not_found` | `action_required` | Part selector does not resolve. |
| `ambiguous_target` | `action_required` | Selector resolves to multiple candidates. |
| `complex_target_requires_preparation` | `action_required` | Target is not one clean monophonic line. |
| `no_pitched_notes` | `action_required` | Target has no annotatable notes. |
| `solfege_verse_already_exists` | `action_required` | A matching generated verse already exists and should be reused. |
| `generated_verse_provenance_invalid` | `error` | Embedded generated-verse metadata is malformed or inconsistent. |
| `invalid_option_combination` | `error` | Input schema combination is invalid. |
| `source_changed` | `error` | Source hash changed during transformation. |
| `derived_score_invalid` | `error` | Generated MusicXML failed reparse/validation. |
| `write_failed` | `error` | Derived artifact could not be persisted. |
| `settings_revision_conflict` | `action_required` / HTTP 409 | Caller settings state is stale. |
| `score_version_conflict` | `action_required` / HTTP 409 | Active score changed before mutation. |
| `idempotency_conflict` | `error` / HTTP 409 | Operation ID was reused with different input. |

## 12. Existing-Verse Detection

The tool should prevent accidental duplicates without deciding which verse the user intended to sing.

Before mutation:

1. Inspect existing lyric verses on the selected target.
2. Compare non-empty whole tokens against the canonical solfege vocabulary.
3. If a verse is overwhelmingly solfege and covers the target note stream, return `solfege_verse_already_exists` with candidate verse numbers.
4. Let the LLM select/reuse that verse through the existing `reparse` workflow.

Do not automatically replace, append over, or select the existing verse inside this tool.

## 13. Backend Integration

### New Modules

```text
src/musicxml/solfege.py       # pure tonal mapping and XML mutation
src/api/solfege.py            # validation, artifact write, reparse
src/backend/solfege_settings.py # shared UI/MCP settings mutation service
```

### Modified Modules

```text
src/mcp/tools.py              # schema and output contract
src/mcp/handlers.py           # handler
src/mcp_server.py             # CPU allowlist
src/backend/mcp_client.py     # CPU routing
src/backend/orchestrator.py   # context injection and active-score update
src/backend/session.py        # canonical settings and score revision state
src/backend/main.py           # GET/PATCH settings endpoints
src/backend/config/system_prompt.txt
ui/src/api.ts                 # direct settings API client
ui/src/components/SolfegeSettingsControl.tsx
ui/src/MainApp.tsx            # score workspace integration and hydration
```

### Session Update On Success

The orchestrator must atomically store:

- the derived MusicXML path as the active source,
- the derived parsed score as `current_score`,
- the returned score summary,
- `explicit_verse_number = new_verse_number`,
- canonical solfege settings and settings revision,
- a monotonically increasing score version,
- transform provenance including source path, source hash, options, and target.

Derived voice-part mappings from a different source score version must be invalidated.

## 14. Prompt Rules

Add these orchestration rules:

- Prefer an existing clearly solfege-like verse over creating a duplicate.
- If the user explicitly asks to add solfege and no suitable verse exists, call `add_solfege_lyric_verse`.
- If the user asks to sing in solfege and no suitable verse exists, add the verse first, then synthesize the returned selected verse with `solfege_pronunciation_patch=true`.
- Do not ask the LLM to generate or pass individual syllables.
- Prepare complex voice parts before calling the solfege tool.
- Do not call `reparse` after successful insertion; the returned derived score already selects the new verse.
- If the tool reports an existing solfege verse, use `reparse` to select that verse rather than adding another.
- If the user asks to change solfege system or mode, call `modify_solfege_settings` with only the requested fields. The tool updates every generated solfege verse and returns canonical UI settings.
- Do not add another verse merely because settings changed.
- After a settings change, continue using the current selected verse unless the user requests another target or verse.

## 15. Solfege Settings UI

### 15.1 Component

Add a `SolfegeSettingsControl` to the score workspace toolbar or score-settings panel. It is an operational control, not a chat message or marketing card.

Controls:

```text
System: [Movable Do | Fixed Do]
Mode:   [Major | Minor (La) | Minor (Do)]
```

Recommended interaction components:

- Use a two-option segmented control for `System`.
- Use a three-option segmented control or compact select for `Mode`, depending on available width.
- On mobile, use a select for mode if the three labels cannot fit without truncation.
- Disable `Mode` while `System=Fixed Do` because mode does not affect fixed-do pitch names.
- Retain the last selected mode while disabled so switching back to movable-do restores it.
- Show a compact pending indicator while a settings mutation is running.
- Disable both controls during the mutation to prevent conflicting writes.
- On failure, restore the last server-confirmed values and show the normal application error treatment.

### 15.2 Canonical Settings Model

```ts
type SolfegeSettings = {
  system: "movable_do" | "fixed_do";
  mode: "major" | "minor_la_based" | "minor_do_based";
  revision: number;
};
```

Defaults for every new score session:

```json
{
  "system": "movable_do",
  "mode": "major",
  "revision": 1
}
```

These defaults replace the earlier `mode=auto` proposal. The UI exposes all supported settings explicitly, and generated results therefore never depend on an invisible mode inference.

### 15.3 Visibility And Empty State

- Show the control whenever a score is loaded.
- If the score has no SightSinger-generated solfege verses, changing settings updates the session default for future generated verses but does not modify MusicXML.
- Once generated verses exist, display the number of affected generated verses in accessible status metadata, not as permanent instructional copy.
- User-authored solfege-like verses are never counted or modified.

### 15.4 Direct UI Flow

The control calls the settings API directly and does not involve the LLM:

```text
User changes control
  -> UI sends PATCH /sessions/{session_id}/solfege-settings
  -> backend updates canonical settings
  -> backend recomputes all generated solfege verses atomically
  -> backend reparses and activates the derived score
  -> response returns settings revision + score version
  -> UI hydrates controls from response and refreshes score preview
```

The UI must not optimistically commit the new values. It may show the pending selection, but the canonical displayed state changes only after a successful response.

## 16. Modify Solfege Settings API

### 16.1 Endpoint

```text
GET   /sessions/{session_id}/solfege-settings
PATCH /sessions/{session_id}/solfege-settings
```

`GET` returns canonical settings, settings revision, score version, and generated-verse count.

`PATCH` accepts a full desired settings value from the UI:

```json
{
  "settings": {
    "system": "fixed_do",
    "mode": "major"
  },
  "expected_settings_revision": 1,
  "expected_score_version": 4,
  "operation_id": "01J..."
}
```

Successful response:

```json
{
  "status": "ready",
  "settings": {
    "system": "fixed_do",
    "mode": "major",
    "revision": 2
  },
  "score_version": 5,
  "updated_generated_verses": [
    { "part_id": "Soprano", "verse_number": "3", "notes_updated": 38 },
    { "part_id": "Alto", "verse_number": "3", "notes_updated": 35 }
  ],
  "current_score": {},
  "score_summary": {}
}
```

If no generated verses exist, return `updated_generated_verses=[]`, increment the settings revision, and leave the score version unchanged.

If settings change but produce byte-equivalent generated lyric content, update the settings revision and provenance but do not increment score version unless the persisted MusicXML artifact changed.

### 16.2 Atomic Update Algorithm

1. Verify `expected_settings_revision` and `expected_score_version` under the session lock.
2. Load the active MusicXML and validate `sightsinger.solfege.v1` provenance.
3. Find every generated verse listed by provenance across all parts.
4. Recompute each verse from note pitches using the requested canonical settings.
5. Replace only generated verse `<text>` and `<extend>` content in place.
6. Preserve part IDs, verse numbers, selected verse, and all user-authored lyrics.
7. Update embedded provenance settings.
8. Write a new derived MusicXML artifact atomically.
9. Reparse and validate the complete artifact.
10. Commit settings, score, score summary, score version, and artifact path in one session-state transaction.

Any failure before step 10 leaves settings and active score unchanged.

### 16.3 Concurrency

- A stale settings revision returns HTTP `409 settings_revision_conflict` with current canonical settings.
- A stale score version returns HTTP `409 score_version_conflict`.
- The UI refreshes canonical state and may retry only the user's latest explicit selection.
- Repeated `operation_id` with the same request returns the original result.
- Reusing an operation ID with different settings returns `409 idempotency_conflict`.

## 17. Modify Solfege Settings MCP Tool

### 17.1 Tool

```text
modify_solfege_settings
```

This is the LLM-facing equivalent of the direct settings API. It invokes the same application service and therefore has identical mutation semantics.

LLM-authored input:

```json
{
  "system": "fixed_do",
  "mode": null,
  "reason": "The user asked to change generated solfege to fixed do."
}
```

Rules:

- At least one of `system` or `mode` must be non-null.
- Omitted/null fields retain their canonical current values.
- The backend injects active paths, current settings revision, score version, and operation ID.
- The tool updates all generated verses, not only the currently selected part.
- The tool returns the same canonical settings and updated-score payload as the REST API.

LLM-visible schema:

```json
{
  "type": "object",
  "properties": {
    "system": {
      "type": ["string", "null"],
      "enum": ["movable_do", "fixed_do", null]
    },
    "mode": {
      "type": ["string", "null"],
      "enum": ["major", "minor_la_based", "minor_do_based", null]
    },
    "reason": { "type": "string", "minLength": 1 }
  },
  "required": ["reason"],
  "anyOf": [
    { "required": ["system"], "properties": { "system": { "type": "string" } } },
    { "required": ["mode"], "properties": { "mode": { "type": "string" } } }
  ],
  "additionalProperties": false
}
```

### 17.2 UI Synchronization Decision

Two implementation options were considered.

#### Option 1: MCP changes the UI control and relies on its change handler

Rejected.

- The backend cannot depend on a browser being connected.
- Programmatic UI hydration can accidentally trigger duplicate API calls.
- Multiple browser tabs create unclear ownership.
- The score mutation would be coupled to a presentation event.

#### Option 2: MCP calls the mutation service directly and publishes canonical state

Selected.

```text
User asks LLM: "Change to fixed do"
  -> LLM calls modify_solfege_settings(system="fixed_do")
  -> MCP handler invokes the shared settings mutation service
  -> service updates all generated verses and session state
  -> tool response contains canonical settings + score version
  -> chat response tells UI to refresh session score/settings
  -> UI hydrates controls without firing its user-change handler
```

The REST endpoint and MCP handler are two adapters over one service, for example:

```python
apply_solfege_settings_change(
    session_id,
    desired_settings,
    expected_settings_revision,
    expected_score_version,
    operation_id,
)
```

Neither adapter calls the other over HTTP.

### 17.3 State Propagation To UI

- Include `solfege_settings`, `solfege_settings_revision`, and `score_version` in the session snapshot.
- Include updated values in direct API responses and completed MCP/chat workflow responses.
- Emit or expose a `solfege_settings_updated` session event when the existing frontend transport supports it.
- On any score refresh, the UI hydrates controls from canonical session state.
- Hydration uses state assignment only; API calls occur exclusively inside explicit user event handlers.
- All connected tabs converge on the highest settings revision.

## 18. Observability

Log structured boundaries without logging entire score payloads:

```text
solfege_transform_start operation_id source_hash part_id part_index system mode
solfege_transform_validated target_part_id verse_number note_count tonal_region_count
solfege_transform_ready output_path output_hash elapsed_ms
solfege_transform_action_required code diagnostics_summary
solfege_transform_failed code elapsed_ms
solfege_settings_update_start operation_id from_revision requested_system requested_mode
solfege_settings_update_ready to_revision score_version updated_verse_count elapsed_ms
solfege_settings_update_conflict expected_revision actual_revision
```

Metrics:

- transform attempts by system and outcome,
- action-required counts by code,
- transform latency,
- annotated-note count,
- existing-verse reuse rate,
- settings changes by origin (`ui` or `mcp`),
- generated verses updated per settings change,
- settings/score revision conflict rate,
- derived-score validation failure rate.

## 19. Security

- Resolve source and output paths through existing project/session path guards.
- The LLM never controls raw paths.
- Reject output paths outside the active session directory.
- Apply existing MusicXML/MXL decompression and size limits.
- Do not fetch external MusicXML references or DTDs.
- Bound part count, note count, lyric token length, and output size.

## 20. Test Plan

### Unit Tests

- Verse allocation with no lyrics, numbered lyrics, missing numbers, and numeric gaps.
- Existing lyrics remain byte-equivalent at the element level.
- Major movable-do mapping.
- Minor la-based and do-based mapping.
- Fixed-do natural and chromatic mapping.
- Mid-score key changes.
- Tie start/stop/continue behavior.
- Rests and grace-note handling.
- Unicode score metadata and lyrics remain intact.
- Complex voices and chords are rejected.
- Existing solfege verse detection returns candidates.
- Retry with the same operation ID is idempotent.
- Generated-verse provenance survives parse/serialize and re-upload.
- Updating settings rewrites every generated verse across multiple parts.
- Updating settings preserves generated verse numbers and current selection.
- User-authored solfege-like verses remain unchanged.
- A failure in any generated verse rolls back the entire settings change.

### MCP Contract Tests

- Tool appears only on the CPU worker.
- Exactly one target selector is required.
- LLM options and backend-injected fields validate correctly.
- Every ready/action-required output matches its schema.
- `modify_solfege_settings` accepts partial setting changes and rejects empty changes.
- Both add and modify tools consume backend-injected canonical settings.

### Backend Integration Tests

- Orchestrator injects canonical paths and operation ID.
- Successful output becomes the active score.
- New verse appears in `score_summary.parts[].lyric_verses[]`.
- `explicit_verse_number` is updated.
- Add-and-sing workflow forwards `solfege_pronunciation_patch=true`.
- Existing derived voice-part context is invalidated when required.
- Failed transforms do not alter session state.
- UI PATCH bypasses the LLM and invokes the shared mutation service.
- MCP modification invokes the same service without an internal HTTP call.
- MCP-originated changes are reflected in the next UI session hydration.
- Direct UI changes are reflected in later LLM dynamic context.
- Stale settings and score revisions return conflicts without mutation.
- Multiple connected clients converge on the highest revision.

### UI Component Tests

- Initial control values are Movable Do and Major.
- User selection sends one PATCH request with expected revisions.
- Programmatic hydration does not send PATCH requests.
- Controls are disabled while a mutation is pending.
- Mode is disabled for Fixed Do and its retained value is restored for Movable Do.
- Failed requests restore server-confirmed settings.
- Successful responses refresh score preview and all control instances.

### Golden Fixtures

```text
tests/fixtures/solfege/
  major_simple.xml
  minor_la.xml
  minor_do.xml
  fixed_chromatic.xml
  key_change.xml
  ties_rests_grace.xml
  existing_multiverse.xml
  complex_multivoice.xml
```

Compare semantic MusicXML structure rather than raw serialization whitespace.

## 21. Acceptance Criteria

1. A simple selected part receives one new lyric verse without changing existing verses.
2. The result reparses and exposes the new verse's first 20 tokens in the score summary.
3. The returned score has the new verse selected and can be synthesized without another reparse.
4. Solfege mapping is deterministic and contains no LLM-generated syllables.
5. Complex raw parts return `complex_target_requires_preparation`.
6. New sessions expose `movable_do + major` in the UI and backend state.
7. A direct UI settings change bypasses the LLM and updates every generated verse atomically.
8. An LLM settings request calls `modify_solfege_settings`, updates every generated verse, and synchronizes the UI from canonical state.
9. Settings changes preserve user-authored verses, generated verse numbers, and the active verse selection.
10. UI hydration never triggers a second mutation request.
11. Retrying the same operation does not append or rewrite verses twice.
12. Source files are never overwritten.
13. Unicode metadata survives the transformation.
14. All unit, schema, API, UI, integration, and golden-fixture tests pass.

## 22. Implementation Sequence

1. Implement pure mapping and MusicXML mutation in `src/musicxml/solfege.py`.
2. Add golden fixtures and unit tests.
3. Implement `src/api/solfege.py` with validation, atomic writes, and reparse.
4. Add generated-verse provenance and multi-verse rewrite tests.
5. Implement the shared settings mutation service and session revisions.
6. Add GET/PATCH solfege settings endpoints.
7. Register both MCP schemas, handlers, and CPU routing.
8. Add orchestrator path injection, idempotency, active-score updates, and MCP-originated settings synchronization.
9. Build `SolfegeSettingsControl` and direct API integration.
10. Update the system prompt for existing-versus-new verses and settings changes.
11. Add end-to-end add-only, add-and-sing, UI-change, and LLM-change tests.

## 23. Deferred Decisions

- Optional score-aware automatic major/minor mode.
- Region-specific minor policies.
- User-selected custom lyric verse numbers.
- Alternative localized syllable sets.
- A UI badge or provenance panel for generated lyric verses.
