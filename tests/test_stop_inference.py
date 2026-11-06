import src.api.inference as inference


def test_stop_after_mel_skips_vocoder(monkeypatch):
    calls = []
    monkeypatch.setattr(
        inference,
        "synthesize_mel",
        lambda **kwargs: calls.append("mel") or {"mel": [[0.0]]},
    )
    monkeypatch.setattr(
        inference,
        "vocode",
        lambda **kwargs: calls.append("vocoder") or {"waveform": [0.0], "sample_rate": 44100},
    )
    result = inference.synthesize_audio(
        phoneme_ids=[1], durations=[1], f0=[440.0], voicebank="unused",
        before_vocoder=lambda: False,
    )
    assert result == {"status": "stopped", "stage": "vocoder"}
    assert calls == ["mel"]
