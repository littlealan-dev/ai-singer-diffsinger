import types

import onnxruntime as ort

from src.acoustic.model import DiffSingerModel
from src.vocoder.model import Vocoder


class _DummySession:
    def get_inputs(self):
        return []

    def get_outputs(self):
        return []

    def run(self, *_args, **_kwargs):
        return []


def _capture_providers(monkeypatch, available):
    captured = {}

    def fake_get_available_providers():
        return list(available)

    def fake_session(path, providers=None, sess_options=None):
        captured["providers"] = providers
        captured["sess_options"] = sess_options
        return _DummySession()

    monkeypatch.setattr(ort, "get_available_providers", fake_get_available_providers)
    monkeypatch.setattr(ort, "InferenceSession", fake_session)
    return captured


def test_cuda_provider_used_when_available(monkeypatch, tmp_path):
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"dummy")
    captured = _capture_providers(monkeypatch, available=["CUDAExecutionProvider", "CPUExecutionProvider"])
    model = DiffSingerModel(model_path, device="cuda")
    assert captured["providers"][0] == "CUDAExecutionProvider"
    assert "CPUExecutionProvider" in captured["providers"]


def test_cuda_provider_falls_back(monkeypatch, tmp_path):
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"dummy")
    captured = _capture_providers(monkeypatch, available=["CPUExecutionProvider"])
    model = DiffSingerModel(model_path, device="cuda")
    assert captured["providers"] == ["CPUExecutionProvider"]


def test_vocoder_cuda_provider_used_when_available(monkeypatch, tmp_path):
    model_path = tmp_path / "vocoder.onnx"
    model_path.write_bytes(b"dummy")
    captured = _capture_providers(monkeypatch, available=["CUDAExecutionProvider", "CPUExecutionProvider"])
    vocoder = Vocoder(model_path, device="cuda")
    assert captured["providers"][0] == "CUDAExecutionProvider"
    assert "CPUExecutionProvider" in captured["providers"]
