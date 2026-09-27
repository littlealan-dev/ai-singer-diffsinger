from datetime import datetime
import logging

import pytest

import src.backend.job_store as job_store_module
from src.backend.job_store import JobStore, build_progress_payload


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data or {})


class _JobRef:
    def __init__(self, data):
        self.data = data

    def get(self, transaction=None):
        return _Snapshot(self.data)


class _Collection:
    def __init__(self, ref):
        self.ref = ref

    def document(self, _job_id):
        return self.ref


class _Transaction:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.writes = []

    def set(self, ref, payload, merge=False):
        if self.fail:
            raise RuntimeError("transaction write failed")
        self.writes.append((ref, payload, merge))


class _Client:
    def __init__(self, data, *, fail=False):
        self.ref = _JobRef(data)
        self.transaction_value = _Transaction(fail=fail)

    def collection(self, _name):
        return _Collection(self.ref)

    def transaction(self):
        return self.transaction_value


def _progress_store(monkeypatch, data, *, fail=False):
    monkeypatch.setattr(job_store_module.firestore, "transactional", lambda func: func)
    store = JobStore()
    store._client = _Client(data, fail=fail)
    return store


def test_job_store_progress_update_writes_while_job_is_running(monkeypatch):
    store = _progress_store(monkeypatch, {"status": "running"})

    assert store.update_job_progress(
        "job-123",
        status="running",
        step="synthesize",
        message="Generating audio...",
        progress=0.75,
    ) is True

    writes = store._client.transaction_value.writes
    assert len(writes) == 1
    _, payload, merge = writes[0]
    assert merge is True
    assert payload["status"] == "running"
    assert payload["step"] == "synthesize"
    assert payload["message"] == "Generating audio..."
    assert payload["progress"] == 0.75
    assert payload["updatedAt"] is job_store_module.firestore.SERVER_TIMESTAMP


@pytest.mark.parametrize(
    "terminal_status",
    ["completed", "failed", "cancelled", "action_required", "credit_reconciliation_required"],
)
def test_job_store_progress_update_preserves_terminal_job(
    monkeypatch, caplog, terminal_status
):
    original = {
        "status": terminal_status,
        "step": "terminal",
        "message": "Terminal message.",
        "progress": 1.0,
    }
    store = _progress_store(monkeypatch, original)

    with caplog.at_level(logging.INFO):
        updated = store.update_job_progress(
            "job-terminal",
            status="running",
            step="synthesize",
            message="Taking a breath for the take...",
            progress=0.8,
        )

    assert updated is False
    assert store._client.transaction_value.writes == []
    assert store._client.ref.data == original
    assert f"current_status={terminal_status}" in caplog.text


def test_job_store_progress_update_logs_transaction_failure(monkeypatch, caplog):
    store = _progress_store(monkeypatch, {"status": "running"}, fail=True)

    with caplog.at_level(logging.WARNING), pytest.raises(
        RuntimeError, match="transaction write failed"
    ):
        store.update_job_progress(
            "job-failed-write",
            status="running",
            step="synthesize",
            message="Generating audio...",
            progress=0.8,
        )

    assert "job_progress_update_failed job_id=job-failed-write" in caplog.text


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


def test_build_progress_payload_includes_audio_track_metadata():
    payload = build_progress_payload(
        "job-track",
        {
            "status": "completed",
            "audioUrl": "/sessions/abc/audio",
            "audioTrack": {
                "key": "id:Soprano",
                "label": "Soprano",
                "part_id": "Soprano",
                "part_index": 0,
                "verse_number": "1",
            },
        },
    )
    assert payload["audio_track"] == {
        "key": "id:Soprano",
        "label": "Soprano",
        "part_id": "Soprano",
        "part_index": 0,
        "verse_number": "1",
    }


def test_progress_payload_reports_whether_this_job_published_midi():
    """Billing cannot answer this, so the payload must say it explicitly.

    A revision of an already-paid score regenerates MIDI while charging zero
    instrumental credits, so the UI cannot infer freshness from the breakdown.
    """
    performance_midi = {"version": 1, "has_instrumental_parts": True}

    published = build_progress_payload(
        "job-published",
        {
            "status": "completed",
            "performanceMidi": performance_midi,
            "performanceMidiPaths": {"scoreVersionNo": 2, "originalPath": "a.mid"},
        },
    )
    assert published["performance_midi_published"] is True

    # A vocal-only take on an already-generated score still carries the
    # metadata, but wrote nothing, so the UI must not churn its track set.
    reused = build_progress_payload(
        "job-vocal-only",
        {"status": "completed", "performanceMidi": performance_midi},
    )
    assert reused["performance_midi"] == performance_midi
    assert reused["performance_midi_published"] is False

    # Server-side file locations never reach the client.
    assert "performance_midi_paths" not in published
    assert "performanceMidiPaths" not in published


def test_build_progress_payload_publishes_midi_and_credit_breakdown_at_completion():
    performance_midi = {
        "version": 1,
        "has_instrumental_parts": True,
        "instrumental_parts": [{"part_id": "P2", "eligible": True}],
    }
    credit_breakdown = {
        "estimated": {
            "vocal_part_credits": 2,
            "instrumental_credits": 1,
            "total_credits": 3,
        },
        "actual": {
            "duration_seconds": 119.8,
            "vocal_part_credits": 4,
            "instrumental_credits": 1,
            "total_credits": 5,
        },
        "quote_id": "quote-1",
        "pricing_version": 1,
    }
    payload = build_progress_payload(
        "job-components",
        {
            "status": "completed",
            "performanceMidi": performance_midi,
            "creditBreakdown": credit_breakdown,
        },
    )
    assert payload["performance_midi"] == performance_midi
    assert payload["credit_breakdown"] == credit_breakdown


def test_build_progress_payload_includes_export_mix_job_kind():
    payload = build_progress_payload(
        "job-mix",
        {
            "status": "running",
            "progress": 0.42,
            "jobKind": "export_mix",
        },
    )
    assert payload["job_kind"] == "export_mix"
    assert payload["progress"] == 0.42


def test_build_progress_payload_includes_export_mix_billing_fields():
    payload = build_progress_payload(
        "job-mix-billing",
        {
            "status": "completed",
            "jobKind": "export_mix",
            "actualDurationSeconds": 121.2,
            "consumedCredits": 3,
            "billing": {
                "requiredCredits": 3,
                "billableDurationSeconds": 121.2,
                "pricing": "export_mix_v1",
            },
        },
    )
    assert payload["status"] == "done"
    assert payload["actual_duration_seconds"] == 121.2
    assert payload["consumed_credits"] == 3
    assert payload["required_credits"] == 3
    assert payload["billable_duration_seconds"] == 121.2
    assert payload["billing"]["pricing"] == "export_mix_v1"


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
