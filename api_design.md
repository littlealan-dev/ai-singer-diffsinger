# SVS Backend API Design

This document defines the public APIs for the Singing Voice Synthesis backend. These APIs are designed to be called by an LLM orchestrator via MCP.

---

## Design Philosophy

1. **LLM as Orchestrator**: The LLM understands musical terminology and decides which APIs to call
2. **JSON as Universal Format**: Score data is JSON — the LLM can read, reason about, and modify it
3. **Simplified Public Surface**: MCP only exposes parse/modify/synthesize/save + metadata; detailed steps are internal
4. **Code Execution for Flexibility**: Complex score modifications are done via Python code, not specialized APIs

---

## API Overview (Public MCP Tools)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           USER / LLM                                    │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            MCP SERVER                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 1: parse_score        │  MusicXML → Score JSON                    │
│  UTILITY: modify_score      │  Execute Python code on Score JSON        │
│  CONVENIENCE: synthesize    │  Score → Audio (runs steps 2-6 internally)│
│  OUTPUT: save_audio         │  Waveform → File                          │
│  METADATA: list_voicebanks  │  List available voices                    │
│  METADATA: get_voicebank_info │  Query voice capabilities               │
└─────────────────────────────────────────────────────────────────────────┘
```

Pipeline steps (`phonemize`, `align_phonemes_to_notes`, `predict_durations`, `predict_pitch`,
`predict_variance`, `synthesize_audio`) remain internal and are not exposed via `tools/list`.

---

## MCP Tool Schemas

The MCP server returns both input and output schemas for each exposed tool via:
`tools/list`. These schemas are the authoritative contract for public MCP calls.

---

## Step 1: `parse_score`

Parse a MusicXML file into a JSON structure.

| Attribute | Description |
|-----------|-------------|
| **Input** | `file_path`: Path to MusicXML file |
| | `part_id`: Part ID to extract (optional) |
| | `part_index`: Part index to extract (optional) |
| | `verse_number`: Verse number to select (optional) |
| **Output** | Score as JSON dict (see schema below), including `score_summary` |
| **Purpose** | Convert music notation to LLM-readable format |

**Input example:**
```
parse_score("assets/test_data/amazing-grace-satb-verse1.xml")
```

**Output example:**
```json
{
  "title": "Amazing Grace",
  "tempos": [{"offset_beats": 0.0, "bpm": 120.0}],
  "parts": [
    {
      "notes": [
        {
          "offset_beats": 0.0,
          "duration_beats": 1.0,
          "measure_number": 1,
          "voice": "1",
          "pitch_midi": 69,
          "pitch_hz": 440.0,
          "lyric": "A-",
          "syllabic": "begin",
          "lyric_is_extended": false,
          "is_rest": false,
          "tie_type": null
        }
      ]
    }
  ],
  "structure": {"repeats": [], "endings": [], "jumps": []},
  "score_summary": {
    "title": "Amazing Grace",
    "composer": "John Newton",
    "parts": [
      {
        "part_id": "P1",
        "part_name": "Soprano",
        "has_lyrics": true
      }
    ],
    "available_verses": ["1"]
  }
}
```

---

## Utility: `modify_score`

Execute Python code to modify the score JSON.

| Attribute | Description |
|-----------|-------------|
| **Input** | `score`: Score JSON dict |
| | `code`: Python code string |
| **Output** | Modified score JSON dict |
| **Purpose** | Apply arbitrary transformations (transpose, dynamics, etc.) |

**Available in code context:**
- `score` — The score dict (mutable)
- `math` — Python math module
- Basic builtins: `len`, `range`, `enumerate`, `min`, `max`, `round`, `sorted`

**NOT available (for security):**
- File I/O, network, imports, subprocess

**Example — Crescendo at bar 12:**
```python
modify_score(score, """
bar_12 = [n for n in score['parts'][0]['notes'] if n['measure_number'] == 12]
for i, note in enumerate(bar_12):
    note['velocity'] = 0.35 + 0.45 * (i / max(1, len(bar_12) - 1))
""")
```

**Output example (truncated):**
```json
{
  "parts": [
    {
      "notes": [
        {"measure_number": 12, "velocity": 0.35},
        {"measure_number": 12, "velocity": 0.40}
      ]
    }
  ]
}
```

---

## Internal Pipeline Steps (not exposed via MCP)

These APIs are implemented for debugging and internal composition. The MCP server does not
expose them via `tools/list`; `synthesize` uses them under the hood.

## Utility: `phonemize`

Convert lyrics to phoneme sequences.

| Attribute | Description |
|-----------|-------------|
| **Input** | `lyrics`: List of lyric strings |
| | `voicebank`: Voicebank path or ID |
| | `language`: Language code (default: "en") |
| **Output** | `phonemes`: List of phoneme strings |
| | `phoneme_ids`: List of token IDs |
| | `language_ids`: List of language IDs (for multilingual) |
| | `word_boundaries`: List of phoneme counts per word |
| **Purpose** | Text-to-phoneme conversion with voicebank dictionary |

**Example:**
```
phonemize(["hello", "world"], voicebank="Raine_Rena")
→ {
    "phonemes": ["en/hh", "en/ah", "en/l", "en/ow", "en/w", "en/er", "en/l", "en/d"],
    "phoneme_ids": [26, 10, 31, 35, 47, 22, 31, 17],
    "language_ids": [1, 1, 1, 1, 1, 1, 1, 1],
    "word_boundaries": [4, 4]
  }
```

---

## Step 2: `align_phonemes_to_notes`

Prepare phonemes and note timing for inference.

This is the required entry point for step-by-step workflows, consolidating the timing
logic used by `synthesize(...)` so an LLM does not re-implement beat → frame conversion.

| Attribute | Description |
|-----------|-------------|
| **Input** | `score`: Score JSON dict |
| | `voicebank`: Voicebank path or ID |
| | `part_id`: Part ID (preferred over `part_index`, optional) |
| | `part_index`: Part index (optional; defaults to first part with lyrics) |
| | `verse_number`: Verse number to select (optional, defaults to first available) |
| | `voice_id`: Voice selection within a part (optional) |
| | `include_phonemes`: Include phoneme strings in output (default: false) |
| **Output** | `phoneme_ids`: Token IDs |
| | `phonemes`: Phoneme strings (optional) |
| | `language_ids`: Language ID per phoneme |
| | `word_boundaries`: Phoneme counts per word |
| | `word_durations`: Frames per word group |
| | `word_pitches`: MIDI pitch per word group |
| | `note_durations`: Frames per note (for pitch) |
| | `note_pitches`: MIDI pitch per note (for pitch) |
| | `note_rests`: Rest flags per note |
| **Purpose** | Provide ready-to-use inputs for `predict_durations` and `predict_pitch` |

**Example:**
```
align_phonemes_to_notes(score, voicebank="Raine_Rena", voice_id="soprano", include_phonemes=True)
→ {
  "phoneme_ids": [...],
  "phonemes": ["en/hh", "en/ah", "en/l", "en/ow", ...],
  "language_ids": [...],
  "word_boundaries": [...],
  "word_durations": [...],
  "word_pitches": [...],
  "note_durations": [...],
  "note_pitches": [...],
  "note_rests": [...]
}
```

## Step 3: `predict_durations`

Predict timing for each phoneme.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `word_boundaries`: Phoneme counts per word |
| | `word_durations`: Duration per word in frames (from step 2) |
| | `word_pitches`: MIDI pitch per word (from step 2) |
| | `voicebank`: Voicebank path or ID |
| | `language_ids`: Language ID per phoneme (optional) |
| **Output** | `durations`: Frames per phoneme |
| | `total_frames`: Total audio frames |
| | `encoder_out`: Encoder features for downstream steps |
| | `x_masks`: Encoder masks |
| **Purpose** | Determine phoneme timing aligned to musical rhythm |

**Input example:**
```
predict_durations(
  phoneme_ids=[26, 10, 31],
  word_boundaries=[3],
  word_durations=[120],
  word_pitches=[69],
  voicebank="Raine_Rena",
  language_ids=[1, 1, 1],
)
```

**Output example:**
```json
{
  "durations": [40, 40, 40],
  "total_frames": 120,
  "encoder_out": "<array>",
  "x_masks": "<array>"
}
```

---

## Step 4: `predict_pitch`

Generate natural pitch curves.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `durations`: From predict_durations step |
| | `note_pitches`: MIDI pitch per note |
| | `note_durations`: Frames per note |
| | `note_rests`: Rest flags per note |
| | `voicebank`: Voicebank path or ID |
| | `language_ids`: Language ID per phoneme (optional) |
| | `encoder_out`: Encoder output from predict_durations (optional) |
| **Output** | `f0`: Pitch curve in Hz (one value per frame) |
| | `pitch_midi`: Pitch curve in MIDI notes |
| **Purpose** | Add vibrato, transitions, and natural pitch variation |
| **Fallback** | If no pitch model, returns MIDI-derived frequencies |

**Input example:**
```
predict_pitch(
  phoneme_ids=[26, 10, 31],
  durations=[40, 40, 40],
  note_pitches=[69],
  note_durations=[120],
  note_rests=[False],
  voicebank="Raine_Rena",
  language_ids=[1, 1, 1],
)
```

**Output example:**
```json
{
  "f0": [440.1, 439.9, 440.2, "..."],
  "pitch_midi": [69.0, 69.0, 69.0, "..."]
}
```

---

## Step 5: `predict_variance`

Generate expressive parameters (optional step).

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `durations`: From predict_durations step |
| | `f0`: From predict_pitch step |
| | `voicebank`: Voicebank path or ID |
| | `language_ids`: Language ID per phoneme (optional) |
| | `encoder_out`: Encoder output from predict_durations (optional) |
| **Output** | `breathiness`: Breathiness curve (0.0–1.0 per frame) |
| | `tension`: Tension curve |
| | `voicing`: Voicing curve |
| **Purpose** | Add expressiveness (airy voice, tense voice, etc.) |
| **Fallback** | Returns zeros if no variance model |

**Input example:**
```
predict_variance(
  phoneme_ids=[26, 10, 31],
  durations=[40, 40, 40],
  f0=[440.0, 440.0, 440.0],
  voicebank="Raine_Rena",
  language_ids=[1, 1, 1],
)
```

**Output example:**
```json
{
  "breathiness": [0.02, 0.03, 0.02, "..."],
  "tension": [0.10, 0.11, 0.10, "..."],
  "voicing": [0.95, 0.96, 0.95, "..."]
}
```

---

## Step 6: `synthesize_audio`

Generate audio by running mel synthesis and vocoding.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: Token sequence |
| | `durations`: Frames per phoneme |
| | `f0`: Pitch curve |
| | `breathiness`, `tension`, `voicing`: Variance curves (optional) |
| | `voicebank`: Voicebank path or ID |
| | `language_ids`: Language ID per phoneme (optional) |
| | `vocoder_path`: Optional explicit vocoder path |
| **Output** | `waveform`: Audio samples (numpy array) |
| | `sample_rate`: Audio sample rate |
| | `hop_size`: Samples per frame |
| **Purpose** | End-to-end acoustic synthesis |

**Input example:**
```
synthesize_audio(
  phoneme_ids=[26, 10, 31],
  durations=[40, 40, 40],
  f0=[440.0, 440.0, 440.0],
  voicebank="Raine_Rena",
  breathiness=[0.02, 0.03, 0.02],
  tension=[0.10, 0.11, 0.10],
  voicing=[0.95, 0.96, 0.95],
  language_ids=[1, 1, 1],
)
```

**Output example:**
```json
{
  "waveform": [0.001, 0.004, -0.003, "..."],
  "sample_rate": 44100,
  "hop_size": 512
}
```

---

## Additional Internal APIs (advanced / debugging)

These APIs are kept for debugging and advanced workflows, but are not exposed via `tools/list`.

### `synthesize_mel`

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`, `durations`, `f0`, variance curves, `voicebank`, `language_ids` |
| **Output** | `mel`, `sample_rate`, `hop_size` |
| **Purpose** | Inspect mel output before vocoding |

**Input example:**
```
synthesize_mel(
  phoneme_ids=[26, 10, 31],
  durations=[40, 40, 40],
  f0=[440.0, 440.0, 440.0],
  voicebank="Raine_Rena",
  language_ids=[1, 1, 1],
)
```

**Output example:**
```json
{
  "mel": [[0.12, 0.08, 0.04], "..."],
  "sample_rate": 44100,
  "hop_size": 512
}
```

### `vocode`

| Attribute | Description |
|-----------|-------------|
| **Input** | `mel`, `f0`, `voicebank`, `vocoder_path` |
| **Output** | `waveform`, `sample_rate` |
| **Purpose** | Swap vocoders or benchmark vocoder-only behavior |

**Input example:**
```
vocode(
  mel=[[0.12, 0.08, 0.04], "..."],
  f0=[440.0, 440.0, 440.0],
  voicebank="Raine_Rena",
)
```

**Output example:**
```json
{
  "waveform": [0.001, 0.004, -0.003, "..."],
  "sample_rate": 44100
}
```

---

## Output: `save_audio`

Write audio to a file.

| Attribute | Description |
|-----------|-------------|
| **Input** | `waveform`: Audio samples |
| | `output_path`: File path |
| | `sample_rate`: Sample rate |
| | `format`: "wav" or "mp3" (default: "wav") |
| **Output** | `audio_base64`: Base64-encoded audio bytes |
| | `duration_seconds`: Audio duration |
| | `sample_rate`: Sample rate |
| **Purpose** | Persist the generated audio |

**Input example:**
```
save_audio([0.001, 0.004, -0.003], "outputs/demo.wav", sample_rate=44100)
```

**Output example:**
```json
{
  "audio_base64": "UklGRiQAAABXQVZFZm10IBAAAAABAAEA...",
  "duration_seconds": 0.00007,
  "sample_rate": 44100
}
```

---

## Convenience: `synthesize`

Run the full pipeline (internal steps 2–6) in one call.

| Attribute | Description |
|-----------|-------------|
| **Input** | `score`: Score JSON dict |
| | `voicebank`: Voicebank path or ID |
| | `part_index`: Part index (default: 0) |
| | `voice_id`: Voice selection within a part (optional) |
| | `voice_color`: Voice color ID (optional) |
| | `articulation`: Global legato/staccato (-1.0 to +1.0, default: 0.0) |
| | `airiness`: Global breathiness (0.0 to 1.0, default: 1.0) |
| | `intensity`: Global tension (0.0 to 1.0, default: 1.0) |
| | `clarity`: Global voicing (0.0 to 1.0, default: 1.0) |
| | `device`: Inference device (default: "cpu") |
| **Output** | `waveform`: Audio samples |
| | `sample_rate`: Sample rate |
| | `duration_seconds`: Audio duration |
| **Purpose** | Simple end-to-end synthesis |

**Global adjustments:**
- `articulation`: lower = more staccato (adds short gaps), higher = more legato (fewer gaps)
- `airiness`: lower = cleaner/less airy, higher = more airy
- `intensity`: lower = more relaxed, higher = more tense/bright
- `clarity`: lower = more breathy/less voiced, higher = more voiced/clear

**Input example:**
```
synthesize(
  score,
  "Raine_Rena",
  voice_id="soprano",
  voice_color="02: soft",
  articulation=0.3,
  airiness=0.9,
  intensity=0.8,
  clarity=0.95,
)
```

**Output example:**
```json
{
  "waveform": [0.001, 0.004, -0.003, "..."],
  "sample_rate": 44100,
  "duration_seconds": 24.50
}
```

`voice_id` accepts a numeric voice (e.g. `"1"`) or labels like `"soprano"`, `"alto"`, `"tenor"`, `"bass"`.

Equivalent to calling the internal APIs below (not MCP-exposed):
```
prep = align_phonemes_to_notes(score, voicebank)
dur = predict_durations(prep['phoneme_ids'], prep['word_boundaries'], prep['word_durations'], prep['word_pitches'], voicebank, language_ids=prep['language_ids'])
pitch = predict_pitch(prep['phoneme_ids'], dur['durations'], prep['note_pitches'], prep['note_durations'], prep['note_rests'], voicebank, language_ids=prep['language_ids'], encoder_out=dur['encoder_out'])
var = predict_variance(prep['phoneme_ids'], dur['durations'], pitch['f0'], voicebank, language_ids=prep['language_ids'], encoder_out=dur['encoder_out'])
audio = synthesize_audio(
  prep['phoneme_ids'],
  dur['durations'],
  pitch['f0'],
  voicebank,
  breathiness=var['breathiness'],
  tension=var['tension'],
  voicing=var['voicing'],
  language_ids=prep['language_ids'],
)
waveform = audio['waveform']
```

---

## Metadata: `list_voicebanks`

List available voicebanks.

| Attribute | Description |
|-----------|-------------|
| **Input** | `search_path`: Optional directory to search |
| **Output** | List of voicebank info objects |
| **Purpose** | Discover available voices |

**Input example:**
```
list_voicebanks("assets/voicebanks")
```

**Output example:**
```json
[
  {
    "id": "Raine_Rena_2.01",
    "name": "Raine Rena",
    "path": "assets/voicebanks/Raine_Rena_2.01"
  },
  {
    "id": "Raine_Reizo_2.01",
    "name": "Raine Reizo",
    "path": "assets/voicebanks/Raine_Reizo_2.01"
  }
]
```

---

## Metadata: `get_voicebank_info`

Get detailed information about a voicebank.

| Attribute | Description |
|-----------|-------------|
| **Input** | `voicebank`: Voicebank path or ID |
| **Output** | Capabilities object |
| **Purpose** | Query what models/languages are supported |

**Input example:**
```
get_voicebank_info("Raine_Rena_2.01")
```

**Output example:**
```json
{
  "name": "Raine Rena",
  "languages": ["en", "ja"],
  "has_pitch_model": true,
  "has_variance_model": true,
  "speakers": ["embeds/normal", "embeds/soft", "embeds/strong"],
  "voice_colors": [
    {"name": "01: normal", "suffix": "embeds/normal"},
    {"name": "02: soft", "suffix": "embeds/soft"},
    {"name": "03: strong", "suffix": "embeds/strong"}
  ],
  "default_voice_color": "01: normal",
  "sample_rate": 44100
}
```

---

## Example Workflows

### Simple Render (using convenience API)
```
1. score = parse_score("song.xml")
2. audio = synthesize(score, "Raine_Rena", voice_id="soprano")
3. save_audio(audio, "output.wav")
```

### Internal Full Control (not MCP-exposed)
This workflow uses internal-only APIs for debugging and is not available via `tools/list`.
```
1. score = parse_score("song.xml")
2. modify_score(score, "...transpose up 12...")
3. prep = align_phonemes_to_notes(score, "Raine_Rena", voice_id="soprano")
4. dur = predict_durations(prep['phoneme_ids'], prep['word_boundaries'], prep['word_durations'], prep['word_pitches'], "Raine_Rena", language_ids=prep['language_ids'])
5. pitch = predict_pitch(prep['phoneme_ids'], dur['durations'], prep['note_pitches'], prep['note_durations'], prep['note_rests'], "Raine_Rena", language_ids=prep['language_ids'], encoder_out=dur['encoder_out'])
6. var = predict_variance(prep['phoneme_ids'], dur['durations'], pitch['f0'], "Raine_Rena", language_ids=prep['language_ids'], encoder_out=dur['encoder_out'])
7. audio = synthesize_audio(
    prep['phoneme_ids'],
    dur['durations'],
    pitch['f0'],
    "Raine_Rena",
    breathiness=var['breathiness'],
    tension=var['tension'],
    voicing=var['voicing'],
    language_ids=prep['language_ids'],
)
8. save_audio(audio, "output.wav")
```

### Debug Phonemes Only (internal)
This workflow uses internal-only APIs and is not available via `tools/list`.
```
1. phonemes = phonemize(["hello", "world"], "Raine_Rena")
2. Return phonemes to user for inspection
```

### Compare Two Voicebanks
```
1. score = parse_score("song.xml")
2. audio_a = synthesize(score, "Raine_Rena")
3. audio_b = synthesize(score, "Other_Voice")
4. save_audio(audio_a, "version_a.wav")
5. save_audio(audio_b, "version_b.wav")
```

---

## Score JSON Schema

The score JSON captures all singing-voice-related information from MusicXML.

### Top Level
```
ScoreJSON
├── title: string | null           # Work title
├── tempos: TempoEvent[]           # Tempo markings
├── parts: Part[]                  # Voice parts
└── structure: Structure           # Repeats, endings, jumps (placeholder)
```

### Note (comprehensive)
```
Note
├── offset_beats: float            # Position in score
├── duration_beats: float          # Note length in beats
├── measure_number: int | null     # Bar number
├── voice: string | null           # Voice number within the part
├── pitch_midi: float | null       # MIDI note (null for rests)
├── pitch_hz: float | null         # Frequency in Hz
├── lyric: string | null           # The sung text
├── syllabic: string | null        # "begin", "middle", "end", "single"
├── lyric_is_extended: bool        # Melisma (held syllable)
├── is_rest: bool                  # True if rest
└── tie_type: string | null        # "start", "stop", "continue"
```

### Structure
```
Structure
├── repeats: [{start_bar, end_bar, times}]
├── endings: [{number, start_bar, end_bar}]
└── jumps: [{type, at_bar, target_bar}]     # da_capo, dal_segno, coda, fine
```

---

## Dynamics Reference

| Marking | velocity | Description |
|---------|----------|-------------|
| ppp | 0.1 | Very very soft |
| pp | 0.2 | Very soft |
| p | 0.35 | Soft |
| mp | 0.5 | Medium soft |
| mf | 0.65 | Medium loud |
| f | 0.8 | Loud |
| ff | 0.9 | Very loud |
| fff | 0.95 | Very very loud |

---

## Security: `modify_score` Sandboxing

The `modify_score` API executes code in a restricted environment:

1. **RestrictedPython**: Used when installed; otherwise falls back to unsafe `exec`
2. **Limited namespace**: Only `score`, `math`, basic builtins
3. **No I/O**: No file access, network, imports

---

## API Summary

Public MCP tools:

| # | API | Purpose |
|---|-----|---------|
| 1 | `parse_score` | MusicXML → JSON |
| — | `modify_score` | Python code on JSON |
| — | `synthesize` | All-in-one (internal steps 2–6) |
| — | `save_audio` | Audio → File |
| — | `list_voicebanks` | Discover voices |
| — | `get_voicebank_info` | Query capabilities |

Internal only (not MCP-exposed):

| # | API | Purpose |
|---|-----|---------|
| — | `phonemize` | Lyrics → Phonemes |
| 2 | `align_phonemes_to_notes` | Score → Phonemes + note timing |
| 3 | `predict_durations` | Phonemes → Timing |
| 4 | `predict_pitch` | Notes → F0 curve |
| 5 | `predict_variance` | → Expressiveness |
| 6 | `synthesize_audio` | Mel + vocoder → Audio |
| — | `synthesize_mel` | Mel only (debug) |
| — | `vocode` | Vocoder only (debug) |

**Total: 6 public MCP tools + 8 internal APIs**
