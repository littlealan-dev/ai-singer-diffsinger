import numpy as np
import pytest
import soundfile as sf

from src.backend.audio_mix import MixTrackSource, render_mix_to_wav


def test_render_mix_to_wav_applies_volume_and_duration(tmp_path):
    sample_rate = 8000
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    output = tmp_path / "mix.wav"
    sf.write(first, np.full((sample_rate // 2, 1), 0.25, dtype=np.float32), sample_rate)
    sf.write(second, np.full((sample_rate, 1), 0.5, dtype=np.float32), sample_rate)

    progress_values: list[float] = []
    metadata = render_mix_to_wav(
        [
            MixTrackSource("id:Soprano", "Soprano", "Soprano", first, 1.0),
            MixTrackSource("id:Alto", "Alto", "Alto", second, 0.5),
        ],
        output,
        progress_callback=progress_values.append,
    )

    data, rate = sf.read(output, dtype="float32", always_2d=True)
    assert rate == sample_rate
    assert metadata == {"duration_seconds": 1.0, "sample_rate": sample_rate, "channels": 1}
    assert np.allclose(data[: sample_rate // 2, 0], 0.5, atol=1e-4)
    assert np.allclose(data[sample_rate // 2 :, 0], 0.25, atol=1e-4)
    assert progress_values[0] == 0.0
    assert progress_values[-1] == 1.0


def test_render_mix_to_wav_rejects_sample_rate_mismatch(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    sf.write(first, np.zeros((8, 1), dtype=np.float32), 8000)
    sf.write(second, np.zeros((8, 1), dtype=np.float32), 16000)

    with pytest.raises(ValueError, match="sample rate mismatch"):
        render_mix_to_wav(
            [
                MixTrackSource("id:Soprano", "Soprano", "Soprano", first, 1.0),
                MixTrackSource("id:Alto", "Alto", "Alto", second, 1.0),
            ],
            tmp_path / "mix.wav",
        )
