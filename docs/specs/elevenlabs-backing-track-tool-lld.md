# ElevenLabs Backing Track Tool LLD

## 1. Goal

Add a new LLM-callable MCP tool, `generate_backing_track`, that:

1. can be called only after a successful singing synthesis has completed for the current session
2. analyzes the current score using deterministic code
3. uses the configured backend LLM to turn score metadata plus user style intent into a high-quality ElevenLabs music prompt
4. calls the ElevenLabs Music API to generate an instrumental backing track
5. returns progress and final audio in the existing studio UI

This document is for design review only. No runtime changes are included yet.

## 2. Context

Relevant existing implementation:

1. Score parsing is deterministic via `parse_score(...)` in `src/api/score.py`.
2. A proof-of-concept metadata extractor already exists in `src/api/backing_track_prompt.py`.
3. Current LLM-callable MCP tools are defined in `src/mcp/tools.py`, implemented in `src/mcp/handlers.py`, and exposed in `src/mcp_server.py`.
4. The chat orchestrator only allows a small tool set today: `reparse`, `preprocess_voice_parts`, `synthesize` in `src/backend/orchestrator.py`.
5. Singing synthesis already writes progress jobs, session audio, and signed playback URLs that the UI can poll and play:
   - `src/backend/orchestrator.py`
   - `src/backend/job_store.py`
   - `src/backend/session.py`
   - `src/backend/main.py`
   - `ui/src/api.ts`

Important architectural point:

The POC did not use an LLM to parse MusicXML. It used deterministic parsing plus prompt templating. That should remain true in production. The new LLM step should only write the ElevenLabs music prompt, not infer raw score structure directly from XML.

## 3. User Story

Example flow:

1. User uploads a score.
2. User reparses / preprocesses / synthesizes a singing render as today.
3. User asks: `Generate a lo-fi backing track for this song.`
4. LLM calls `generate_backing_track`.
5. Backend verifies that a valid singing render already exists for the active score.
6. Backend extracts structured score metadata.
7. Backend calls the configured backend LLM with a dedicated backing-track prompt-writer system prompt.
8. Backend sends the generated prompt to ElevenLabs `music.compose(...)` with `force_instrumental=true`.
9. UI shows progress, then receives playable/downloadable backing-track audio.

## 4. Scope

In scope:

1. New MCP tool definition and handler.
2. Orchestrator allowlist and tool execution support.
3. Validation that the prerequisite singing render exists.
4. Deterministic score analysis for backing-track metadata.
5. Internal LLM prompt-writing step.
6. ElevenLabs compose call for instrumental music.
7. Persisting and serving the generated backing-track audio.
8. UI support to render backing-track progress and final playback.

Out of scope for v1:

1. Stem separation or mixing vocal + backing track together.
2. Multi-track accompaniment export.
3. Native composition-plan generation as the primary path.
4. Chord inference from raw notes when MusicXML lacks `<harmony>` tags.
5. Credit and pricing policy for backing tracks.
6. Regeneration history / multiple backing-track variants per session.

## 5. External API Constraints

This section is based on the current ElevenLabs docs:

1. Compose music: https://elevenlabs.io/docs/api-reference/music/compose
2. Create composition plan: https://elevenlabs.io/docs/api-reference/music/create-composition-plan
3. Music quickstart: https://elevenlabs.io/docs/developers/guides/cookbooks/music/quickstart
4. Eleven Music prompting guidance: https://elevenlabs.io/docs/best-practices/prompting/eleven-music/

Confirmed behaviors:

1. `POST /v1/music` accepts either `prompt` or `composition_plan`.
2. `force_instrumental=true` is available only with `prompt`, not with `composition_plan`.
3. `music_length_ms` can be supplied with prompt-based generation.
4. `output_format` is a query parameter.

Design implication:

For v1, this feature should use prompt-based composition, not composition-plan-based composition.

Reason:

1. user explicitly wants the LLM to write a natural music prompt from score metadata plus style request
2. `force_instrumental=true` is only supported with prompt input
3. prompt-based compose is the shortest path from current POC to hackathon-ready feature

Composition plans remain a future enhancement path, not the primary v1 path.

## 6. Design Summary

Add a new tool:

1. `generate_backing_track`

Execution path:

1. LLM calls `generate_backing_track(style_request=..., additional_requirements=...)`
2. Orchestrator validates session state
3. Backend extracts structured score metadata from current score + raw MusicXML
4. Backend invokes a dedicated internal prompt-writer LLM turn
5. Backend calls ElevenLabs `music.compose(...)`
6. Backend stores resulting MP3 as the current session backing track
7. Backend exposes progress and final `audio_url` through the existing polling model

Key principle:

The LLM must not parse MusicXML directly. The backend should pass the LLM a deterministic metadata payload and ask it only to translate that into a better prompt for ElevenLabs.

## 7. Preconditions and Gating

The tool must only succeed when all of the following are true:

1. a session exists
2. a score has been uploaded and parsed
3. the session has a current score
4. at least one singing synthesis job has completed successfully for the session
5. that successful singing synthesis corresponds to the currently active score version

Why the last condition matters:

If the user reparses or preprocesses after synthesis, the old singing render may no longer align with the current score. Backing track generation should not silently proceed from stale session state.

### 7.1 Proposed Session Metadata Extension

Persist additional synthesis context after a successful singing render:

1. `last_successful_singing_render`
   - `score_version`
   - `job_id`
   - `audio_path`
   - `duration_seconds`
   - `voicebank`
   - `part_index`
   - `voice_part_id`

Current session state only stores `current_audio`, which is insufficient to distinguish:

1. singing audio vs future backing-track audio
2. which score version the audio belongs to

### 7.2 Failure Contract

If any precondition fails, the tool should not throw a low-level exception to the LLM. It should return a typed `action_required` or `error` payload with a concise message for natural-language explanation.

Recommended cases:

1. `no_score_uploaded`
   - `Please upload a score first.`
2. `no_successful_singing_render`
   - `Generate a singing track first, then I can create a backing track from the same score.`
3. `stale_singing_render_for_current_score`
   - `The score has changed since the last singing render. Please synthesize the current score first, then retry backing track generation.`
4. `missing_style_request`
   - optional soft failure if the user request is too vague
   - or default to a simple supportive instrumental style

Recommendation:

Do not require a separate explicit confirmation step. If singing audio exists for the active score, the backing-track tool can run directly.

## 8. New MCP Tool Contract

### 8.1 Tool Name

`generate_backing_track`

### 8.2 Intended Caller

1. primary: chat orchestrator via LLM tool call
2. secondary: direct MCP invocation in development/tests

### 8.3 Proposed Input Schema

```json
{
  "type": "object",
  "properties": {
    "style_request": {
      "type": "string",
      "description": "Natural-language style or genre request such as 'lo-fi', 'light jazz trio', or 'EDM J-pop'."
    },
    "additional_requirements": {
      "type": ["string", "null"],
      "description": "Optional extra musical requirements from the user, such as instrumentation, mood, or arrangement constraints."
    },
    "output_format": {
      "type": ["string", "null"],
      "description": "Optional ElevenLabs output format. Default: mp3_44100_128."
    },
    "seed": {
      "type": ["integer", "null"],
      "description": "Optional ElevenLabs music seed."
    }
  },
  "required": ["style_request"],
  "additionalProperties": false
}
```

Notes:

1. The tool should not accept `score` from the LLM. Backend must inject current session score context.
2. The tool should not accept `music_length_ms` from the LLM in v1. Backend should derive it from score duration to preserve structure.

### 8.4 Proposed Output Contract

Tool returns one of:

1. `status=queued` or `running`
2. `status=completed`
3. `status=action_required`
4. `status=error`

Representative shape:

```json
{
  "status": "completed",
  "job_kind": "backing_track",
  "audio_url": "/sessions/<session_id>/audio?file=backing_track.mp3",
  "duration_seconds": 34.0,
  "backing_track_prompt": "...",
  "metadata": {
    "key_signature": "G major",
    "time_signature": "4/4",
    "bpm": 120,
    "measure_count": 17
  }
}
```

`backing_track_prompt` should be retained in logs/session metadata for debugging, but not necessarily shown to the end user by default.

## 9. Deterministic Score Analysis

Use deterministic code to extract score metadata before any LLM step.

### 9.1 Baseline Module

Evolve `src/api/backing_track_prompt.py` into a more general score-analysis module for backing-track generation.

Current POC already extracts:

1. title
2. key signature
3. time signature
4. BPM
5. measure count
6. duration
7. pickup information
8. chord progression from `<harmony>` tags

### 9.2 Proposed Metadata Payload

The prompt-writer LLM should receive a stable structured payload such as:

```json
{
  "title": "Happy Birthday to You - 4/4 - G key",
  "key_signature": "G major",
  "time_signature": "4/4",
  "bpm": 120,
  "measure_count": 17,
  "duration_seconds": 34.0,
  "duration_mmss": "0:34",
  "pickup": {
    "has_pickup": true,
    "starts_on_beat": 4,
    "second_bar_downbeat_note": "E4"
  },
  "chord_progression": [
    { "measure": 2, "chords": ["G"] },
    { "measure": 3, "chords": ["D"] }
  ]
}
```

### 9.3 Data Sources

Use both:

1. parsed score JSON from `parse_score(...)`
2. direct MusicXML XML traversal

Recommended field ownership:

1. BPM, note timing, pickup analysis:
   - from parsed score JSON
2. key signature, time signature, harmony tags:
   - from raw MusicXML XML

### 9.4 Known Limitation

Chord progression is currently only reliable when the score contains `<harmony>` tags.

Design decision for v1:

1. if harmony tags exist, include measure-level chord progression
2. if harmony tags do not exist, omit `chord_progression` instead of inventing unstable harmonic analysis
3. prompt-writer LLM must be told whether harmony is authoritative or unavailable

## 10. Internal LLM Prompt Writer

This feature introduces a second LLM usage mode:

1. outer LLM call:
   - normal chat orchestrator
   - decides whether to call `generate_backing_track`
2. inner LLM call:
   - deterministic backend sub-step
   - writes the final ElevenLabs music prompt

### 10.1 Why an Inner LLM Call Is Needed

The final prompt should combine:

1. deterministic score metadata
2. user genre/style request
3. arrangement guidance
4. ElevenLabs-friendly phrasing

That is better handled by a prompt-writing LLM step than by pure string templating.

### 10.2 Provider Choice

Use the existing configured backend LLM provider:

1. `LLM_PROVIDER=gemini`
2. `LLM_PROVIDER=openai`

Reason:

This keeps the feature provider-agnostic and reuses the same LLM factory infrastructure already being built for the hackathon.

### 10.3 Prompt-Writer Contract

Add a dedicated backing-track system prompt, for example:

`src/backend/config/backing_track_prompt_writer_system_prompt.txt`

Responsibilities:

1. preserve structural metadata exactly
2. preserve tempo, duration, and pickup alignment
3. preserve chord progression when supplied
4. generate a concise but musically rich prompt for ElevenLabs
5. force instrumental-only phrasing
6. include user style request and extra requirements naturally

Explicit instructions should include:

1. do not invent a new measure count
2. do not change BPM
3. do not change key unless user explicitly asks for transposition and backend supports it
4. mention pickup/downbeat alignment when pickup exists
5. if chord progression is unavailable, do not hallucinate exact chords

### 10.4 Output Shape

The inner LLM call should return strict JSON:

```json
{
  "prompt": "Create an instrumental lo-fi backing track..."
}
```

Do not let the inner LLM call tools. This is a no-tools generation path.

## 11. ElevenLabs Compose Step

### 11.1 Transport Choice

Use the Python ElevenLabs SDK already added to the repo.

### 11.2 Request Shape

Recommended v1 call:

```python
client.music.compose(
    prompt=generated_prompt,
    music_length_ms=derived_length_ms,
    model_id=settings.elevenlabs_music_model,
    force_instrumental=True,
    output_format=settings.elevenlabs_music_output_format,
    seed=seed_if_provided,
)
```

### 11.3 Derived Parameters

1. `music_length_ms`
   - derived from score duration
   - round to nearest integer milliseconds
2. `force_instrumental`
   - always `True`
3. `output_format`
   - default `mp3_44100_128`
4. `model_id`
   - default `music_v1`

### 11.4 Why Not Composition Plans in v1

Even though ElevenLabs supports composition plans, v1 should use prompt-based compose because:

1. the hackathon feature goal is prompt generation from score metadata
2. `force_instrumental` is prompt-only
3. prompt-based generation is simpler to review and iterate quickly

## 12. Orchestrator Integration

### 12.1 LLM Tool Allowlist

Extend orchestrator allowlist from:

1. `reparse`
2. `preprocess_voice_parts`
3. `synthesize`

to:

4. `generate_backing_track`

### 12.2 Execution Order Policy

Add a system-prompt rule:

1. do not call `generate_backing_track` before a successful singing synthesis for the active score
2. if no singing render exists yet, tell the user you need to synthesize the singing track first

### 12.3 Tool Execution Behavior

`generate_backing_track` should behave more like `synthesize` than `preprocess_voice_parts`:

1. long-running
2. asynchronous job
3. returns `chat_progress` first
4. final audio is delivered via progress polling

### 12.4 Proposed Job Kind

Add:

1. `jobKind = "backing_track"`

This lets UI distinguish backing-track jobs from singing synthesis jobs.

## 13. Session, Job, and Artifact Persistence

## 13.1 Problem

Current session model only stores a single `current_audio`.

That is insufficient once the session may contain:

1. singing audio
2. backing-track audio

### 13.2 Proposed Session Model Extension

Add:

1. `current_singing_audio`
2. `current_backing_track_audio`
3. `last_successful_singing_render`

Do not overload `current_audio` forever if both media types need to coexist in the UI.

### 13.3 Compatibility Plan

Short-term compatibility option:

1. keep `current_audio` for singing render
2. add `current_backing_track_audio` for the new feature

This minimizes regression risk for the existing singing playback path.

### 13.4 Storage Path Convention

Use a deterministic session-local path such as:

1. `data/sessions/<session_id>/backing_track.mp3`

If cloud storage is enabled, mirror current synthesis behavior:

1. upload to session-scoped storage path
2. issue signed playback token through existing `/sessions/{session_id}/audio` route

## 14. UI Contract

### 14.1 Current State

The UI can already render:

1. `chat_progress`
2. `chat_audio`
3. progress polling with `audio_url`

### 14.2 Recommended v1 UI Behavior

Reuse the existing progress player pattern with minimal extension:

1. show a progress card for backing-track generation
2. when complete, show an audio player and download icon
3. optionally label it as `Backing track`

### 14.3 Progress Payload Extension

Include:

1. `job_kind = "backing_track"`
2. optional `details.media_type = "backing_track"`

This allows UI copy and icons to differ without changing the basic transport contract.

## 15. Error Handling

Error classes:

1. precondition failure
2. metadata extraction failure
3. prompt-writer LLM failure
4. ElevenLabs API auth failure
5. ElevenLabs API validation failure
6. audio persistence failure

Recommended user-facing behavior:

1. precondition failures:
   - return `action_required`
2. transient provider failures:
   - return `error` with concise natural-language explanation
3. invalid style request:
   - either normalize to a simpler prompt or return `action_required`

Recommended logging:

1. extracted metadata summary
2. generated backing-track prompt
3. ElevenLabs request parameters excluding secrets
4. ElevenLabs response status and request ID

## 16. Configuration

Add ElevenLabs music-specific settings, separate from generic API key settings:

1. `ELEVENLABS_API_KEY`
2. `ELEVENLABS_MUSIC_MODEL`
3. `ELEVENLABS_MUSIC_OUTPUT_FORMAT`
4. `ELEVENLABS_MUSIC_TIMEOUT_SECONDS`

Recommended defaults:

1. `ELEVENLABS_MUSIC_MODEL=music_v1`
2. `ELEVENLABS_MUSIC_OUTPUT_FORMAT=mp3_44100_128`
3. `ELEVENLABS_MUSIC_TIMEOUT_SECONDS=180`

No new LLM-provider env is needed for the backing-track prompt writer. It should use the existing backend LLM provider selection.

## 17. Tests

### 17.1 Deterministic Unit Tests

1. metadata extraction for known MusicXML fixtures
2. pickup detection
3. harmony-tag extraction
4. no-harmony fallback behavior
5. prompt-writer input payload assembly

### 17.2 Tool Contract Tests

1. `generate_backing_track` rejects when no score exists
2. rejects when no singing render exists
3. rejects when current score version is newer than the last singing render
4. returns progress job when prerequisites are met

### 17.3 Integration Tests

Mock:

1. inner LLM prompt writer
2. ElevenLabs client

Verify:

1. prompt writer receives deterministic metadata
2. ElevenLabs compose is called with `force_instrumental=true`
3. returned audio artifact is persisted and exposed via progress/audio URL

### 17.4 Live Tests

Optional gated live test once a paid ElevenLabs Music API key is available.

## 18. Rollout Plan

1. land deterministic score-analysis extraction cleanup
2. add MCP tool schema and handler with precondition checks only
3. add internal prompt-writer flow behind the tool
4. add ElevenLabs compose integration
5. extend UI to label backing-track progress/audio
6. validate with one simple song and one multi-style regression set

## 19. Open Questions

1. Should backing-track generation consume credits, and if yes, under what pricing model?
2. Should v1 allow multiple backing-track variants per session, or only keep the latest one?
3. Should the user be allowed to request backing track before singing render if the score is already parsed and preprocessed, or is the successful singing render requirement intentionally strict?
4. Should no-harmony scores be blocked, or should v1 proceed with prompts that omit explicit chord progression?
5. Should the UI eventually support playing singing audio and backing track side-by-side in the same message group?

## 20. Recommended v1 Decisions

For hackathon speed, I recommend:

1. require a successful singing render for the current score version
2. use prompt-based ElevenLabs compose, not composition plans
3. keep deterministic metadata extraction
4. use the backend LLM only for prompt writing
5. support exactly one latest backing-track artifact per session
6. treat missing harmony tags as non-fatal and omit exact chord progression from the prompt

## 21. V1.1 Extension: LLM-Assisted Chord Deduction

This section supplements v1 with a fallback path for scores that do not contain explicit `<harmony>` tags.

### 21.1 Goal

When MusicXML has no usable harmony annotations, attempt to infer a practical backing-track chord progression from the notes using the backend LLM.

This is a fallback only.

Priority order should remain:

1. explicit `<harmony>` tags from MusicXML
2. LLM-assisted chord deduction from deterministic note summaries
3. no chord progression in prompt if deduction is too uncertain

### 21.2 Why LLM Fallback Instead of Pure Algorithm in V1.1

For hackathon scope, LLM fallback is acceptable because:

1. many lead-sheet or piano-vocal scores omit `<harmony>` tags
2. deterministic harmonic analysis from arbitrary polyphonic note clusters is non-trivial
3. the LLM can often infer a reasonable functional progression from compact per-measure note summaries plus key and meter

But the LLM must not see raw MusicXML only and improvise freely. The backend should still preprocess the score into a structured harmonic-analysis payload.

### 21.3 Input to the Chord-Deduction LLM

Do not pass raw full MusicXML as the only input.

Instead, build a deterministic condensed payload per measure, for example:

```json
{
  "title": "Happy Birthday to You - 4/4 - G key",
  "key_signature": "G major",
  "time_signature": "4/4",
  "pickup": {
    "has_pickup": true,
    "starts_on_beat": 4
  },
  "measures": [
    {
      "measure": 2,
      "note_summary": ["G4", "B4", "D5"],
      "bass_summary": ["G3"],
      "strong_beat_notes": ["G4", "B4"],
      "duration_profile": "mostly tonic triad on strong beats"
    }
  ]
}
```

Recommended deterministic preprocessing:

1. collect sounding pitches by measure
2. collect bass or lowest pitch candidates by measure
3. collect notes on strong beats separately
4. preserve key signature, time signature, and pickup status
5. optionally include previous/next measure context windows

This keeps the LLM grounded in normalized musical evidence rather than loose XML text.

### 21.4 Proposed Internal LLM Task

Add a second dedicated internal prompt for harmonic inference, for example:

`src/backend/config/backing_track_chord_inference_system_prompt.txt`

Responsibilities:

1. infer one practical accompaniment chord or short chord sequence per measure
2. prefer simple, musically plausible backing-track harmony
3. respect the known key unless there is strong evidence of tonicization or modal borrowing
4. use slash chords only when bass evidence clearly supports them
5. return `unknown` instead of hallucinating when evidence is weak

Strict output contract:

```json
{
  "confidence": "high",
  "measures": [
    { "measure": 2, "chords": ["G"] },
    { "measure": 3, "chords": ["D"] }
  ],
  "notes": "Brief optional rationale."
}
```

### 21.5 Confidence and Acceptance Rules

The backend should not trust LLM chord output unconditionally.

Accept the inferred progression only if:

1. the response is valid JSON
2. measure numbers are within score bounds
3. chord symbols parse successfully under a restricted chord grammar
4. confidence is not `low`
5. coverage is sufficient for the score segment being analyzed

If those checks fail:

1. discard inferred harmony
2. proceed without explicit chord progression in the final music prompt

### 21.6 Chord Symbol Grammar

Restrict accepted inferred chord names to a conservative grammar such as:

1. root: `A` to `G`
2. accidentals: `b`, `#`
3. suffixes:
   - major implied
   - `m`
   - `7`
   - `maj7`
   - `m7`
   - `dim`
   - `aug`
   - `m7b5`
4. optional slash bass:
   - `C/E`
   - `D/F#`

Do not accept exotic free-form text from the LLM in the chord field.

### 21.7 Where It Fits in the Pipeline

Updated metadata flow:

1. parse score deterministically
2. read explicit `<harmony>` tags
3. if present, use them directly
4. if absent, build chord-inference evidence payload
5. call internal chord-deduction LLM
6. validate inferred chords
7. attach accepted progression to score metadata
8. call the backing-track prompt-writer LLM
9. call ElevenLabs compose

So v1.1 adds one extra internal LLM sub-step before prompt writing, only when harmony tags are missing.

### 21.8 Prompt-Writer Interaction

The prompt-writer LLM should be told the provenance of the chord progression:

1. `source = explicit_harmony_tags`
2. `source = llm_inferred_from_notes`
3. `source = unavailable`

Behavior:

1. if `explicit_harmony_tags`, treat chords as authoritative
2. if `llm_inferred_from_notes`, use them conservatively and avoid overstating certainty
3. if `unavailable`, do not mention exact chord progression

### 21.9 Risks

Main risks:

1. hallucinated harmony on ambiguous measures
2. over-complex jazz-like deductions for simple songs
3. unstable measure-by-measure output across repeated runs
4. bad slash-chord guesses from thin bass evidence

Mitigations:

1. keep the input highly structured
2. ask for the simplest plausible accompaniment harmony
3. constrain output schema and chord grammar
4. reject low-confidence or invalid results
5. allow prompt-writer to omit exact progression when confidence is weak

### 21.10 Recommended V1.1 Scope

For hackathon speed, v1.1 should:

1. infer at most one or two chords per measure
2. target simple tonal songs first
3. avoid advanced reharmonization
4. prefer stable diatonic accompaniment language
5. fall back safely to no explicit progression if inference is uncertain

### 21.11 Test Plan Additions

Add tests for:

1. no-`<harmony>` score produces structured inference payload
2. valid mocked LLM chord response is accepted and propagated
3. invalid chord response is rejected
4. low-confidence response is rejected
5. prompt-writer receives chord provenance metadata

### 21.12 Recommended Design Decision

I recommend recording both:

1. `chord_progression`
2. `chord_progression_source`

Possible values:

1. `explicit_harmony_tags`
2. `llm_inferred_from_notes`
3. `unavailable`

That keeps downstream prompt-writing and debugging straightforward.
