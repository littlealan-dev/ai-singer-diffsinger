from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay" / "compare_preprocess_runs.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
_spec = importlib.util.spec_from_file_location("compare_preprocess_runs", _SCRIPT)
compare = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = compare
_spec.loader.exec_module(compare)


def _job(**overrides):
    job = {
        "job_id": "job-x",
        "status": "completed",
        "wall_time_s": 10.0,
        "quality_class": 3,
        "findings": [],
        "llm_calls": [],
        "lanes": [],
    }
    job.update(overrides)
    return job


def _finding(rule, severity, target="alto", domain="notes"):
    return {"rule": rule, "severity": severity, "target": target, "domain": domain, "measures": [1, 2]}


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ({"quality_class": 2}, "quality_class_lower"),
        ({"status": "failed"}, "candidate_not_completed"),
        ({"findings": [_finding("range", "P0")]}, "new_P0"),
        ({"findings": [_finding("rhythm", "P1")]}, "new_P1"),
    ],
)
def test_each_ship_gate_reason(candidate, reason):
    row, _, _ = compare.compare_job("job-x", _job(), _job(**candidate))
    assert row["gate"] == "FAIL"
    assert row["gate_fail_reasons"] == reason


def test_new_p2_is_reported_without_failing_gate():
    row, p2, _ = compare.compare_job(
        "job-x", _job(), _job(findings=[_finding("diction", "P2", domain="lyrics")])
    )
    assert row["gate"] == "PASS"
    assert row["new_p2"] == 1
    assert p2 == [{
        "job_id": "job-x", "rule": "diction", "domain": "lyrics",
        "target": "alto", "measures": "[1, 2]",
    }]


def test_finding_subtraction_uses_multiset_counts():
    finding = _finding("range", "P1")
    same_row, _, _ = compare.compare_job(
        "job-x", _job(findings=[finding]), _job(findings=[finding])
    )
    extra_row, _, _ = compare.compare_job(
        "job-x", _job(findings=[finding]), _job(findings=[finding, finding])
    )
    assert same_row["gate"] == "PASS"
    assert same_row["new_p1"] == 0
    assert extra_row["gate_fail_reasons"] == "new_P1"
    assert extra_row["new_p1"] == 1


def _note(pitch, lyric=None, offset=0.0):
    return {
        "measure": 1, "offset_beats": offset, "pitch_midi": pitch,
        "duration_beats": 1.0, "lyric": lyric,
    }


def test_lane_diff_counts_insert_delete_replace_and_lyrics():
    baseline = _job(lanes=[
        {"lane_id": "insert", "notes": [_note(60)]},
        {"lane_id": "delete", "notes": [_note(60), _note(62, offset=1)]},
        {"lane_id": "replace", "notes": [_note(60)]},
        {"lane_id": "lyrics", "notes": [_note(60, None), _note(62, "la", offset=1)]},
    ])
    candidate = _job(lanes=[
        {"lane_id": "insert", "notes": [_note(60), _note(62, offset=1)]},
        {"lane_id": "delete", "notes": [_note(60)]},
        {"lane_id": "replace", "notes": [_note(64)]},
        {"lane_id": "lyrics", "notes": [_note(60, ""), _note(62, "new", offset=1)]},
    ])
    rows = {row["lane_id"]: row for row in compare.lane_diffs("job-x", baseline, candidate)}
    assert rows["insert"]["notes_inserted"] == 1
    assert rows["delete"]["notes_deleted"] == 1
    assert rows["replace"]["notes_replaced"] == 1
    assert rows["lyrics"]["lyrics_changed"] == 1
    assert len(rows) == 4


def test_only_in_one_run_lanes_are_reported():
    rows = {row["lane_id"]: row for row in compare.lane_diffs(
        "job-x",
        _job(lanes=[{"lane_id": "old", "notes": [_note(60)]}]),
        _job(lanes=[{"lane_id": "new", "notes": [_note(72)]}]),
    )}
    assert rows["old"]["lane_status"] == "only_in_baseline"
    assert rows["old"]["notes_deleted"] == 1
    assert rows["new"]["lane_status"] == "only_in_candidate"
    assert rows["new"]["notes_inserted"] == 1


def test_missing_and_unreadable_job_files_are_reported(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    (baseline / "jobs").mkdir(parents=True)
    (candidate / "jobs").mkdir(parents=True)
    (baseline / "jobs" / "missing-candidate.json").write_text(json.dumps(_job()))
    (candidate / "jobs" / "missing-baseline.json").write_text(json.dumps(_job()))
    (baseline / "jobs" / "bad.json").write_text("not json")
    (candidate / "jobs" / "bad.json").write_text(json.dumps(_job()))
    malformed = _job(lanes=[{"lane_id": "x", "notes": [_note([], offset=0)]}])
    (baseline / "jobs" / "bad-shape.json").write_text(json.dumps(malformed))
    (candidate / "jobs" / "bad-shape.json").write_text(json.dumps(_job()))
    summary_path = compare.compare_runs(baseline, candidate, tmp_path / "report")
    with (tmp_path / "report" / "jobs.csv").open(newline="", encoding="utf-8") as file:
        rows = {row["job_id"]: row for row in csv.DictReader(file)}
    assert rows["missing-candidate"]["candidate_status"] == "missing_in_candidate"
    assert rows["missing-baseline"]["baseline_status"] == "missing_in_baseline"
    assert rows["bad"]["baseline_status"] == "unreadable"
    assert rows["bad"]["candidate_status"] == "completed"
    assert rows["bad-shape"]["baseline_status"] == "unreadable"
    assert rows["bad-shape"]["candidate_status"] == "completed"
    assert all(row["gate"] == "FAIL" for row in rows.values())
    assert "Unreadable job files: 2" in summary_path.read_text()


def test_fixture_report_has_gate_failures_p2_and_metric_medians(tmp_path, capsys):
    baseline = _FIXTURES / "baseline"
    candidate = _FIXTURES / "candidate"
    out = tmp_path / "report"
    assert compare.main([
        "--baseline", str(baseline), "--candidate", str(candidate), "--out", str(out)
    ]) == 0
    assert str(out / "summary.md") in capsys.readouterr().out
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Gate: FAIL" in summary
    assert "job-a: quality_class_lower;candidate_not_completed;new_P0;new_P1" in summary
    assert "New P2 findings: 1" in summary
    assert "Baseline median wall_time_s: 20" in summary
    assert "Jobs with any lane difference: 1" in summary
    with (out / "jobs.csv").open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == list(compare.JOBS_COLUMNS)
        rows = {row["job_id"]: row for row in reader}
    assert rows["job-b"]["gate"] == "PASS"
    assert rows["job-a"]["baseline_total_tokens"] == "130"
    with (out / "new_p2.csv").open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == list(compare.NEW_P2_COLUMNS)
        assert [row["job_id"] for row in reader] == ["job-b"]
    with (out / "lane_diffs.csv").open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == list(compare.LANE_COLUMNS)
        assert [row["job_id"] for row in reader] == ["job-a"]
