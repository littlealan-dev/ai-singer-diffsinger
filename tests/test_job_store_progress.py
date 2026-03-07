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


def test_build_progress_payload_maps_credit_reconciliation_status_to_error():
    payload = build_progress_payload(
        "job-billing",
        {
            "status": "credit_reconciliation_required",
            "message": "Billing finalization failed.",
            "errorMessage": "settle_failed",
            "outputPath": "sessions/u/s/j/audio.wav",
            "audioUrl": "/sessions/abc/audio",
        },
    )
    assert payload["status"] == "error"
    assert payload["message"] == "Billing finalization failed."
    assert payload["error"] == "settle_failed"
    assert "audio_url" not in payload


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


def test_build_progress_payload_includes_details():
    payload = build_progress_payload(
        "job-details",
        {
            "status": "completed",
            "message": "Please review the candidate.",
            "details": {
                "quality_class": 2,
                "issues": [{"rule": "validation_failed_needs_review"}],
            },
        },
    )
    assert payload["status"] == "done"
    assert payload["details"]["quality_class"] == 2
    assert payload["details"]["issues"][0]["rule"] == "validation_failed_needs_review"


def test_build_progress_payload_includes_action_required():
    payload = build_progress_payload(
        "job-action",
        {
            "status": "completed",
            "message": "Please pick a verse.",
            "actionRequired": {
                "status": "action_required",
                "action": "verse_selection_required",
                "available_verses": ["1", "2"],
            },
        },
    )
    assert payload["status"] == "done"
    assert payload["action_required"]["action"] == "verse_selection_required"
    assert payload["action_required"]["available_verses"] == ["1", "2"]
