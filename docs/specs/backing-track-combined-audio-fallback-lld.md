# Backing Track Combined-Audio MCP Tool LLD

## Goal

Add a separate MCP tool for a demo fallback where the backend returns one combined audio file containing:

- the previously generated singing audio for the current score version
- the newly generated instrumental backing track

This replaces the earlier flag-based idea. The outer LLM should decide whether the user wants:

- pure backing track only, or
- a combined melody + backing-track output

The UI should continue to receive a normal single `audio_url` and should not need multitrack playback support.

## Product Decision

Do not use a backend env flag to switch a single backing-track endpoint between two modes.

Instead:

- keep the existing MCP tool for pure backing track
  - `generate_backing_track`
- add a new MCP tool for combined output
  - `generate_combined_backing_track`

Reason:

- user intent is semantic, not operational
- the outer LLM is already responsible for tool selection
- a separate tool keeps job meaning, logging, progress payloads, and UI labeling clean
- this avoids one tool returning two different artifact types under the same name

## Non-Goals

- no frontend multitrack playback
- no per-track volume sliders
- no advanced mastering, ducking, or stem control
- no time-stretching or beat correction
- no automatic fallback from one tool to the other

## Current State

Current backing-track flow:

1. LLM calls `generate_backing_track`
2. orchestrator starts a `backing_track` job
3. MCP handler calls [generate_backing_track()](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py)
4. backend writes one instrumental file to disk
5. orchestrator returns one `audio_url`

Relevant existing session state:

- [last_successful_singing_render](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/session.py)
- [current_backing_track_audio](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/session.py)

Current prerequisite gating for backing track already exists:

- a successful singing render must exist for the active score version

## Proposed Tool Surface

### Existing Tool

`generate_backing_track`

Purpose:

- generate instrumental backing track only

### New Tool

`generate_combined_backing_track`

Purpose:

- generate instrumental backing track
- mix it with the latest successful singing render for the current score version
- return the mixed file as a single audio artifact

## LLM Tool-Selection Policy

The outer LLM should interpret user intent and choose the tool accordingly.

### Use `generate_backing_track`

When the user asks for:

- a backing track
- accompaniment only
- instrumental only
- karaoke track
- backing music without the melody

### Use `generate_combined_backing_track`

When the user asks for:

- both together
- melody plus backing track
- a demo mix
- one combined audio file
- accompaniment under the singing
- a version that already includes the melody

### Do Not Guess Wrong

If the user intent is ambiguous, default to:

- `generate_backing_track`

Reason:

- instrumental-only output is the current primary feature
- combined output is a fallback/demo-oriented variant

## Preconditions

Both tools require:

1. a successfully uploaded and parsed score
2. a successful singing render for the current score version

The new combined tool additionally requires:

3. the latest successful singing render is readable from local disk or storage
4. the newly generated backing track is readable for mixdown

If prerequisites are not met:

- return a structured MCP error / action-required result
- the outer LLM should explain naturally to the user what is missing

## High-Level Flow

### Pure Backing Track

1. generate instrumental backing track
2. save it
3. return instrumental artifact

### Combined Backing Track

1. generate instrumental backing track
2. resolve the latest valid singing render for the current score version
3. if a pure backing-track WAV already exists for the same latest score version, reuse it
4. otherwise generate the backing track first
5. decode singing and backing audio
6. normalize both to a common format
7. attenuate both tracks to 70%
8. right-pad the shorter waveform with silence if needed
9. sum the waveforms
10. protect against clipping
11. encode mixed output as MP3
12. return mixed artifact

## New MCP Tool Contract

### Name

`generate_combined_backing_track`

### Inputs

Recommended inputs:

- `style_request: string`
- `additional_requirements?: string`
- `file_path: string`
- `output_path: string`

These mirror the existing pure backing-track tool so the orchestrator integration stays parallel.

### Output

```json
{
  "status": "completed",
  "message": "Combined backing track ready.",
  "output_path": "relative/path/to/backing_track_combined.mp3",
  "duration_seconds": 36.0,
  "backing_track_prompt": "Original instrumental backing track...",
  "metadata": {},
  "output_format": "mp3_44100_128",
  "render_variant": "combined"
}
```

Additional details recommended for debug/UI:

- `vocal_gain: 0.7`
- `backing_gain: 0.7`
- `source_singing_duration_seconds`
- `source_backing_duration_seconds`
- `mixed_duration_seconds`

## Audio Mixing Rules

### Decode Format

Decode both sources to:

- sample rate: `44100`
- channels: stereo
- sample format: `float32`

Preferred implementation:

- continue using `ffmpeg-python`
- continue using `numpy`
- continue using `soundfile`

This matches the current audio postprocessing stack already used in [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py).

### Gain Staging

Before sum:

- singing waveform *= `0.7`
- backing waveform *= `0.7`

Then:

- `mixed = vocal + backing`

### Clipping Protection

If the mixed waveform exceeds full scale:

- normalize peak to a safe ceiling, for example `0.98`

This is not intended as mastering. It is only safe mix protection.

### Alignment

For v1 fallback:

- no time-stretching
- no onset alignment
- no beat-detection correction
- if lengths differ, right-pad the shorter waveform with silence

Assumption:

- the singing render and backing track target the same score timing

## Pickup Handling

The combined tool should reuse the final backing-track artifact exactly as produced by the existing backing-track pipeline.

That means:

- if the backing-track generation path prepends pickup silence, mix against that final artifact
- do not invent a second pickup-specific mix rule in the combined tool

Reason:

- pickup handling should remain owned by the backing-track generation layer, not duplicated in the mix layer

## Runtime Ownership

Recommended implementation ownership:

- keep backing-track generation and mixing logic in [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py)

Recommended public runtime surface:

- `generate_backing_track(...)`
- `generate_combined_backing_track(...)`

Internally:

- both may share a common helper that generates the backing-track source artifact first
- the combined variant then performs mixdown

Reason:

- keeps audio-specific logic in one backend module
- avoids leaking waveform/mixing logic into the orchestrator

## Orchestrator Changes

### Existing

Current tool path already supports:

- `generate_backing_track`

### New

Add a parallel flow for:

- `generate_combined_backing_track`

Recommended job semantics:

- new `jobKind`: `combined_backing_track`
- new MCP tool name routed by the orchestrator
- separate user-facing progress message

Suggested messages:

- queued: `Let me create the combined melody and backing track...`
- completed: `Combined backing track ready.`
- failed: `Couldn't finish the combined backing track.`

## Session State Impact

Do not overload `current_backing_track_audio` with two meanings.

Recommended addition:

- `current_combined_backing_track_audio`

Reason:

- pure backing track and combined output are different artifacts
- storing them separately avoids ambiguity
- future UI can choose which artifact to present

Existing state remains:

- `current_backing_track_audio`
- `last_successful_singing_render`

New state:

- `current_combined_backing_track_audio`

## Storage Behavior

Pure backing track:

- keep current behavior
- additionally retain the backing-track WAV artifact in storage after MP3 delivery encoding
- do not delete the WAV after compressing/transcoding to MP3

Combined backing track:

- write final artifact as something like `backing_track_combined.mp3`
- upload only the final returned artifact in storage-enabled environments

For combined generation, also preserve and consult the latest pure backing-track WAV artifact for reuse.

Optional debug outputs when `BACKEND_DEBUG=true`:

- decoded singing WAV
- decoded backing WAV
- final mixed WAV

## UI Contract Impact

The UI still receives:

- one normal `audio_url`
- one normal completed job

But the progress payload should distinguish the new artifact type cleanly.

Recommended progress payload markers:

- `job_kind: "backing_track"` for pure instrumental
- `job_kind: "combined_backing_track"` for combined output

That is better than using one `job_kind` plus a nested `render_variant` string.

## Error Handling

### Pure Backing Track Errors

No change from current design.

### Combined Tool Errors

Fail the job if:

- no successful singing render exists
- the singing render belongs to an old score version
- the singing source cannot be read
- the generated backing source cannot be read
- decode/normalize/mix/encode fails

Recommended internal errors:

- `Combined backing-track generation requires a successful singing render for the current score version.`
- `Could not read the singing audio needed for combined backing-track generation.`
- `Could not mix singing and backing-track audio.`

Recommended user-facing progress failure message:

- `Couldn't finish the combined backing track.`

## MCP Tool Definitions

### Existing Tool

Keep:

- `generate_backing_track`

### New Tool

Add:

- `generate_combined_backing_track`

Tool description should explicitly say:

- generates one mixed output containing both melody and accompaniment
- requires a successful singing render for the current score version

## System Prompt Updates

Update the outer LLM system prompt so it knows:

- `generate_backing_track` is for instrumental-only output
- `generate_combined_backing_track` is for one mixed melody+backing result
- both require a successful singing render for the current score version
- if Dynamic Context shows readiness is satisfied, prefer the tool that matches user intent instead of calling `synthesize` again

The prompt should also mention ambiguous intent handling:

- if user requests "backing track" without asking for melody included, default to `generate_backing_track`

## API / Job Summary

### Pure Backing Track

- MCP tool: `generate_backing_track`
- job kind: `backing_track`
- session artifact: `current_backing_track_audio`

### Combined Backing Track

- MCP tool: `generate_combined_backing_track`
- job kind: `combined_backing_track`
- session artifact: `current_combined_backing_track_audio`

## Implementation Sketch

### New Runtime Helpers

In [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py):

- `_load_audio_waveform(path, sample_rate=44100, channels=2) -> np.ndarray`
- `_pad_waveform_to_length(waveform, frame_count) -> np.ndarray`
- `_mix_vocal_and_backing(vocal, backing, vocal_gain=0.7, backing_gain=0.7) -> np.ndarray`
- `_save_mixed_mp3(waveform, output_path, sample_rate=44100, bitrate="128k") -> dict`
- `_resolve_reusable_backing_track_wav(...)`
- `generate_combined_backing_track(...)`

### Pseudo-Flow

```python
def generate_combined_backing_track(...):
    backing = resolve_reusable_backing_track_wav_for_current_score(...)
    if backing is None:
        backing = generate_backing_track(...)
    singing = resolve_latest_valid_singing_render(...)
    vocal_wave = load_audio_waveform(singing.path)
    backing_wave = load_audio_waveform(backing.wav_path_or_output_path)
    mixed_wave = mix(vocal_wave * 0.7, backing_wave * 0.7)
    save mixed mp3
    return combined result
```

Reuse rule:

- if a pure backing track exists for the same latest score version and a WAV artifact is available, reuse it directly
- do not call ElevenLabs again in that case
- only call ElevenLabs when no reusable backing-track WAV exists

Likely trigger cases:

- user first generated a pure backing track, then later asks for a combined version
- user directly asks for a combined version after singing exists but no backing track has been generated yet

## Test Plan

### Unit Tests

1. `generate_combined_backing_track` with valid singing render
- returns mixed artifact
- file exists
- duration equals max(vocal, backing)

2. Missing singing render
- explicit failure

3. Stale singing render score version
- explicit failure

4. Reuse existing pure backing-track WAV
- combined tool skips ElevenLabs call when reusable WAV exists for current score version

5. No reusable backing-track WAV
- combined tool generates backing track first, then mixes

6. Gain staging
- both tracks attenuated to 70% before sum

7. Clipping protection
- final mix peak stays below full scale

8. Combined tool preserves current backing-track pickup-prepend behavior

### Integration Tests

1. Chat flow for pure backing track
- user asks for accompaniment only
- LLM chooses `generate_backing_track`
- progress payload returns `job_kind: "backing_track"`

2. Chat flow for combined output
- user asks for melody + backing together
- LLM chooses `generate_combined_backing_track`
- progress payload returns `job_kind: "combined_backing_track"`

3. Existing singing bubble isolation still works
- later combined job does not overwrite the earlier singing message

## Risks

### 1. LLM Tool Misclassification

The LLM may choose pure backing when the user meant combined, or vice versa.

Mitigation:

- clear tool descriptions
- explicit system prompt guidance
- default ambiguous requests to pure backing track

### 2. Timing Mismatch

If singing and backing are not tightly aligned, the combined output will expose it.

Mitigation:

- keep pickup handling inside backing-track generation
- use this combined path only as demo fallback

### 3. Session State Ambiguity

If combined output is stored in the same slot as pure backing track, later UI behavior will become confusing.

Mitigation:

- add `current_combined_backing_track_audio`

## Rollout

Phase 1:

- implement separate MCP tool locally
- verify tool selection and combined output on demo score

Phase 2:

- hand UI developer the updated `job_kind` contract

Phase 3:

- use combined tool only when UI dual-playback is unavailable or user explicitly wants one mixed artifact

## Open Questions

1. Should combined output also be exposed for download alongside pure backing track in future UI?

Recommended answer:

- yes, but not required for this backend-only fallback

2. Should the combined tool reuse the exact backing-track prompt and metadata in job details?

Recommended answer:

- yes, plus mix-specific details such as gains and source durations

3. Should a successful combined render also refresh `current_backing_track_audio`?

Recommended answer:

- no
- keep pure backing and combined artifacts separate
