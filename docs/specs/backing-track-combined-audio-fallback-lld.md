# Backing Track Combined-Audio Fallback LLD

## Goal

Provide a backend-only fallback mode for demo use:

- if enabled by config, backing-track generation should return a single mixed audio file
- the mixed file should contain both:
  - the previously generated singing audio for the current score version
  - the newly generated backing track
- both sources should be attenuated to 70% of their original level before mixing
- the UI should continue to receive one normal `audio_url` and should not require simultaneous dual-track playback support

This is explicitly a backup plan for demo readiness. It is not intended to replace the longer-term UX of independent melody and backing-track playback.

## Non-Goals

- no frontend changes
- no multitrack playback in the browser
- no per-track volume controls in UI
- no stem export
- no advanced mastering, limiting, ducking, or tempo correction
- no attempt to time-stretch mismatched renders

## Current State

Current backing-track flow:

1. LLM calls `generate_backing_track`
2. orchestrator starts a `backing_track` job
3. MCP handler calls [generate_backing_track()](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py)
4. backend writes one instrumental file to disk
5. orchestrator stores that file as the job output and returns one `audio_url`

Important existing session state:

- [last_successful_singing_render](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/session.py)
- [current_backing_track_audio](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/session.py)

Important current backing-track job output behavior:

- job output path currently assumes one MP3 artifact:
  - [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L663)
- UI consumes only one `audio_url` from job progress:
  - [orchestrator.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/orchestrator.py#L615)

## Proposed Config

Add one new backend setting:

- `BACKEND_BACKING_TRACK_OUTPUT_COMBINED_AUDIO`

Semantics:

- `false` or unset:
  - current behavior
  - backing-track job returns instrumental-only audio
- `true`:
  - after backing track is generated, backend mixes singing + backing track
  - returned `audio_url` points to the mixed file

Optional future extension, not required for v1:

- `BACKEND_BACKING_TRACK_MIX_GAIN=0.7`
- `BACKEND_BACKING_TRACK_VOCAL_GAIN=0.7`

For this fallback design, both gains are fixed at `0.7`.

## Preconditions

Combined-audio output is only valid if all of the following are true:

1. backing-track prerequisite is satisfied
2. a `last_successful_singing_render` exists for the current score version
3. that singing audio file is still readable from local disk or storage
4. the backing track finishes successfully

If combined mode is enabled but the singing render is missing or unreadable:

- fail the backing-track job
- surface a clear backend error message to the UI
- do not silently fall back to instrumental-only output

Reason:

- for demo debugging, explicit failure is safer than producing the wrong artifact without notice

## High-Level Flow

### Normal Mode

1. generate backing track
2. save instrumental file
3. return instrumental file as job audio

### Combined Mode

1. generate backing track
2. locate the current session's valid singing render
3. decode both audio files
4. align them to a common sample rate / channel layout
5. scale both waveforms to 70%
6. pad shorter waveform with silence to match the longer one
7. sum the waveforms
8. apply peak protection to avoid hard clipping
9. encode final mixed output as MP3
10. return the mixed file as the job audio

## Design Details

### 1. Source Selection

The mixed output must use:

- singing source:
  - from `last_successful_singing_render`
- backing source:
  - the newly generated backing-track artifact from the same job

The singing source must be validated against the active score version before mix.

Required source fields from session state:

- `path`
- `storage_path` if applicable
- `score_version`
- `duration_seconds`

If the render belongs to an old score version:

- reject the combined mix attempt

## 2. Audio Decode

Use backend-side decode to `float32` waveform for both tracks.

Preferred implementation:

- continue using `ffmpeg-python` and existing audio libs already present in [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py)
- decode both sources to:
  - sample rate: `44100`
  - channels: stereo
  - sample format: `float32`

Rationale:

- current backing-track code already uses `ffmpeg`, `numpy`, and `soundfile`
- this avoids introducing a second audio toolchain

## 3. Alignment Logic

Assumption for fallback mode:

- the singing render and generated backing track are intended for the same score timing

Mixing logic:

1. decode both to stereo `float32`
2. if lengths differ, right-pad the shorter one with silence
3. no time-stretching
4. no onset detection
5. no beat alignment correction

Why this is acceptable for the backup plan:

- backend already controls pickup handling for backing tracks
- both tracks target the same score duration
- simple overlay is fast and robust enough for a demo fallback

## 4. Gain Staging

Apply fixed pre-mix attenuation:

- singing waveform *= `0.7`
- backing waveform *= `0.7`

Then sum:

- `mixed = vocal + backing`

Peak protection:

- if `max(abs(mixed)) > 1.0`
  - normalize the mixed waveform down to a safe ceiling, for example `0.98`

Reason:

- two 70% tracks can still clip when summed
- a simple peak normalization is enough for this fallback

This is not intended to be transparent mastering; it is just safe mix output.

## 5. Output Artifact Rules

### Default Instrumental Mode

- output file remains `backing_track.mp3`

### Combined Mode

Recommended output file name:

- `backing_track_combined.mp3`

Reason:

- makes the artifact type obvious in logs, storage, and debugging

The returned job should still expose:

- one `audio_url`

But the `details` block should include extra metadata:

- `render_variant: "combined"`
- `vocal_gain: 0.7`
- `backing_gain: 0.7`
- `source_singing_duration_seconds`
- `source_backing_duration_seconds`
- `mixed_duration_seconds`

## 6. Storage Behavior

Local-only mode:

- write the combined MP3 to the session jobs directory

Storage-enabled mode:

- upload only the final returned artifact
- do not upload intermediate WAV/PCM decode artifacts unless `BACKEND_DEBUG=true`

Optional debug behavior:

- when debug is enabled, retain:
  - decoded backing WAV
  - decoded singing WAV
  - final mixed WAV

This is useful for demo troubleshooting but should not be relied on in production.

## 7. Orchestrator Impact

Only the backing-track job path changes.

Required behavior:

- if combined mode is disabled:
  - no change
- if combined mode is enabled:
  - after backing-track generation succeeds, orchestrator or backing-track runtime must perform mix
  - job output points to the combined file

Recommended ownership:

- keep mix logic in [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py)

Reason:

- it already owns backing-track artifact generation
- it already owns pickup-specific postprocessing
- it keeps audio postprocessing in one place instead of leaking into orchestrator

## 8. Session State Impact

No new session concept is required for v1 fallback.

Current state can remain:

- `last_successful_singing_render`
- `current_backing_track_audio`

But in combined mode, `current_backing_track_audio` will actually point to:

- the combined artifact returned to the client

This is acceptable for the fallback plan, but should be documented clearly:

- in combined mode, "backing track audio" means "combined melody + backing artifact"

Optional future improvement:

- add `current_combined_backing_track_audio`

Not required for v1.

## 9. Error Handling

When combined mode is enabled, fail the job if:

- singing source is missing
- singing source belongs to old score version
- singing source file cannot be loaded
- backing track cannot be decoded
- sample-rate/channel normalization fails
- final MP3 encode fails

Recommended user-facing message:

- `Couldn't finish the combined backing track.`

Recommended internal error detail examples:

- `Combined backing-track output requires a successful singing render for the current score version.`
- `Could not read the singing audio needed for combined backing-track output.`
- `Could not mix the singing and backing-track audio.`

## 10. UI Contract

No UI contract change is required.

The frontend still receives:

- one `audio_url`
- one normal completed audio job

So the current player and download behavior remain unchanged.

This is the main value of the fallback design.

## API / Config Summary

### New Env

```bash
BACKEND_BACKING_TRACK_OUTPUT_COMBINED_AUDIO=false
```

### Job Details Additions

```json
{
  "render_variant": "combined",
  "vocal_gain": 0.7,
  "backing_gain": 0.7,
  "source_singing_duration_seconds": 34.0,
  "source_backing_duration_seconds": 34.0,
  "mixed_duration_seconds": 36.0
}
```

## Implementation Sketch

### New Helper Surface

In [backing_track.py](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/backing_track.py):

- `_should_output_combined_audio(settings) -> bool`
- `_load_audio_waveform(path, sample_rate=44100, channels=2) -> np.ndarray`
- `_pad_waveform_to_length(waveform, frame_count) -> np.ndarray`
- `_mix_vocal_and_backing(vocal, backing, vocal_gain=0.7, backing_gain=0.7) -> np.ndarray`
- `_save_mixed_mp3(waveform, output_path, sample_rate=44100, bitrate="128k") -> dict`

### Updated Runtime Flow

Pseudo-flow:

```python
backing = generate backing track as before

if not combined_mode:
    return backing

singing = resolve current valid singing render
vocal_wave = load singing audio
backing_wave = load backing audio
mixed_wave = mix(vocal_wave * 0.7, backing_wave * 0.7)
save mixed mp3
return combined result
```

## Test Plan

### Unit Tests

1. Combined mode disabled
- backing-track output remains instrumental-only

2. Combined mode enabled with valid singing render
- returned artifact is mixed output
- file exists
- duration equals max(vocal, backing)

3. Combined mode enabled but no singing render
- job fails with explicit error

4. Combined mode enabled but stale singing score version
- job fails with explicit error

5. Gain staging
- both tracks are attenuated before sum

6. Clipping protection
- mixed output is peak-limited / normalized below 1.0

### Integration Tests

1. End-to-end chat flow:
- synthesize singing
- request backing track with combined mode on
- completed job returns one `audio_url`
- audio download succeeds

2. Existing singing bubble isolation still works
- prior singing message remains bound to its original job-specific progress URL
- combined backing-track job does not overwrite the old singing message

## Risks

### 1. Timing mismatch

If the backing track drifts or phrase alignment is imperfect, the mixed output will expose that more clearly.

Mitigation:

- this fallback is only for demo use
- keep using the pickup-prepend logic already implemented

### 2. Clipping / muddy mix

Two summed tracks may sound dense.

Mitigation:

- fixed 70% attenuation
- peak normalization after sum

### 3. Source availability

If the stored singing audio is unavailable, combined mode cannot succeed.

Mitigation:

- explicit failure
- no silent fallback

## Rollout

Phase 1:

- implement env-gated combined mode locally
- verify on demo score

Phase 2:

- if UI dual-playback is not ready, enable the env for demo/prod

Phase 3:

- once UI dual playback exists, disable combined mode by default

## Open Questions

1. Should combined mode reuse the pickup-prepended backing-track output before mix, or mix first and prepend silence after mix?

Recommended answer:

- reuse the final backing-track output exactly as generated, then mix with singing

2. Should the final file still be stored under `current_backing_track_audio`?

Recommended answer:

- yes for v1, to minimize model changes

3. Should the combined output message say "Backing track ready" or "Combined melody + backing track ready"?

Recommended answer:

- keep the user-facing copy simple unless demo feedback shows confusion
