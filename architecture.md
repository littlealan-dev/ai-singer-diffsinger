# DiffSinger SVS Backend – Architecture

## Purpose

This project implements a backend-only Singing Voice Synthesis (SVS) pipeline. Given a music score (MusicXML) and a trained voice model (voicebank), it generates a sung vocal audio file.

**Current Status**: Core pipeline implemented and ready for end-to-end validation.

**Future Goal**: Expose the backend via an MCP server so an LLM-driven chat UI can orchestrate synthesis from natural language prompts.

---

## How It Works (Bird's Eye View)

```
┌─────────────────┐
│   MusicXML      │  Your music score with lyrics, notes, and timing
└────────┬────────┘
         ▼
┌─────────────────┐
│  Score Parser   │  Extracts notes, lyrics, pitches, durations, tempo
└────────┬────────┘
         ▼
┌─────────────────┐
│   Phonemizer    │  Converts lyrics ("hello") → phonemes (["hh","ah","l","ow"])
└────────┬────────┘
         ▼
┌─────────────────┐
│ Duration Model  │  Predicts how long each phoneme should last
└────────┬────────┘
         ▼
┌─────────────────┐
│  Pitch Model    │  Adds realistic pitch curves (vibrato, transitions)
│   (optional)    │  Falls back to flat MIDI notes if unavailable
└────────┬────────┘
         ▼
┌─────────────────┐
│ Acoustic Model  │  Generates a Mel Spectrogram (the "blueprint" of sound)
└────────┬────────┘
         ▼
┌─────────────────┐
│    Vocoder      │  Converts Mel Spectrogram → audible waveform (WAV)
└────────┬────────┘
         ▼
┌─────────────────┐
│   Output WAV    │  The final sung audio file
└─────────────────┘
```

---

## Project Structure

```
ai-singer-diffsinger/
├── src/
│   ├── musicxml/
│   │   └── parser.py         # MusicXML parsing (uses music21)
│   ├── phonemizer/
│   │   └── phonemizer.py     # Text-to-phoneme conversion
│   ├── acoustic/
│   │   └── model.py          # ONNX model wrappers
│   ├── vocoder/
│   │   └── model.py          # Vocoder wrapper
│   └── pipeline.py           # Main orchestrator
├── assets/
│   ├── voicebanks/           # DiffSinger voicebanks (ONNX models + configs)
│   └── test_data/            # Sample MusicXML files for testing
├── tests/
│   ├── test_musicxml_parser.py
│   ├── test_phonemizer.py
│   └── test_end_to_end.py
└── third_party/              # Reference implementations (not runtime dependencies)
    ├── DiffSinger/           # Original DiffSinger repo (reference only)
    └── OpenUtau/             # OpenUtau source (reference only)
```

---

## The Pipeline Workflow

The `Pipeline` class in `src/pipeline.py` is the main entry point. Here's how it processes a song:

### Step 1: Load Voicebank

When you create a `Pipeline`, it scans the voicebank folder for:

| Folder | Contains | Required? |
|--------|----------|-----------|
| `dsdur/` | Duration model (`dur.onnx`) + Linguistic encoder | ✅ Yes |
| `dspitch/` | Pitch predictor (`pitch.onnx`) | ⚡ Recommended |
| `dsvariance/` | Breathiness/tension predictor | ⚠️ Optional |
| Root or `dsmain/` | Acoustic model (`acoustic.onnx`) | ✅ Yes |
| `dsvocoder/` or path in config | Vocoder (`vocoder.onnx`) | ✅ Yes |

The pipeline auto-discovers available models and gracefully falls back when optional ones are missing.

### Step 2: Parse the Score

The `parse_musicxml()` function reads your MusicXML file and extracts:
- **Notes**: Pitch (MIDI number), duration (in beats)
- **Lyrics**: The text to sing
- **Tempo**: BPM changes throughout the piece
- **Rests**: Silent gaps between phrases

### Step 3: Convert Lyrics to Phonemes

The `Phonemizer` converts each lyric syllable into phonemes:

```
"hello" → ["hh", "ah", "l", "ow"]
"world" → ["w", "er", "l", "d"]
```

It uses a **hybrid approach**:
1. **Dictionary Lookup**: Check the voicebank's `dsdict.yaml` first
2. **G2P Fallback**: Use machine learning (`g2p_en`) for unknown words

Each phoneme is also mapped to:
- A **token ID** (integer the model understands)
- A **language ID** (for multilingual voicebanks)

### Step 4: Predict Durations

The **Linguistic Encoder** converts phonemes into a rich representation, then the **Duration Model** predicts how many audio frames each phoneme should occupy.

The pipeline aligns these predictions to match your original note timings, ensuring the singing stays in sync with the beat.

### Step 5: Predict Pitch (Optional)

If the voicebank includes a pitch model:
- It generates a natural-sounding F0 (fundamental frequency) curve
- Adds subtle vibrato, smooth transitions between notes, and expressive nuances

If no pitch model is available:
- The pipeline uses a **naive fallback**: flat MIDI note frequencies
- This sounds robotic but still works

### Step 6: Generate the Spectrogram

The **Acoustic Model** takes all the processed data:
- Phoneme IDs and durations
- Pitch curve (F0)
- Optional variance parameters (breathiness, tension)
- Speaker embedding (voice identity)

It outputs a **Mel Spectrogram** — a visual representation of sound that captures the voice's timbre and characteristics.

### Step 7: Vocode to Audio

The **Vocoder** (HiFi-GAN) converts the Mel Spectrogram into an actual audio waveform. This is the final step that produces the `.wav` file you can listen to.

---

## Key Concepts

### Voicebank

A voicebank is a trained voice model package containing:
- ONNX neural network files
- Configuration files (`dsconfig.yaml`)
- Phoneme dictionaries
- Speaker embeddings

Think of it as a "voice profile" — each voicebank sounds like a different singer.

### Phonemes

Phonemes are the smallest units of speech sound. Unlike letters, they represent actual pronunciation:
- "cat" → `k ae t` (3 phonemes)
- "though" → `dh ow` (2 phonemes despite 6 letters)

The voicebank was trained on a specific phoneme set, so we must convert lyrics to that exact format.

### Mel Spectrogram

A Mel Spectrogram is a way to represent audio visually:
- Time flows left to right
- Frequency (pitch) goes bottom to top
- Color intensity shows loudness

Neural networks are excellent at generating these, and vocoders are excellent at converting them back to audio.

### Speaker Embedding

A vector (list of numbers) that captures a voice's unique characteristics. Changing the embedding changes the voice identity while keeping the same lyrics and melody.

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| AI Runtime | ONNX Runtime (CPU, extensible to GPU) |
| Score Parsing | music21 |
| G2P | g2p_en (English), voicebank dictionaries |
| Audio I/O | soundfile, numpy |
| Config | PyYAML |

---

## Design Principles

### 1. Modular & Swappable

Each component (Phonemizer, Duration Model, Pitch Model, Acoustic, Vocoder) is isolated. You can:
- Swap vocoders without changing the pipeline
- Use different voicebanks without code changes
- Add new languages by extending the Phonemizer

### 2. Graceful Degradation

Missing optional models don't crash the pipeline:
- No pitch model? Uses flat MIDI notes
- No variance model? Uses neutral defaults

### 3. Voicebank-Driven Configuration

The voicebank's `dsconfig.yaml` tells the pipeline:
- Sample rate and hop size
- Which models to load
- Phoneme and language mappings

No hardcoded assumptions about any specific voicebank.

---

## Current Limitations

- **English only** (for G2P fallback; dictionary-based phonemes work for other languages)
- **Batch processing** (no real-time streaming)
- **Single voice per render** (no multi-singer mixing yet)

---

## Future Roadmap

### Phase 2: Commercial Vocoder
Replace the validation vocoder with a commercially-licensed one (e.g., MIT HiFi-GAN).

### Phase 3: MCP Server
Wrap the pipeline as an MCP server so an LLM can:
- Accept natural language instructions ("sing it softly with vibrato")
- Translate them to synthesis parameters
- Call the pipeline and return audio

### Phase 4: Expression Controls
Add support for dynamic controls like:
- Breathiness / Tension curves
- Gender shifting
- Emotion / style embeddings

---

## Summary

This backend transforms sheet music into sung audio using neural networks. The `Pipeline` class orchestrates the flow from MusicXML through text-to-phoneme conversion, duration/pitch prediction, acoustic synthesis, and finally vocoding. The architecture is designed to be voicebank-agnostic and vocoder-swappable, preparing for future LLM integration via MCP.
