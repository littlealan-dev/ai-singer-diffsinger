from datetime import datetime, timedelta, timezone
import json

from scripts.sing_all_report import summarize


def test_summarize_uses_stored_fields_and_outputs_only_aggregates():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    jobs = [
        {
            "jobId": "fictional-job-id", "uid": "fictional-owner", "title": "Private Example Song",
            "createdAt": start, "endedAt": start + timedelta(seconds=70),
            "status": "completed", "choices": {"mode": "lyrics", "mixes": "all", "repeats": True},
            "items": [
                {"type": "take", "status": "done", "creditsReserved": 5, "creditsCharged": 4},
                {"type": "take", "status": "done", "creditsReserved": 5, "creditsCharged": 5},
                {"type": "mixes", "status": "done", "creditsReserved": 3, "creditsCharged": 2},
            ],
            "package": {"state": "done", "zipPath": "fictional-private-path"}, "zipDownloads": 3,
        },
        {
            "createdAt": start, "endedAt": start + timedelta(seconds=110),
            "retryOf": "fictional-job-id", "status": "completed",
            "choices": {"mode": "lyrics", "mixes": "full", "repeats": False},
            "items": [
                {"type": "take", "status": "done", "carried": True},
                {"type": "take", "status": "done", "creditsReserved": 5, "creditsCharged": 5},
                {"type": "take", "status": "done", "creditsReserved": 5, "creditsCharged": 3},
            ],
            "package": {"state": "done"},
        },
        {
            "createdAt": start, "status": "partial", "endReason": "stuck",
            "items": [
                {"type": "take", "status": "done", "creditsReserved": 5, "creditsCharged": 5},
                {"type": "take", "status": "failed", "creditsReserved": 5},
            ],
            "decision": {"parts": [{"reason": "split_failed"}, {"reason": "split_failed"}],
                         "answer": {"action": "continue"}},
            "package": {"state": "failed", "error": "mix_failed:RuntimeError"},
        },
        {
            "createdAt": start, "status": "cancelled", "endReason": "decision_timeout",
            "items": [{"type": "take", "status": "skipped", "creditsReserved": 5}],
            "decision": {"parts": [{"reason": "split_review"}]},
        },
        {
            "createdAt": start, "status": "cancelled", "endReason": "decision_cancelled",
            "decision": {"parts": [{"reason": "parts_changed"}], "answer": {"action": "cancel"}},
        },
        {
            "createdAt": start, "status": "needs_decision",
            "items": [{"type": "take", "status": "queued", "creditsReserved": 5}],
            "decision": {"parts": [{"reason": "solfege_failed"}]},
        },
        {"createdAt": end, "status": "failed"},
        {"createdAt": start - timedelta(microseconds=1), "status": "completed"},
    ]
    report = summarize(jobs, start, end)
    assert report == {
        "jobs_started": 6, "retry_jobs": 1, "retry_share": 1 / 6,
        "outcomes": {"completed": 2, "partial": 1, "failed": 0, "cancelled": 2},
        "end_reasons": {"unspecified": 2, "stuck": 1, "decision_timeout": 1, "decision_cancelled": 1},
        "paused_jobs": 4, "pause_rate": 4 / 6,
        "pause_reasons": {"split_failed": 1, "split_review": 1, "parts_changed": 1, "solfege_failed": 1},
        "pause_answers": {"continue": 1, "timed_out": 1, "cancel": 1},
        "takes": {"done": 5, "planned": 8, "share": 5 / 8},
        "credits": {"reserved": 43, "charged": 24, "refunded": 14, "refund_share": 14 / 43},
        "time_to_finish_by_takes": {"2-4": {"jobs": 2, "median_seconds": 90.0, "p90_seconds": 110.0}},
        "choices": {
            "mode": {"lyrics": 2, "unspecified": 4},
            "mixes": {"all": 1, "full": 1, "none": 4},
            "repeats": {"true": 1, "false": 1, "unspecified": 4},
        },
        "zip_downloads": {"completed_with_download": 1, "completed_with_zip": 2, "share": 0.5},
        "packaging_failures": {"mix_failed:RuntimeError": 1},
    }
    data = json.dumps(report)
    for private in ("fictional-job-id", "fictional-owner", "Private Example Song", "fictional-private-path"):
        assert private not in data


def test_empty_period_and_unexpected_strings_are_safe():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    empty = summarize([], start, end)
    assert empty["jobs_started"] == 0
    assert empty["takes"]["share"] == empty["credits"]["refund_share"] == 0
    assert empty["zip_downloads"]["share"] == empty["retry_share"] == empty["pause_rate"] == 0

    report = summarize([{
        "createdAt": start.isoformat(), "status": "failed", "endReason": "fictional-private-id",
        "package": {"state": "failed", "error": "mix_failed:fictional-private-id"},
    }], start, end)
    assert report["end_reasons"] == {"other": 1}
    assert report["packaging_failures"] == {"mix_failed": 1}
    assert "fictional-private-id" not in json.dumps(report)
