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


# Two-run gate (Alan, 2026-10-23).

GOOD = _job()
FAILED = _job(status="failed", quality_class=None)


def _gate(v1, candidate, tiebreak=None):
    return compare.gate_job("job-x", list(v1), list(candidate), tiebreak)


@pytest.mark.parametrize(
    ("v1", "candidate", "result"),
    [
        ((GOOD, GOOD), (GOOD, GOOD), "pass"),
        ((GOOD, GOOD), (FAILED, FAILED), "block"),
        ((GOOD, GOOD), (GOOD, FAILED), "needs_tiebreak"),
        ((GOOD, FAILED), (FAILED, FAILED), "review"),   # 1/2 -> 0/2: review, not block
        ((FAILED, FAILED), (FAILED, FAILED), "pass"),
        ((GOOD, FAILED), (GOOD, GOOD), "pass"),
    ],
)
def test_gate_by_good_run_counts(v1, candidate, result):
    row, _ = _gate(v1, candidate)
    assert row["result"] == result


def test_gate_tiebreak_good_passes_for_review_and_bad_blocks():
    row, review = _gate((GOOD, GOOD), (GOOD, FAILED), tiebreak=GOOD)
    assert row["result"] == "review"
    assert row["tiebreak_status"] == "good"
    assert any("tie-break" in item["reason"] for item in review)
    row, _ = _gate((GOOD, GOOD), (GOOD, FAILED), tiebreak=FAILED)
    assert row["result"] == "block"
    assert "1/3" in row["reasons"]


def test_gate_new_p0_in_either_candidate_run_blocks_but_known_p0_does_not():
    p0 = _finding("same_clef_claim_coverage", "P0")
    new_p0, _ = _gate((GOOD, GOOD), (GOOD, _job(findings=[p0])))
    assert new_p0["result"] == "block"
    assert "new P0 same_clef_claim_coverage" in new_p0["reasons"]
    known, _ = _gate((_job(findings=[p0]), GOOD), (_job(findings=[p0]), _job(findings=[p0])))
    assert "new P0" not in known["reasons"]


def test_gate_same_new_p1_in_both_runs_blocks_one_run_only_is_review():
    p1 = _finding("weak_lyric_source_with_better_alternative", "P1")
    both, _ = _gate((FAILED, FAILED), (_job(findings=[p1]), _job(findings=[p1])))
    assert both["result"] == "block"
    assert "same new P1" in both["reasons"]
    one, review = _gate((FAILED, FAILED), (_job(findings=[p1]), FAILED))
    assert one["result"] == "review"
    assert [item["reason"] for item in review] == ["new P1 in one candidate run only"]
    known, _ = _gate((_job(findings=[p1]), FAILED), (_job(findings=[p1]), _job(findings=[p1])))
    assert known["result"] != "block"


def test_gate_quality_class_below_v1_minimum_blocks():
    lower, _ = _gate((GOOD, _job(quality_class=2)), (GOOD, _job(quality_class=1)))
    assert lower["result"] == "block"
    assert "quality_class 1 < v1 minimum 2" in lower["reasons"]
    equal, _ = _gate((GOOD, _job(quality_class=2)), (GOOD, _job(quality_class=2)))
    assert "quality_class" not in equal["reasons"]


def test_gate_new_p2_is_review_only():
    row, review = _gate((GOOD, GOOD), (GOOD, _job(findings=[_finding("partial_lyric_coverage", "P2")])))
    assert row["result"] == "review"
    assert review[0]["reason"] == "new P2"


def _write_run(root, name, jobs, label=None):
    run = root / name
    (run / "jobs").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "label": label or name, "planner": "v1", "corpus_dir": "/corpus",
    }))
    for job_id, job in jobs.items():
        (run / "jobs" / f"{job_id}.json").write_text(json.dumps({**job, "job_id": job_id}))
    return run


def _gate_runs(tmp_path, v1a, v1b, ca, cb, tiebreak=None):
    runs = [
        _write_run(tmp_path, name, jobs)
        for name, jobs in (("v1a", v1a), ("v1b", v1b), ("c1", ca), ("c2", cb))
    ]
    if tiebreak is not None:
        _write_run(tmp_path, "c1-tiebreak", tiebreak)
    summary = compare.evaluate_gate(runs[:2], runs[2:], tmp_path / "report")
    return summary.read_text(encoding="utf-8")


def test_evaluate_gate_lists_tiebreak_jobs_with_runner_command(tmp_path):
    # Job c improves (1/2 -> 2/2), so the set-level good-run totals tie.
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": GOOD, "c": FAILED},
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": FAILED, "c": GOOD},
    )
    assert "Result: needs tie-breaker" in summary
    assert "- b" in summary
    assert "run_preprocess_replay.py --corpus /corpus" in summary
    assert "--label c1-tiebreak --planner v1 --job b'" in summary


def test_evaluate_gate_tiebreak_present_passes_with_review(tmp_path):
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": GOOD, "c": FAILED},
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": FAILED, "c": GOOD},
        tiebreak={"b": GOOD},
    )
    assert "Result: pass" in summary
    assert "passed only on the tie-break run" in summary


def test_evaluate_gate_tiebreak_job_alone_passes_at_set_level(tmp_path):
    # A 2/2 -> 1/2 job with a good tie-break run leaves the candidate 1 good run
    # below v1, which is within v1's own run-to-run variation. v1 has noise 1
    # (job c good in its first run only), so one 2/2 -> 1/2 job is not more.
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": GOOD, "c": FAILED},
        {"a": GOOD, "b": GOOD, "c": GOOD}, {"a": GOOD, "b": FAILED, "c": FAILED},
        tiebreak={"b": GOOD},
    )
    assert "Result: pass" in summary
    assert "Good runs: v1 5/6, candidate 4/6" in summary
    assert "Jobs 2/2 -> 1/2: candidate 1, v1 noise 1" in summary


def test_evaluate_gate_set_level_exactly_one_good_run_below_v1_passes(tmp_path):
    # Job a goes 1/2 -> 0/2: only a review item, and one run below v1 is within the margin.
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD}, {"a": FAILED, "b": GOOD},
        {"a": FAILED, "b": GOOD}, {"a": FAILED, "b": GOOD},
    )
    assert "Result: pass" in summary
    assert "set level:" not in summary


def test_evaluate_gate_set_level_two_good_runs_below_v1_blocks(tmp_path):
    # Each job alone is only "review" (1/2 -> 0/2), but together they are 2 runs below v1.
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD}, {"a": FAILED, "b": FAILED},
        {"a": FAILED, "b": FAILED}, {"a": FAILED, "b": FAILED},
    )
    assert "Result: blocked: investigate" in summary
    assert "set level: candidate good runs 0/4 more than 1 below v1 good runs 2/4" in summary


def test_evaluate_gate_set_level_blocks_on_more_drops_than_v1_noise(tmp_path):
    # v1 noise: no job good in v1a and not in v1b. Candidate: two jobs 2/2 -> 1/2,
    # balanced by two jobs 0/2 -> 1/2 so the good-run totals tie.
    v1 = {"a": GOOD, "b": GOOD, "c": FAILED, "d": FAILED}
    summary = _gate_runs(
        tmp_path, v1, dict(v1),
        {"a": GOOD, "b": GOOD, "c": GOOD, "d": GOOD},
        {"a": FAILED, "b": FAILED, "c": FAILED, "d": FAILED},
        tiebreak={"a": GOOD, "b": GOOD},
    )
    assert "Result: blocked: investigate" in summary
    assert "jobs going 2/2 -> 1/2: 2 > v1 noise" in summary


def test_evaluate_gate_reports_missing_results_not_pass(tmp_path):
    summary = _gate_runs(
        tmp_path,
        {"a": GOOD, "b": GOOD}, {"a": GOOD, "b": GOOD},
        {"a": GOOD, "b": GOOD}, {"a": GOOD},
    )
    assert "Result: incomplete: missing results" in summary
    assert "- b: c2 missing" in summary


def test_main_requires_one_or_two_of_each_run(tmp_path):
    with pytest.raises(SystemExit):
        compare._parse_args(["--baseline", "a", "--candidate", "b", "--candidate", "c", "--out", "o"])
