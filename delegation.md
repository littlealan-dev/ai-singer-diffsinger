# Coding Agent Delegation Instructions

**Objective**: Implement the Phase 1 End-to-End Validation Pipeline for the DiffSinger backend.

**Context**: 
We are building a standalone Python inference engine for DiffSinger. We do **NOT** use the `DiffSinger` codebase as a runtime library. We only use ONNX Runtime. 
Reference code for the community standard implementation (OpenUtau) is available in `third_party/OpenUtau`.

**Sources of Truth**:
- `architecture.md`: Defines high-level constraints.
- `implementation_plan.md`: Defines specific file paths and class structures.
- `third_party/OpenUtau`: Reference implementation for phonemization logic and tensor shapes.

## Checklist for Agent

### 1. Environment Setup
- [ ] Install dependencies: `pip install -r requirements.txt`.
- [ ] Initialize/Verify Submodules: 
  - `third_party/DiffSinger`: Original codebase for reference.
  - `third_party/OpenUtau`: C# reference for community-standard phonemizers.

### 2. Implementation (`src/`)

#### A. Phonemizer (`src/phonemizer/phonemizer.py`)
Implement the `Phonemizer` class that emulates OpenUtau's hybrid logic:
- **Dictionary Lookup**: First check voicebank-specific dictionaries (e.g., `dsdict-en.yaml`, `dsdict-ja.yaml`, or falling back to `dsdict.yaml`).
- **G2P Fallback (English)**: If missing in dictionary, use `g2p_en`.
  - **Mapping**: ARPABET symbols must be lowercase, have digits (stress) stripped, and be prefixed with the language code if `use_lang_id: true` (e.g., `HH1` -> `en/hh`).
- **Language Support**: 
  - Load `languages.json` (from `dsconfig.yaml`'s `languages` key).
  - Produce a `languages` tensor containing the numerical ID for each phoneme.
- **Validation**: Ensure all phonemes exist in the voicebank's `phonemes.json`.

#### B. Acoustic Model & Duration Model (`src/acoustic/model.py`)
DiffSinger uses two ONNX models for timing and features:
1.  **Duration Model** (`dur.onnx`): Predicts phoneme durations.
    - Inputs: `tokens`, `languages`, `ph_midi`, `word_div`, `word_dur`, `spk_embed`.
2.  **Acoustic Model** (`acoustic.onnx`): Predicts Mel Spectrogram.
    - Inputs: `tokens`, `languages`, `ph_midi`, `word_div`, `word_dur`, `spk_embed`, and possibly `f0` or `velocity`.

#### C. Vocoder (`src/vocoder/model.py`)
- Load the PC-NSF-HiFi-GAN ONNX model.
- `infer(mel_spectrogram)` -> returns Audio Waveform (numpy array).

#### D. Pipeline Orchestrator (`src/pipeline.py`)
- Arguments: `--xml` (MusicXML), `--voice` (Voicebank folder), `--out` (Output WAV).
- **Core Orchestration**:
  1. Parse MusicXML -> extract `lyrics`, `note pitch`, `note duration (beats)`, and `tempo`.
  2. Instantiate `Phonemizer` to get `tokens` and `language_ids`.
  3. Calculate `word_dur` (note durations converted to frames/samples based on tempo and hop_size).
  4. Run Duration Model -> get predicted alignment.
  5. Run Acoustic Model -> get Mel Spectrogram.
  6. Run Vocoder -> get WAV.

### 3. Verification
- [ ] Run `python3 -m unittest discover tests` to ensure `phonemizer` and `parser` tests pass.
- [ ] Implement `tests/test_end_to_end.py` that takes a simple 1-note MusicXML and produces a WAV file.
- [ ] Verify tensor shapes against `third_party/OpenUtau/OpenUtau.Core/DiffSinger/DiffSingerBasePhonemizer.cs`.
