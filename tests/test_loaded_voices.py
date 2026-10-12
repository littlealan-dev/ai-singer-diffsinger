from __future__ import annotations

from pathlib import Path

import pytest

import src.acoustic.model as acoustic_model
import src.api.inference as inference
import src.api.vocode as vocode_module
import src.vocoder.model as vocoder_model
from src import ort_memory
from src.api import loaded_voices


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    loaded_voices.reset()
    monkeypatch.delenv("SYNTHESIS_MAX_LOADED_VOICES", raising=False)
    yield
    loaded_voices.reset()


def test_a_third_voice_unloads_the_least_recently_used_one():
    cache: dict = {}
    for voice in ("/vb/a", "/vb/b"):
        loaded_voices.use(voice, cache, f"acoustic:{voice}")
        cache[f"acoustic:{voice}"] = object()
    # Using A again makes B the least recently used.
    loaded_voices.use("/vb/a", cache, "acoustic:/vb/a")

    loaded_voices.use("/vb/c", cache, "acoustic:/vb/c")
    cache["acoustic:/vb/c"] = object()

    assert sorted(cache) == ["acoustic:/vb/a", "acoustic:/vb/c"]


def test_an_entry_another_loaded_voice_uses_is_kept():
    cache: dict = {"shared": object(), "only_a": object()}
    loaded_voices.use("/vb/a", cache, "shared")
    loaded_voices.use("/vb/a", cache, "only_a")
    loaded_voices.use("/vb/b", cache, "shared")

    loaded_voices.use("/vb/c", cache, "only_c")

    assert "shared" in cache
    assert "only_a" not in cache


def test_the_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SYNTHESIS_MAX_LOADED_VOICES", "1")
    cache: dict = {}
    loaded_voices.use("/vb/a", cache, "a")
    cache["a"] = object()

    loaded_voices.use("/vb/b", cache, "b")

    assert "a" not in cache


def test_models_and_vocoders_of_an_unloaded_voice_leave_their_caches(monkeypatch):
    class FakeModel:
        def __init__(self, model_path, device):
            self.model_path = model_path

    monkeypatch.setattr(inference, "_model_cache", {})
    monkeypatch.setattr(vocode_module, "_vocoder_cache", {})
    monkeypatch.setattr(vocode_module, "Vocoder", FakeModel)
    monkeypatch.setattr(
        vocode_module, "resolve_vocoder_model_path", lambda path: Path(path) / "vocoder.onnx"
    )

    for voice in ("a", "b", "c"):
        voicebank = Path("/vb") / voice
        inference._get_model(FakeModel, voicebank / "acoustic.onnx", "cuda", voicebank=voicebank)
        vocode_module._load_vocoder(voicebank, "cuda")

    assert sorted(inference._model_cache) == [
        "FakeModel:/vb/b/acoustic.onnx",
        "FakeModel:/vb/c/acoustic.onnx",
    ]
    assert sorted(vocode_module._vocoder_cache) == ["vocoder:/vb/b:cuda", "vocoder:/vb/c:cuda"]


class _FakeSession:
    def __init__(self, path, providers, sess_options):
        self.providers = providers
        self.run_calls = []

    def get_providers(self):
        return [p[0] if isinstance(p, tuple) else p for p in self.providers]

    def get_inputs(self):
        return []

    def get_outputs(self):
        return []

    def run(self, output_names, inputs, run_options=None):
        self.run_calls.append(run_options)
        return [None]


@pytest.mark.parametrize(
    "module, factory",
    [
        (acoustic_model, lambda path: acoustic_model.AcousticModel(path, "cuda")),
        (vocoder_model, lambda path: vocoder_model.Vocoder(path, "cuda")),
    ],
)
def test_cuda_sessions_grow_as_requested_and_shrink_after_each_run(
    module, factory, monkeypatch, tmp_path
):
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"")
    monkeypatch.setattr(module.ort, "get_available_providers", lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(module.ort, "InferenceSession", _FakeSession)

    model = factory(model_path)
    if isinstance(model, vocoder_model.Vocoder):
        model.forward(mel=None, f0=None)
    else:
        model.run({})

    assert model.session.providers[0] == (
        "CUDAExecutionProvider",
        {"arena_extend_strategy": "kSameAsRequested"},
    )
    (run_options,) = model.session.run_calls
    assert run_options.get_run_config_entry("memory.enable_memory_arena_shrinkage") == "gpu:0"


def test_cpu_sessions_run_without_pool_options(monkeypatch, tmp_path):
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"")
    monkeypatch.setattr(acoustic_model.ort, "InferenceSession", _FakeSession)

    model = acoustic_model.AcousticModel(model_path, "cpu")
    model.run({})

    assert model.session.run_calls == [None]
    assert ort_memory.run_options_for(model.session) is None
