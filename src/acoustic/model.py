import onnxruntime as ort
import numpy as np
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any

class DiffSingerModel:
    """Base class for DiffSinger ONNX models."""
    def __init__(self, model_path: Path, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self.session = self._load_session()
        self.input_names = [node.name for node in self.session.get_inputs()]
        self.output_names = [node.name for node in self.session.get_outputs()]

    def _load_session(self) -> ort.InferenceSession:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found at {self.model_path}")
        
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            providers.insert(0, "CUDAExecutionProvider")
        elif self.device == "coreml":
            providers.insert(0, "CoreMLExecutionProvider")
            
        return ort.InferenceSession(str(self.model_path), providers=providers)

    def run(self, inputs: Dict[str, Any]) -> List[Any]:
        # Filter inputs that are not expected by the model
        filtered_inputs = {k: v for k, v in inputs.items() if k in self.input_names}
        # Check for missing required inputs? (ONNX runtime will handle this, but we can warn)
        return self.session.run(self.output_names, filtered_inputs)

    def verify_input_names(self, inputs: Dict[str, Any]):
        missing = [name for name in self.input_names if name not in inputs]
        # Some inputs might be optional, difficult to know from ONNX signature alone without metadata
        # But generally we should provide everything we can.
        if missing:
            logging.debug(f"Model {self.model_path.name} missing inputs: {missing}")

class LinguisticModel(DiffSingerModel):
    """
    Encoder model (linguistic.onnx).
    Inputs: tokens, word_div, word_dur, languages
    Outputs: encoder_out, x_masks
    """
    pass

class DurationModel(DiffSingerModel):
    """
    Duration Predictor (dur.onnx).
    Inputs: encoder_out, x_masks, ph_midi, spk_embed
    Outputs: duration
    """
    def forward(self, encoder_out: np.ndarray, x_masks: np.ndarray, ph_midi: np.ndarray, spk_embed: Optional[np.ndarray] = None) -> np.ndarray:
        inputs = {
            "encoder_out": encoder_out,
            "x_masks": x_masks,
            "ph_midi": ph_midi
        }
        if spk_embed is not None and "spk_embed" in self.input_names:
            inputs["spk_embed"] = spk_embed
            
        outputs = self.run(inputs)
        return outputs[0] # duration frames

class PitchModel(DiffSingerModel):
    """
    Pitch Predictor (pitch.onnx).
    Inputs: encoder_out, note_midi, note_dur, ph_dur, pitch, retake, speedup/steps, expr, spk_embed
    Outputs: pitch (f0)
    """
    pass

class VarianceModel(DiffSingerModel):
    """
    Variance Predictor (variance.onnx).
    Inputs: encoder_out, ph_dur, pitch, retake, speedup/steps, spk_embed
    Outputs: energy, breathiness, voicing, tension
    """
    pass

class AcousticModel(DiffSingerModel):
    """
    Acoustic Model (acoustic.onnx).
    Inputs: tokens, durations, f0, speedup/steps, languages, spk_embed, gender, velocity, energy, breathiness...
    Outputs: mel
    """
    pass
