"""Public progress for made-up Sing All parent records."""

from datetime import datetime, timedelta, timezone

import pytest

from src.backend.sing_all import progress


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def _job(*, stage="reading", status="running"):
    return {
        "jobId": "parent-a", "sessionId": "song-a", "title": "Example Tune",
        "status": status, "stage": stage, "endReason": None,
        "cancelRequested": False,
        "choices": {"rows": [], "language": "en", "solfegeOnly": False,
                    "mode": "both", "verse": "1", "repeats": False},
        "quote": {"total": 6},
        "rows": [
            {"rowId": "line-a", "sourcePartId": "P1", "partName": "Duet",
             "name": "Upper", "voice": "voice-a", "voiceName": "Voice A",
             "split": True, "include": True},
            {"rowId": "line-b", "sourcePartId": "P2", "name": "Unticked",
             "voice": "voice-b", "split": False, "include": False},
        ],
        "items": [
            {"itemId": "take:line-a:lyrics", "type": "take", "rowId": "line-a",
             "mode": "lyrics", "status": "queued", "reason": None,
             "creditsReserved": 3, "creditsCharged": 0, "estimatedSeconds": 75},
            {"itemId": "take:line-a:solfege", "type": "take", "rowId": "line-a",
             "mode": "solfege", "status": "queued", "reason": None,
             "creditsReserved": 3, "creditsCharged": 0, "estimatedSeconds": 75},
            {"itemId": "split:P1", "type": "split", "sourcePartId": "P1",
             "status": "queued", "reason": None,
             "creditsReserved": 0, "creditsCharged": 0},
        ],
        "notes": [{"code": "repeats_written_order", "partId": None, "partName": None}],
        "render": {"state": "none"},
        "createdAt": NOW - timedelta(seconds=20), "endedAt": None,
    }


@pytest.mark.parametrize("stage,remaining,split_done", [
    ("reading", 375, False), ("splitting", 375, False),
    ("solfege", 135, True), ("singing", 75, True),
])
def test_running_progress_has_contract_fields_and_quote_order(stage, remaining, split_done):
    job = _job(stage=stage)
    if split_done:
        job["items"][2]["status"] = "done"
    result = progress(job, now=NOW)
    assert set(result) == {
        "jobId", "songId", "title", "status", "stage", "endReason",
        "cancelRequested", "choices", "counts", "rows", "credits", "receipt",
        "notes", "elapsedS", "remainingS", "render", "createdAt", "endedAt",
    }
    assert (result["jobId"], result["songId"], result["title"]) == (
        "parent-a", "song-a", "Example Tune")
    assert (result["status"], result["stage"], result["endReason"]) == (
        "running", stage, None)
    assert result["choices"] == {"language": "en", "solfegeOnly": False,
                                 "mode": "both", "verse": "1", "repeats": False}
    assert result["counts"] == {"splits": {"done": int(split_done), "total": 1},
                                 "takes": {"done": 0, "failed": 0, "total": 2}}
    assert result["rows"] == [{
        "rowId": "line-a", "name": "Upper", "voice": "voice-a",
        "voiceName": "Voice A", "split": True,
        "takes": [{"mode": "lyrics", "status": "queued", "reason": None},
                  {"mode": "solfege", "status": "queued", "reason": None}],
    }]
    assert result["credits"] == {"reserved": 6, "charged": 0, "refunded": 0}
    assert result["receipt"] == []
    assert result["notes"] == [{"code": "repeats_written_order",
                                "partId": None, "partName": None}]
    assert (result["elapsedS"], result["remainingS"]) == (20, remaining)
    assert result["render"] == {"state": "none", "placeInLine": None}
    assert result["createdAt"] == "2026-01-02T11:59:40+00:00"
    assert result["endedAt"] is None


def test_waiting_render_shows_current_place_in_line():
    job = _job(stage="singing")
    job["render"] = {"state": "waiting", "placeInLine": 2}
    assert progress(job, now=NOW)["render"] == {"state": "waiting", "placeInLine": 2}
    job["render"]["state"] = "running"
    assert progress(job, now=NOW)["render"]["placeInLine"] is None


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "cancelled"])
def test_ended_progress_has_receipt_and_no_remaining_time(status):
    job = _job(stage="done", status=status)
    job["items"][0].update(status="done", creditsCharged=2)
    job["items"][1].update(status="stopped", reason="stopped")
    job["items"][2].update(status="done")
    job["endReason"] = "instrumental_failed"
    job["endedAt"] = NOW
    result = progress(job, now=NOW + timedelta(seconds=5))
    assert set(result) == {
        "jobId", "songId", "title", "status", "stage", "endReason",
        "cancelRequested", "choices", "counts", "rows", "credits", "receipt",
        "notes", "elapsedS", "remainingS", "render", "createdAt", "endedAt",
    }
    assert result["endReason"] == "instrumentals_failed"
    assert result["counts"] == {"splits": {"done": 1, "total": 1},
                                 "takes": {"done": 1, "failed": 1, "total": 2}}
    assert result["credits"] == {"reserved": 0, "charged": 2, "refunded": 4}
    assert result["elapsedS"] == 20
    assert result["remainingS"] is None
    assert result["render"] == {"state": "none", "placeInLine": None}
    assert result["createdAt"] == "2026-01-02T11:59:40+00:00"
    assert result["endedAt"] == NOW.isoformat()
    assert [(item["type"], item["name"], item["credits"], item["outcome"])
            for item in result["receipt"]] == [
        ("take", "Upper", 2, "charged"),
        ("take", "Upper", 3, "refunded"),
        ("split", "Duet", 0, "included"),
    ]
