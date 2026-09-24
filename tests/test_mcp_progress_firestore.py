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
            "progress_user_id": "user-123",
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


def test_job_store_progress_update_only_forwards_mutable_fields():
    updated = []

    class RecordingJobStore(handlers.JobStore):
        def update_job(self, job_id: str, **fields):
            updated.append((job_id, fields))

    RecordingJobStore().update_job_progress(
        "job-123",
        status="running",
        step="synthesize",
        message="Generating audio...",
        progress=0.75,
    )

    assert updated == [
        (
            "job-123",
            {
                "status": "running",
                "step": "synthesize",
                "message": "Generating audio...",
                "progress": 0.75,
            },
        )
    ]
