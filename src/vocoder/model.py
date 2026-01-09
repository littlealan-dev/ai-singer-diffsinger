import onnxruntime as ort
import numpy as np
import logging
import os
from pathlib import Path
from typing import Dict, Any

class Vocoder:
    """
    HiFi-GAN Vocoder (vocoder.onnx).
    Inputs: mel, f0
    Outputs: waveform
    """
    def __init__(self, model_path: Path, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self.session = self._load_session()
        self.input_names = [node.name for node in self.session.get_inputs()]
        self.output_names = [node.name for node in self.session.get_outputs()]

    def _load_session(self) -> ort.InferenceSession:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found at {self.model_path}")
        
        available = ort.get_available_providers()
        providers = ["CPUExecutionProvider"]
        if self.device == "cuda":
            if "CUDAExecutionProvider" in available:
                providers.insert(0, "CUDAExecutionProvider")
            else:
                logging.warning(
                    "cuda_provider_unavailable model=%s available=%s",
                    self.model_path.name,
                    available,
                )
        elif self.device == "coreml":
            if "CoreMLExecutionProvider" in available:
                providers.insert(0, "CoreMLExecutionProvider")
            else:
                logging.warning(
                    "coreml_provider_unavailable model=%s available=%s",
                    self.model_path.name,
                    available,
                )
            
        opts = ort.SessionOptions()
        intra_threads = os.getenv("ORT_INTRA_OP_NUM_THREADS")
        inter_threads = os.getenv("ORT_INTER_OP_NUM_THREADS")
        if intra_threads:
            opts.intra_op_num_threads = int(intra_threads)
        if inter_threads:
            opts.inter_op_num_threads = int(inter_threads)
        logging.info(
            "ort_session_config model=%s providers=%s intra_threads=%s inter_threads=%s",
            self.model_path.name,
            providers,
            opts.intra_op_num_threads,
            opts.inter_op_num_threads,
        )
        return ort.InferenceSession(str(self.model_path), providers=providers, sess_options=opts)

    def forward(self, mel: np.ndarray, f0: np.ndarray) -> np.ndarray:
        """
        Args:
            mel: [B, T, n_mel] (or [B, n_mel, T]? Check usage)
            f0: [B, T]
        Returns:
            waveform: [B, output_len]
        """
        inputs = {
            "mel": mel,
            "f0": f0
        }
        outputs = self.session.run(self.output_names, inputs)
        return outputs[0]
