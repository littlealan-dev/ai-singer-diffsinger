# Coding Agent Delegation Instructions

**Objective**: Implement the Phase 1 End-to-End Validation Pipeline for the DiffSinger backend.

**Context**: 
We are building a standalone Python inference engine for DiffSinger. We do **NOT** use the `DiffSinger` codebase as a runtime library. We only use ONNX Runtime. The official repo is kept in `third_party/` for reference/debugging only.

**Sources of Truth**:
- `architecture.md`: Defines high-level constraints (Vocoder isolation, Phonemizer requirements).
- `implementation_plan.md`: Defines specific file paths and class structures.

## Checklist for Agent

### 1. Environment Setup
- [ ] Install dependencies: `pip install -r requirements.txt` (Ensure `onnxruntime`, `numpy`, `soundfile`, `pyyaml`, `music21`, `g2p_en` are installed).
- [ ] Initialize Submodule: Add `https://github.com/openvpi/DiffSinger.git` to `third_party/`. 
  - *Command*: `git submodule add https://github.com/openvpi/DiffSinger.git third_party/DiffSinger`
  - *Note*: Do NOT import this package in `src/`.

### 2. Implementation (`src/`)
Implement the following modules according to `implementation_plan.md`:

#### A. Phonemizer (`src/phonemizer/base.py`)
- Implement `Phonemizer` class.
- **Critical Logic**: 
  - Load `phonemes.json` (ID map) via `voice/dsconfig.yaml` (e.g. `dsmain/phonemes.json`).
  - Load dictionary from `voice/dsdur/dsdict.yaml` (or `dsdur/dsdict-<lang>.yaml`).
  - Use `g2p_en` to convert English text to ARPABET.
  - Map ARPABET tokens to the keys in `phonemes.json` (e.g. `HH` -> `en/hh`) to get IDs.
- *Constraint*: The provided voicebank (`Raine_Rena`) relies on OpenUtau's G2P, so we must emulate it using `g2p_en`.

#### B. Acoustic Model (`src/acoustic/model.py`)
- Implement `AcousticModel` class.
- Load the ONNX model specified in `dsconfig.yaml` (key: `acoustic`).
- `infer(phoneme_ids, f0, speedup)` -> returns Mel Spectrogram (numpy array).

#### C. Vocoder (`src/vocoder/model.py`)
- Implement `Vocoder` class.
- Load the PC-NSF-HiFi-GAN ONNX (e.g. `assets/vocoders/pc_nsf_hifigan_44.1k/pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx`) using `onnxruntime.InferenceSession`.
- `infer(mel_spectrogram)` -> returns Audio Waveform (numpy array).

#### D. Pipeline Orchestrator (`src/pipeline.py`)
- Create a CLI script using `argparse`.
- Arguments: `--xml` (MusicXML path), `--voice` (Voicebank folder path), `--out` (Output WAV path).
- Logic:
  1. Parse MusicXML (extract lyrics, pitch, duration).
  2. Parse `voice/dsconfig.yaml` to find `dsmain/acoustic.onnx` and `dsmain/phonemes.json`.
  3. Load dictionary from `voice/dsdur/dsdict.yaml` (or `dsdur/dsdict-<lang>.yaml`).
  4. Instantiate `Phonemizer` and `AcousticModel`.
  5. Convert lyrics -> phoneme IDs using G2P + Mapping.
  6. Run `AcousticModel`.
  7. Run `Vocoder`.
  8. Save output with `soundfile`.

### 3. Verification
- Create a dummy test script `tests/test_shapes.py` to verify that ONNX models load and accept tensors of the correct shape.
