from datetime import datetime

from src.backend.job_store import build_progress_payload


def test_build_progress_payload_maps_status_and_fields():
    payload = build_progress_payload(
        "job-123",
        {
            "status": "completed",
            "step": "done",
            "message": "Ready.",
            "progress": 1.0,
            "audioUrl": "/sessions/abc/audio",
            "updatedAt": datetime(2026, 1, 1),
        },
    )
    assert payload["status"] == "done"
    assert payload["job_id"] == "job-123"
    assert payload["audio_url"] == "/sessions/abc/audio"
    assert payload["progress"] == 1.0


def test_build_progress_payload_maps_error_status():
    payload = build_progress_payload(
        "job-err",
        {
            "status": "failed",
            "errorMessage": "boom",
        },
    )
    assert payload["status"] == "error"
    assert payload["error"] == "boom"


def test_build_progress_payload_includes_preprocess_review_fields():
    payload = build_progress_payload(
        "job-pre",
        {
            "status": "completed",
            "message": "Please review the derived score.",
            "jobKind": "preprocess",
            "reviewRequired": True,
        },
    )
    assert payload["status"] == "done"
    assert payload["job_kind"] == "preprocess"
    assert payload["review_required"] is True
