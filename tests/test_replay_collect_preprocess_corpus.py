from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay" / "collect_preprocess_corpus.py"
_spec = importlib.util.spec_from_file_location("collect_preprocess_corpus", _SCRIPT)
collector = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = collector
_spec.loader.exec_module(collector)

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def _job(job_id: str = "job-a", **overrides):
    job = {
        "job_id": job_id,
        "jobKind": "preprocess",
        "status": "completed",
        "createdAt": NOW - timedelta(days=1),
        "completedAt": NOW - timedelta(days=1) + timedelta(seconds=12),
        "inputPath": f"scores/{job_id}.xml",
        "inputSha256": f"sha-{job_id}",
        "inputFileName": "song.xml",
        "scoreTitle": "Song",
        "demoSongId": None,
        "expandRepeats": False,
        "step": "done",
        "reviewRequired": True,
        "message": "finished",
        "errorMessage": None,
        "warningMessage": None,
        "details": {"quality_class": "good"},
        "preprocessSpecJson": json.dumps({
            "toolCalls": [{"name": "plan", "arguments": {}}],
            "planningContext": {"target": {"voice": "alto"}, "other_instruction": "", "user_request": "Sing"},
            "initialMessage": "Start",
            "initialThoughtSummary": "Plan",
            "userEmail": "singer@example.com",
            "originatingTurnId": "turn-secret",
            "llmCallsUsed": 2,
        }),
    }
    job.update(overrides)
    return job


def test_selection_filters_status_kind_window_spec_and_path():
    jobs = [
        _job(),
        _job("running", status="running"),
        _job("wrong-kind", jobKind="synthesis"),
        _job("old", createdAt=NOW - timedelta(days=31)),
        _job("no-spec", preprocessSpecJson="not json"),
        _job("array-spec", preprocessSpecJson="[]"),
        _job("no-path", inputPath=""),
        _job("failed", status="failed"),
    ]
    result = collector.select_jobs(jobs, days=30, max_jobs=200, now=NOW)
    assert {job["job_id"] for job in result.jobs} == {"job-a", "failed"}
    assert result.scanned == len(jobs)


def test_selection_dedupes_newest_using_canonical_context_and_caps_kept_jobs():
    older = _job("older", inputSha256="same", createdAt=NOW - timedelta(days=2))
    newer = _job("newer", inputSha256="same")
    different = _job("different")
    # The same planning context in another key order is still the same request.
    spec = json.loads(newer["preprocessSpecJson"])
    spec["planningContext"] = {"user_request": "Sing", "target": {"voice": "alto"}, "other_instruction": ""}
    newer["preprocessSpecJson"] = json.dumps(spec)
    all_jobs = collector.select_jobs([older, different, newer], 30, 200, NOW)
    assert [job["job_id"] for job in all_jobs.jobs] == ["different", "newer"]
    assert all_jobs.deduped == 1
    capped = collector.select_jobs([older, different, newer], 30, 1, NOW)
    assert len(capped.jobs) == 1


def test_query_uses_existing_status_updated_at_index_and_unions_documents():
    calls = []

    class FakeSnapshot:
        def __init__(self, job_id, status):
            self.id = job_id
            self.status = status

        def to_dict(self):
            return {"status": self.status, "jobKind": "preprocess"}

    class FakeQuery:
        def __init__(self, filters=(), ordering=None):
            self.filters = filters
            self.ordering = ordering

        def where(self, *, filter):
            return FakeQuery(self.filters + (filter,), self.ordering)

        def order_by(self, field, *, direction):
            return FakeQuery(self.filters, (field, direction))

        def stream(self):
            calls.append((self.filters, self.ordering))
            status = self.filters[1][2]
            return [FakeSnapshot(f"{status}-job", status)]

    class FakeClient:
        def collection(self, name):
            assert name == "jobs"
            return FakeQuery()

    class FakeFirestore:
        FieldFilter = staticmethod(lambda field, operation, value: (field, operation, value))

        class Query:
            DESCENDING = "descending"

    jobs = list(collector._query_jobs(NOW, 30, client=FakeClient(), firestore_module=FakeFirestore))
    cutoff = NOW - timedelta(days=30)
    assert calls == [
        (
            (("jobKind", "==", "preprocess"), ("status", "==", status), ("updatedAt", ">=", cutoff)),
            ("updatedAt", "descending"),
        )
        for status in ("completed", "failed")
    ]
    assert jobs == [
        {"job_id": "completed-job", "status": "completed", "jobKind": "preprocess"},
        {"job_id": "failed-job", "status": "failed", "jobKind": "preprocess"},
    ]


def test_write_entry_preserves_bytes_and_matches_private_safe_interface(tmp_path):
    job = _job(
        userId="user-secret", sessionId="session-secret",
        details={"quality_class": "good", "userId": "user-secret", "note": "singer@example.com; session-secret"},
    )
    spec = json.loads(job["preprocessSpecJson"])
    spec["toolCalls"][0]["arguments"] = {"email": "singer@example.com", "text": "hello singer@example.com"}
    spec["initialMessage"] = "start for user-secret"
    job["preprocessSpecJson"] = json.dumps(spec)
    xml = b"\x00<score>\xff</score>"
    metadata = collector.write_corpus_entry(tmp_path, "job-a", job, xml)
    assert (tmp_path / "job-a" / "input.xml").read_bytes() == xml
    raw_json = (tmp_path / "job-a" / "job.json").read_text(encoding="utf-8")
    assert raw_json == json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert set(metadata) == {
        "job_id", "created_at", "completed_at", "expand_repeats", "input_sha256",
        "input_file_name", "score_title", "demo_song_id", "spec", "prod_outcome",
    }
    assert set(metadata["spec"]) == {"toolCalls", "planningContext", "initialMessage", "initialThoughtSummary"}
    assert set(metadata["prod_outcome"]) == {
        "status", "step", "review_required", "message", "error_message",
        "warning_message", "details", "duration_s",
    }
    assert metadata["prod_outcome"]["duration_s"] == 12.0
    assert metadata["created_at"].endswith("+00:00")
    assert metadata["prod_outcome"]["details"]["quality_class"] == "good"
    for private_value in ("userId", "sessionId", "userEmail", "originatingTurnId", "llmCallsUsed", "user-secret", "session-secret", "singer@example.com"):
        assert private_value not in raw_json


def test_dry_run_writes_nothing_and_never_downloads(tmp_path, capsys):
    def unexpected_download(bucket, path):
        raise AssertionError("dry-run attempted download")

    out = tmp_path / "missing"
    counts = collector.collect(
        [_job()], days=30, max_jobs=200, out_dir=out, bucket="bucket",
        dry_run=True, now=NOW, download=unexpected_download,
    )
    assert not out.exists()
    assert counts == {"scanned": 1, "kept": 1, "deduped": 0, "failed": 0}
    assert "job-a" in capsys.readouterr().out


def test_index_has_fixed_header_and_only_successful_downloads(tmp_path):
    jobs = [_job("good"), _job("missing")]

    def download(bucket, path):
        assert bucket == "bucket"
        if path.endswith("missing.xml"):
            raise FileNotFoundError(path)
        return b"<score/>"

    counts = collector.collect(
        jobs, days=30, max_jobs=200, out_dir=tmp_path, bucket="bucket",
        dry_run=False, now=NOW, download=download,
    )
    with (tmp_path / "index.csv").open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == list(collector.INDEX_COLUMNS)
        rows = list(reader)
    assert [row["job_id"] for row in rows] == ["good"]
    assert rows[0]["duration_s"] == "12.0"
    assert counts == {"scanned": 2, "kept": 1, "deduped": 0, "failed": 1}
    assert not (tmp_path / "missing").exists()


def test_out_is_required():
    with pytest.raises(SystemExit) as error:
        collector._parse_args(["--target", "production"])
    assert error.value.code == 2
