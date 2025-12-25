# Phase 1: End-to-End Validation Pipeline

## Goal Description
Build a minimal, standalone Python backend to validate the DiffSinger OpenUtau-compatible pipeline.
**Success Criteria**: Generate an audible `.wav` file from a test MusicXML file using:
1.  A standard OpenUtau DiffSinger Voicebank (ONNX).
2.  The community PC-HiFi-GAN vocoder.

## User Review Required
> [!IMPORTANT]
> **Asset Requirement**: You must have an OpenUtau DiffSinger voicebank with `dsconfig.yaml`, `dsmain/acoustic.onnx`, `dsmain/phonemes.json`, and a dictionary in `dsdur/dsdict.yaml` (or `dsdur/dsdict-<lang>.yaml`). For the vocoder, use the PC-NSF-HiFi-GAN ONNX (e.g. `assets/vocoders/pc_nsf_hifigan_44.1k/pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx`).

## Proposed Changes

### Configuration & Environment
#### [NEW] [requirements.txt](file:///Users/alanchan/antigravity/ai-singer-diffsinger/requirements.txt)
- `onnxruntime` (or `onnxruntime-gpu` if available)
- `numpy`
- `soundfile`
- `pyyaml`
- `music21` (for parsing MusicXML)
- `g2p_en` (English G2P fallback when a token is missing in the voicebank dictionary)
- `jieba` (optional, for Chinese/Pinyin support if needed later)

### Project Structure (Reference Strategy)
#### [NEW] [third_party/](file:///Users/alanchan/antigravity/ai-singer-diffsinger/third_party/)
- We will include the official DiffSinger repository (e.g., `openvpi/DiffSinger`) here as a **Git Submodule**.
- **Purpose**: Reference implementation, debugging, and A/B testing outputs.
- **Constraint**: The `src/` directory must NOT import from `third_party/` in production code. Only test scripts may import both to compare results.

### Logic Layer (`src/`)

#### [NEW] [src/phonemizer/base.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/phonemizer/base.py)
- `Phonemizer` class.
- **Dependency**: Uses `g2p_en` as a fallback when dictionary lookup fails.
- **Mapping**: Loads `phonemes.json` (via `dsconfig.yaml` key `phonemes`, e.g. `dsmain/phonemes.json`) to map ARPABET symbols (e.g., `AA`) to Model IDs (e.g., `8`).
- **Logic**: 
  1. Input: "Hello"
  2. G2P: `['HH', 'AH0', 'L', 'OW1']`
  3. Map to Bank format: `['en/hh', 'en/ax', 'en/l', 'en/ow']` (heuristic mapping required)
  4. Map to ID: `[26, 13, 31, 35]`

#### [NEW] [src/acoustic/model.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/acoustic/model.py)
- `AcousticModel` wrapper for `acoustic.onnx` (path from `dsconfig.yaml` key `acoustic`, e.g. `dsmain/acoustic.onnx`).
- Inputs: Phoneme IDs, Pitch (f0), Duration (if manual) or just Phonemes (if model has duration predictor).
- Outputs: Mel Spectrogram (`[B, T, n_mels]`).

#### [NEW] [src/vocoder/model.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/vocoder/model.py)
- `Vocoder` wrapper for PC-NSF-HiFi-GAN ONNX (e.g. `assets/vocoders/pc_nsf_hifigan_44.1k/pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx`).
- Inputs: Mel Spectrogram.
- Outputs: Audio Waveform.

#### [NEW] [src/pipeline.py](file:///Users/alanchan/antigravity/ai-singer-diffsinger/src/pipeline.py)
- Implement `Phonemizer` class that emulates OpenUtau's hybrid logic.
- **Phonemization Priority**:
    1. **Voicebank Dictionary Override**: Load the appropriate `dsdict-<lang>.yaml` based on the requested language (falling back to `dsdict.yaml`).
    2. **G2P Fallback**:
        - For **English**: Use `g2p_en` if a token is not found in the dictionary.
        - For **Other Languages**: Rely on `dsdict.yaml` or provide language-specific plugins (Phase 2).
- **Mapping & Prefixing**:
    - Convert phonemes to the voicebank's internal format.
    - Handle language prefixes (e.g., `en/`, `ja/`, `zh/`) based on `use_lang_id` from `dsconfig.yaml`.
- **Validation**: Ensure all final phonemes exist in `phonemes.json`.

## Verification Plan

### Automated Tests
- **Environment**: `python -c "import onnxruntime; print(onnxruntime.get_device())"`
- **Pipeline Shape Check**: Unit test that feeds dummy ID inputs to ONNX models and asserts output tensor shapes match expectations (e.g. Mel frames relative to duration, Audio samples relative to Mel frames).

### Manual Verification
- **Audit**: User (you) will provide a `test.musicxml` and Voicebank path.
- **Run**: `python src/pipeline.py --xml test.musicxml --voice path/to/voicebank --out output.wav`
- **Listen**: Confirm the generated audio contains intelligible lyrics and correct pitch.
