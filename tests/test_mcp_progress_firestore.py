from pathlib import Path

import src.mcp.handlers as handlers


def test_synthesize_progress_updates_firestore(monkeypatch, tmp_path):
    updated = []
    lyric_selection = {"id": "lyrics-1", "number": "1", "name": "Verse 1"}

    class FakeJobStore:
        def update_job_progress(self, job_id: str, **fields):
            updated.append((job_id, fields))

    def fake_synthesize(_score, _voicebank, **kwargs):
        progress_cb = kwargs.get("progress_callback")
        if progress_cb:
            progress_cb("align", "Reading the lyrics and score...", 0.1)
            progress_cb("pitch", "Finding the melody line...", 0.5)
        return {"waveform": [0.0], "sample_rate": 44100, "duration_seconds": 1.0}

    monkeypatch.setattr(handlers, "JobStore", lambda: FakeJobStore())
    monkeypatch.setattr(handlers, "initialize_firebase_app", lambda: None)
    monkeypatch.setattr(handlers, "synthesize", fake_synthesize)
    monkeypatch.setattr(handlers, "resolve_voicebank_id", lambda _voicebank: tmp_path)

    result = handlers.handle_synthesize(
        {
            "score": {"selected_lyric_selection": lyric_selection},
            "lyric_selection": lyric_selection,
            "voicebank": "Raine_Rena_2.01",
            "progress_job_id": "job-123",
        },
        device="cpu",
    )

    assert result["sample_rate"] == 44100
    assert updated
    job_id, fields = updated[0]
    assert job_id == "job-123"
    assert fields == {
        "status": "running",
        "step": "align",
        "message": "Reading the lyrics and score...",
        "progress": 0.1,
    }


def test_synthesize_stop_callbacks_read_job_and_pass_stop_point(monkeypatch, tmp_path):
    lyric_selection = {"id": "lyrics-1", "number": "1", "name": "Verse 1"}
    state = {"status": "running"}
    passed = []

    class FakeJobStore:
        def get_job(self, job_id):
            assert job_id == "job-123"
            return dict(state)

        def pass_stop_point(self, job_id):
            passed.append(job_id)
            return state["status"] != "cancelled"

    def fake_synthesize(_score, _voicebank, **kwargs):
        should_stop = kwargs["stop_check"]
        before_vocoder = kwargs["pass_stop_point"]
        assert should_stop("align") is False
        assert before_vocoder() is True
        state["status"] = "cancelled"
        assert should_stop("pitch") is True
        assert before_vocoder() is False
        return {"status": "stopped"}

    monkeypatch.setattr(handlers, "JobStore", FakeJobStore)
    monkeypatch.setattr(handlers, "initialize_firebase_app", lambda: None)
    monkeypatch.setattr(handlers, "synthesize", fake_synthesize)
    monkeypatch.setattr(handlers, "resolve_voicebank_id", lambda _voicebank: tmp_path)

    result = handlers.handle_synthesize(
        {
            "score": {"selected_lyric_selection": lyric_selection},
            "lyric_selection": lyric_selection,
            "voicebank": "Raine_Rena_2.01",
            "progress_job_id": "job-123",
        },
        device="cpu",
    )
    assert result == {"status": "stopped"}
    assert passed == ["job-123", "job-123"]
