# SVS Backend API Design

This document defines the public APIs for the Singing Voice Synthesis backend. These APIs are designed to be called by an LLM orchestrator via MCP.

---

## Design Philosophy

1. **LLM as Orchestrator**: The LLM understands musical terminology and decides which APIs to call
2. **JSON as Universal Format**: Score data is JSON — the LLM can read, reason about, and modify it
3. **Code Execution for Flexibility**: Complex score modifications are done via Python code, not specialized APIs
4. **Granular Control**: Each pipeline step is exposed as an API for debugging and customization

---

## API Overview (7 Pipeline Steps + Utilities)

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
│  STEP 2: phonemize          │  Lyrics → Phoneme tokens + IDs            │
│  STEP 3: predict_durations  │  Phonemes → Duration per phoneme          │
│  STEP 4: predict_pitch      │  Notes → F0 curve (Hz)                    │
│  STEP 5: predict_variance   │  → Breathiness, tension, voicing          │
│  STEP 6: synthesize_mel     │  All inputs → Mel spectrogram             │
│  STEP 7: vocode             │  Mel → Audio waveform                     │
│  OUTPUT: save_audio         │  Waveform → File                          │
├─────────────────────────────────────────────────────────────────────────┤
│  CONVENIENCE: synthesize    │  Score → Audio (runs steps 2-7)           │
│  METADATA: list_voicebanks  │  List available voices                    │
│  METADATA: get_voicebank_info │  Query voice capabilities               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Step 1: `parse_score`

Parse a MusicXML file into a JSON structure.

| Attribute | Description |
|-----------|-------------|
| **Input** | `file_path`: Path to MusicXML file |
| **Output** | Score as JSON dict (see schema below) |
| **Purpose** | Convert music notation to LLM-readable format |

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

---

## Step 2: `phonemize`

Convert lyrics to phoneme sequences.

| Attribute | Description |
|-----------|-------------|
| **Input** | `lyrics`: List of lyric strings |
| | `voicebank`: Voicebank path or ID |
| | `language`: Language code (default: "en") |
| **Output** | `phonemes`: List of phoneme strings |
| | `phoneme_ids`: List of token IDs |
| | `language_ids`: List of language IDs (for multilingual) |
| **Purpose** | Text-to-phoneme conversion with voicebank dictionary |

**Example:**
```
phonemize(["hello", "world"], voicebank="Raine_Rena")
→ {
    "phonemes": ["hh", "ah", "l", "ow", "w", "er", "l", "d"],
    "phoneme_ids": [15, 4, 21, 32, 45, 12, 21, 8],
    "language_ids": [1, 1, 1, 1, 1, 1, 1, 1]
  }
```

---

## Step 3: `predict_durations`

Predict timing for each phoneme.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `note_durations`: Duration of each note (beats) |
| | `note_pitches`: MIDI pitch of each note |
| | `voicebank`: Voicebank path or ID |
| **Output** | `durations`: Frames per phoneme |
| | `total_frames`: Total audio frames |
| **Purpose** | Determine phoneme timing aligned to musical rhythm |

---

## Step 4: `predict_pitch`

Generate natural pitch curves.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `durations`: From predict_durations step |
| | `note_pitches`: MIDI pitch per note |
| | `note_durations`: Duration per note |
| | `voicebank`: Voicebank path or ID |
| **Output** | `f0`: Pitch curve in Hz (one value per frame) |
| **Purpose** | Add vibrato, transitions, and natural pitch variation |
| **Fallback** | If no pitch model, returns flat MIDI-derived frequencies |

---

## Step 5: `predict_variance`

Generate expressive parameters (optional step).

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: From phonemize step |
| | `durations`: From predict_durations step |
| | `f0`: From predict_pitch step |
| | `voicebank`: Voicebank path or ID |
| **Output** | `breathiness`: Breathiness curve (0.0–1.0 per frame) |
| | `tension`: Tension curve |
| | `voicing`: Voicing curve |
| **Purpose** | Add expressiveness (airy voice, tense voice, etc.) |
| **Fallback** | Returns zeros if no variance model |

---

## Step 6: `synthesize_mel`

Generate the Mel spectrogram.

| Attribute | Description |
|-----------|-------------|
| **Input** | `phoneme_ids`: Token sequence |
| | `durations`: Frames per phoneme |
| | `f0`: Pitch curve |
| | `breathiness`, `tension`, `voicing`: Variance curves (optional) |
| | `voicebank`: Voicebank path or ID |
| **Output** | `mel`: Mel spectrogram [frames × mel_bins] |
| | `sample_rate`: Audio sample rate |
| | `hop_size`: Samples per frame |
| **Purpose** | Core acoustic synthesis |

---

## Step 7: `vocode`

Convert Mel spectrogram to audio waveform.

| Attribute | Description |
|-----------|-------------|
| **Input** | `mel`: Mel spectrogram from synthesize_mel |
| | `f0`: Pitch curve |
| | `vocoder`: Vocoder path (optional, uses voicebank default) |
| **Output** | `waveform`: Audio samples (numpy array) |
| | `sample_rate`: Sample rate |
| **Purpose** | Final audio generation |

---

## Output: `save_audio`

Write audio to a file.

| Attribute | Description |
|-----------|-------------|
| **Input** | `waveform`: Audio samples |
| | `output_path`: File path |
| | `sample_rate`: Sample rate |
| | `format`: "wav" or "mp3" (default: "wav") |
| **Output** | File path |
| **Purpose** | Persist the generated audio |

---

## Convenience: `synthesize`

Run the full pipeline (steps 2–7) in one call.

| Attribute | Description |
|-----------|-------------|
| **Input** | `score`: Score JSON dict |
| | `voicebank`: Voicebank path or ID |
| | `options`: Synthesis options (optional) |
| **Output** | `waveform`: Audio samples |
| | `sample_rate`: Sample rate |
| **Purpose** | Simple end-to-end synthesis |

Equivalent to calling:
```
phonemes = phonemize(score.lyrics, voicebank)
durations = predict_durations(phonemes, score, voicebank)
f0 = predict_pitch(phonemes, durations, score, voicebank)
variance = predict_variance(phonemes, durations, f0, voicebank)
mel = synthesize_mel(phonemes, durations, f0, variance, voicebank)
waveform = vocode(mel, f0, voicebank)
```

---

## Metadata: `list_voicebanks`

List available voicebanks.

| Attribute | Description |
|-----------|-------------|
| **Input** | `search_path`: Optional directory to search |
| **Output** | List of voicebank info objects |
| **Purpose** | Discover available voices |

---

## Metadata: `get_voicebank_info`

Get detailed information about a voicebank.

| Attribute | Description |
|-----------|-------------|
| **Input** | `voicebank`: Voicebank path or ID |
| **Output** | Capabilities object |
| **Purpose** | Query what models/languages are supported |

**Output example:**
```json
{
  "name": "Raine Rena",
  "languages": ["en", "ja"],
  "has_pitch_model": true,
  "has_variance_model": true,
  "speakers": ["normal", "soft", "strong"],
  "sample_rate": 44100
}
```

---

## Example Workflows

### Simple Render (using convenience API)
```
1. score = parse_score("song.xml")
2. audio = synthesize(score, "Raine_Rena")
3. save_audio(audio, "output.wav")
```

### Full Control (using step-by-step APIs)
```
1. score = parse_score("song.xml")
2. modify_score(score, "...transpose up 12...")
3. phonemes = phonemize(score.lyrics, "Raine_Rena")
4. durations = predict_durations(phonemes, score.notes, "Raine_Rena")
5. f0 = predict_pitch(phonemes, durations, score.notes, "Raine_Rena")
6. variance = predict_variance(phonemes, durations, f0, "Raine_Rena")
7. mel = synthesize_mel(phonemes, durations, f0, variance, "Raine_Rena")
8. audio = vocode(mel, f0)
9. save_audio(audio, "output.wav")
```

### Debug Phonemes Only
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
├── composer: string | null        # Composer name
├── tempos: TempoEvent[]           # Tempo markings
├── time_signatures: TimeSig[]     # Time signature changes
├── key_signatures: KeySig[]       # Key signature changes
├── parts: Part[]                  # Voice parts
└── structure: Structure           # Repeats, endings, jumps
```

### Note (comprehensive)
```
Note
├── offset_beats: float            # Position in score
├── duration_beats: float          # Note length in beats
├── measure_number: int            # Bar number
├── beat_in_measure: float         # Position within the bar
├── voice: string | null           # Voice number
│
├── # Pitch
├── pitch_midi: float | null       # MIDI note (60 = C4), null for rests
├── pitch_hz: float | null         # Frequency in Hz
├── is_rest: bool                  # True if rest
│
├── # Lyrics
├── lyric: string | null           # The sung text
├── syllabic: string | null        # "begin", "middle", "end", "single"
├── lyric_extend: bool             # Melisma (held syllable)
│
├── # Dynamics
├── dynamic: string | null         # "pp","p","mp","mf","f","ff","sfz"
├── velocity: float | null         # 0.0–1.0
├── crescendo: string | null       # "start", "stop"
├── diminuendo: string | null      # "start", "stop"
│
├── # Articulation
├── staccato: bool
├── tenuto: bool
├── accent: bool
├── breath_mark: bool
├── fermata: bool
│
├── # Connections
├── tie_type: string | null        # "start", "stop", "continue"
├── slur_type: string | null       # "start", "stop", "continue"
│
└── # Synthesis hints
    ├── breathiness: float | null
    ├── tension: float | null
    └── vibrato_depth: float | null
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

1. **RestrictedPython**: Compile with restrictions
2. **Limited namespace**: Only `score`, `math`, basic builtins
3. **No I/O**: No file access, network, imports
4. **Timeout**: 5 second execution limit

---

## API Summary

| # | API | Purpose |
|---|-----|---------|
| 1 | `parse_score` | MusicXML → JSON |
| — | `modify_score` | Python code on JSON |
| 2 | `phonemize` | Lyrics → Phonemes |
| 3 | `predict_durations` | Phonemes → Timing |
| 4 | `predict_pitch` | Notes → F0 curve |
| 5 | `predict_variance` | → Expressiveness |
| 6 | `synthesize_mel` | → Mel spectrogram |
| 7 | `vocode` | Mel → Audio |
| — | `save_audio` | Audio → File |
| — | `synthesize` | All-in-one (steps 2-7) |
| — | `list_voicebanks` | Discover voices |
| — | `get_voicebank_info` | Query capabilities |

**Total: 12 APIs** — 7 pipeline steps + 5 utilities
