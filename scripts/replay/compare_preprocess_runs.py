#!/usr/bin/env python3
"""Compare offline preprocess replay runs and report the ship gate.

One --baseline and one --candidate: the per-job comparison (jobs.csv, new_p2.csv,
lane_diffs.csv, summary.md). Two of each: the two-run gate (gate_jobs.csv,
review.csv, gate_summary.md), with an optional --tiebreak run.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

JOBS_COLUMNS = (
    "job_id", "baseline_status", "candidate_status", "baseline_quality_class",
    "candidate_quality_class", "gate", "gate_fail_reasons", "new_p0", "new_p1",
    "new_p2", "lanes_with_diff", "baseline_wall_time_s", "candidate_wall_time_s",
    "baseline_total_tokens", "candidate_total_tokens",
)
NEW_P2_COLUMNS = ("job_id", "rule", "domain", "target", "measures")
LANE_COLUMNS = (
    "job_id", "lane_id", "lane_status", "notes_inserted", "notes_deleted",
    "notes_replaced", "lyrics_changed",
)
TOKEN_FIELDS = ("prompt_tokens", "cached_tokens", "thinking_tokens", "output_tokens")


def _read_job(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(job, dict):
            raise ValueError("job must be an object")
        for key in ("findings", "llm_calls", "lanes"):
            value = job.get(key)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
        for key in ("findings", "llm_calls"):
            if any(not isinstance(item, dict) for item in job.get(key) or []):
                raise ValueError(f"{key} items must be objects")
        for lane in job.get("lanes") or []:
            if not isinstance(lane, dict) or not isinstance(lane.get("notes", []), list):
                raise ValueError("lane must contain a notes list")
            if any(not isinstance(note, dict) for note in lane.get("notes") or []):
                raise ValueError("notes must be objects")
            for note in lane.get("notes") or []:
                hash(_note_signature(note))
        for finding in job.get("findings") or []:
            hash(_finding_key(finding))
        if job.get("quality_class") is not None:
            float(job["quality_class"])
        _metrics(job)
        return job, None
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return None, "unreadable"


def _finding_key(finding: dict[str, Any]) -> tuple[Any, Any, Any]:
    return finding.get("rule"), finding.get("severity"), finding.get("target")


def new_findings(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Subtract findings as a multiset, retaining the candidate's metadata."""
    baseline_counts = Counter(_finding_key(finding) for finding in baseline)
    added = []
    for finding in candidate:
        key = _finding_key(finding)
        if baseline_counts[key]:
            baseline_counts[key] -= 1
        else:
            added.append(finding)
    return added


def _note_signature(note: dict[str, Any]) -> tuple[Any, float, Any, float]:
    return (
        note.get("measure"),
        round(float(note.get("offset_beats") or 0.0), 4),
        note.get("pitch_midi"),
        round(float(note.get("duration_beats") or 0.0), 4),
    )


def _lane_map(job: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        str(lane.get("lane_id")): lane.get("notes") or []
        for lane in job.get("lanes") or []
    }


def lane_diffs(
    job_id: str, baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    baseline_lanes = _lane_map(baseline)
    candidate_lanes = _lane_map(candidate)
    rows = []
    for lane_id in sorted(baseline_lanes.keys() | candidate_lanes.keys()):
        before = baseline_lanes.get(lane_id)
        after = candidate_lanes.get(lane_id)
        row = {
            "job_id": job_id,
            "lane_id": lane_id,
            "lane_status": "changed",
            "notes_inserted": 0,
            "notes_deleted": 0,
            "notes_replaced": 0,
            "lyrics_changed": 0,
        }
        if before is None:
            row["lane_status"] = "only_in_candidate"
            row["notes_inserted"] = len(after)
        elif after is None:
            row["lane_status"] = "only_in_baseline"
            row["notes_deleted"] = len(before)
        else:
            matcher = SequenceMatcher(
                a=[_note_signature(note) for note in before],
                b=[_note_signature(note) for note in after],
                autojunk=False,
            )
            for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
                if opcode == "insert":
                    row["notes_inserted"] += b1 - b0
                elif opcode == "delete":
                    row["notes_deleted"] += a1 - a0
                elif opcode == "replace":
                    row["notes_replaced"] += max(a1 - a0, b1 - b0)
                else:
                    row["lyrics_changed"] += sum(
                        (left.get("lyric") or "") != (right.get("lyric") or "")
                        for left, right in zip(before[a0:a1], after[b0:b1])
                    )
        if row["lane_status"] != "changed" or any(row[key] for key in LANE_COLUMNS[3:]):
            rows.append(row)
    return rows


def _metrics(job: dict[str, Any] | None) -> dict[str, int | float | None]:
    if job is None:
        return {"wall_time_s": None, "llm_calls": 0, **{key: 0 for key in TOKEN_FIELDS}, "total_tokens": None}
    calls = job.get("llm_calls") or []
    sums = {key: sum(int(call.get(key) or 0) for call in calls) for key in TOKEN_FIELDS}
    return {
        "wall_time_s": float(job["wall_time_s"]) if job.get("wall_time_s") is not None else None,
        "llm_calls": len(calls),
        **sums,
        "total_tokens": sums["prompt_tokens"] + sums["thinking_tokens"] + sums["output_tokens"],
    }


def compare_job(
    job_id: str, baseline: dict[str, Any] | None, candidate: dict[str, Any] | None,
    baseline_error: str | None = None, candidate_error: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    before_status = (
        f"{baseline_error}_in_baseline" if baseline_error == "missing"
        else baseline_error or (baseline.get("status") if baseline else None)
    )
    after_status = (
        f"{candidate_error}_in_candidate" if candidate_error == "missing"
        else candidate_error or (candidate.get("status") if candidate else None)
    )
    reasons = []
    new = []
    lane_rows = []
    if baseline_error:
        reasons.append(before_status)
    if candidate_error:
        reasons.append(after_status)
    if not baseline_error and not candidate_error and baseline is not None and candidate is not None:
        before_quality = baseline.get("quality_class")
        after_quality = candidate.get("quality_class")
        if before_quality is not None and after_quality is not None and after_quality < before_quality:
            reasons.append("quality_class_lower")
        if baseline.get("status") == "completed" and candidate.get("status") != "completed":
            reasons.append("candidate_not_completed")
        new = new_findings(baseline.get("findings") or [], candidate.get("findings") or [])
        if any(finding.get("severity") == "P0" for finding in new):
            reasons.append("new_P0")
        if any(finding.get("severity") == "P1" for finding in new):
            reasons.append("new_P1")
        lane_rows = lane_diffs(job_id, baseline, candidate)
    counts = Counter(finding.get("severity") for finding in new)
    before_metrics = _metrics(baseline)
    after_metrics = _metrics(candidate)
    row = {
        "job_id": job_id,
        "baseline_status": before_status,
        "candidate_status": after_status,
        "baseline_quality_class": baseline.get("quality_class") if baseline else None,
        "candidate_quality_class": candidate.get("quality_class") if candidate else None,
        "gate": "FAIL" if reasons else "PASS",
        "gate_fail_reasons": ";".join(reasons),
        "new_p0": counts["P0"],
        "new_p1": counts["P1"],
        "new_p2": counts["P2"],
        "lanes_with_diff": len(lane_rows),
        "baseline_wall_time_s": before_metrics["wall_time_s"],
        "candidate_wall_time_s": after_metrics["wall_time_s"],
        "baseline_total_tokens": before_metrics["total_tokens"],
        "candidate_total_tokens": after_metrics["total_tokens"],
    }
    p2_rows = [
        {
            "job_id": job_id,
            "rule": finding.get("rule"),
            "domain": finding.get("domain"),
            "target": finding.get("target"),
            "measures": json.dumps(finding.get("measures")) if finding.get("measures") is not None else None,
        }
        for finding in new if finding.get("severity") == "P2"
    ]
    return row, p2_rows, lane_rows


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _metric_summary(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [_metrics(job) for job in jobs]
    times = [metric["wall_time_s"] for metric in metrics if metric["wall_time_s"] is not None]
    tokens = [metric["total_tokens"] for metric in metrics if metric["total_tokens"] is not None]
    return {
        "median_time": statistics.median(times) if times else None,
        "p90_time": _percentile(times, 0.9),
        "median_tokens": statistics.median(tokens) if tokens else None,
        "p90_tokens": _percentile(tokens, 0.9),
        "llm_calls": sum(metric["llm_calls"] for metric in metrics),
        **{key: sum(metric[key] for metric in metrics) for key in TOKEN_FIELDS},
    }


def _display(value: Any) -> str:
    return "n/a" if value is None else f"{value:g}" if isinstance(value, (float, int)) else str(value)


def compare_runs(baseline_dir: Path, candidate_dir: Path, out_dir: Path) -> Path:
    baseline_dir = Path(baseline_dir)
    candidate_dir = Path(candidate_dir)
    out_dir = Path(out_dir)
    baseline_files = {path.stem: path for path in (baseline_dir / "jobs").glob("*.json")}
    candidate_files = {path.stem: path for path in (candidate_dir / "jobs").glob("*.json")}
    job_ids = sorted(baseline_files.keys() | candidate_files.keys())
    jobs_rows = []
    p2_rows = []
    lane_rows = []
    baseline_jobs = []
    candidate_jobs = []
    missing_baseline = missing_candidate = unreadable = compared = 0
    for job_id in job_ids:
        baseline, baseline_error = _read_job(baseline_dir / "jobs" / f"{job_id}.json")
        candidate, candidate_error = _read_job(candidate_dir / "jobs" / f"{job_id}.json")
        try:
            row, job_p2, job_lanes = compare_job(
                job_id, baseline, candidate, baseline_error, candidate_error
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            # A structurally bad job must not prevent reports for the other jobs.
            baseline_error = baseline_error or "unreadable"
            candidate_error = candidate_error or "unreadable"
            row, job_p2, job_lanes = compare_job(job_id, None, None, baseline_error, candidate_error)
            baseline = candidate = None
        jobs_rows.append(row)
        p2_rows.extend(job_p2)
        lane_rows.extend(job_lanes)
        missing_baseline += baseline_error == "missing"
        missing_candidate += candidate_error == "missing"
        unreadable += baseline_error == "unreadable" or candidate_error == "unreadable"
        compared += baseline is not None and candidate is not None
        if baseline is not None:
            baseline_jobs.append(baseline)
        if candidate is not None:
            candidate_jobs.append(candidate)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "jobs.csv", JOBS_COLUMNS, jobs_rows)
    _write_csv(out_dir / "new_p2.csv", NEW_P2_COLUMNS, p2_rows)
    _write_csv(out_dir / "lane_diffs.csv", LANE_COLUMNS, lane_rows)
    baseline_summary = _metric_summary(baseline_jobs)
    candidate_summary = _metric_summary(candidate_jobs)
    gate = "FAIL" if any(row["gate"] == "FAIL" for row in jobs_rows) else "PASS"
    summary = [
        "# Preprocess replay comparison",
        "",
        f"Jobs compared: {compared}",
        f"Missing in baseline: {missing_baseline}",
        f"Missing in candidate: {missing_candidate}",
        f"Unreadable job files: {unreadable}",
        f"Gate: {gate}",
        "",
        "## Failing jobs",
    ]
    summary.extend(
        f"- {row['job_id']}: {row['gate_fail_reasons']}"
        for row in jobs_rows if row["gate"] == "FAIL"
    )
    if gate == "PASS":
        summary.append("- None")
    summary.extend(["", f"New P2 findings: {len(p2_rows)}", "", "## Run metrics", ""])
    for name, metrics in (("Baseline", baseline_summary), ("Candidate", candidate_summary)):
        summary.extend([
            f"- {name} median wall_time_s: {_display(metrics['median_time'])}",
            f"- {name} p90 wall_time_s: {_display(metrics['p90_time'])}",
            f"- {name} median total tokens: {_display(metrics['median_tokens'])}",
            f"- {name} p90 total tokens: {_display(metrics['p90_tokens'])}",
            f"- {name} llm_calls: {metrics['llm_calls']}",
            *(f"- {name} {key}: {metrics[key]}" for key in TOKEN_FIELDS),
        ])
    summary.append(f"- Jobs with any lane difference: {len({row['job_id'] for row in lane_rows})}")
    summary_path = out_dir / "summary.md"
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")
    return summary_path


# Two-run ship gate (Alan, 2026-10-23). A run of a job is "good" when it
# completed with no P0 or P1 finding. v1 and the candidate each run every job
# twice; a block means "stop and investigate", not a verdict on the candidate.
GATE_JOB_COLUMNS = (
    "job_id", "v1_good", "candidate_good", "result", "reasons",
    "v1_statuses", "candidate_statuses", "tiebreak_status",
    "v1_quality_classes", "candidate_quality_classes",
)
REVIEW_COLUMNS = ("job_id", "reason", "rule", "severity", "target", "runs")
RUNNER_PYTHON = "/Users/alanchan/antigravity/ai-singer-diffsinger/.venv310/bin/python"
REPLAY_ENV = "/Users/alanchan/antigravity/sightsinger-agents/replay.env"


def _is_good(job: dict[str, Any]) -> bool:
    return job.get("status") == "completed" and not any(
        finding.get("severity") in {"P0", "P1"} for finding in job.get("findings") or []
    )


def _keys(job: dict[str, Any], severity: str) -> set[tuple[Any, Any, Any]]:
    return {
        _finding_key(finding)
        for finding in job.get("findings") or []
        if finding.get("severity") == severity
    }


def gate_job(
    job_id: str,
    v1_runs: list[dict[str, Any]],
    candidate_runs: list[dict[str, Any]],
    tiebreak: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply the per-job gate to two v1 runs and two candidate runs of one job.

    result is "block", "needs_tiebreak", "review" or "pass"; review rows list
    what a person should look at even when nothing blocks.
    """
    v1_good = sum(_is_good(run) for run in v1_runs)
    candidate_good = sum(_is_good(run) for run in candidate_runs)
    reasons: list[str] = []
    review: list[dict[str, Any]] = []

    def review_row(reason: str, finding_key=(None, None, None), runs: str = "") -> dict[str, Any]:
        rule, severity, target = finding_key
        return {"job_id": job_id, "reason": reason, "rule": rule, "severity": severity,
                "target": target, "runs": runs}

    if v1_good == 2 and candidate_good == 0:
        reasons.append("good in 2/2 v1 runs but 0/2 candidate runs")
    v1_p0 = set().union(*(_keys(run, "P0") for run in v1_runs))
    for key in sorted(set().union(*(_keys(run, "P0") for run in candidate_runs)) - v1_p0, key=str):
        reasons.append(f"new P0 {key[0]} on {key[2]}")
    v1_p1 = set().union(*(_keys(run, "P1") for run in v1_runs))
    candidate_p1 = [_keys(run, "P1") - v1_p1 for run in candidate_runs]
    for key in sorted(candidate_p1[0] & candidate_p1[1], key=str):
        reasons.append(f"same new P1 {key[0]} on {key[2]} in both candidate runs")
    for key in sorted(candidate_p1[0] ^ candidate_p1[1], key=str):
        review.append(review_row("new P1 in one candidate run only", key,
                                 "1" if key in candidate_p1[0] else "2"))
    v1_classes = [
        run.get("quality_class") for run in v1_runs
        if run.get("status") == "completed" and run.get("quality_class") is not None
    ]
    if v1_classes:
        floor = min(v1_classes)
        for index, run in enumerate(candidate_runs, start=1):
            quality = run.get("quality_class")
            if run.get("status") == "completed" and quality is not None and quality < floor:
                reasons.append(f"candidate run {index} quality_class {quality} < v1 minimum {floor}")
    v1_p2 = set().union(*(_keys(run, "P2") for run in v1_runs))
    for index, run in enumerate(candidate_runs, start=1):
        for key in sorted(_keys(run, "P2") - v1_p2, key=str):
            review.append(review_row("new P2", key, str(index)))

    tiebreak_status = ""
    if reasons:
        result = "block"
    elif v1_good == 2 and candidate_good == 1:
        if tiebreak is None:
            result = "needs_tiebreak"
            tiebreak_status = "missing"
        elif _is_good(tiebreak):
            result = "review"
            tiebreak_status = "good"
            review.append(review_row("passed only on the tie-break run (2/3 good)"))
        else:
            result = "block"
            tiebreak_status = "not good"
            reasons.append("good in 2/2 v1 runs but 1/3 candidate runs (tie-break run not good)")
    else:
        if candidate_good < v1_good:
            review.append(review_row(f"good in {v1_good}/2 v1 runs but {candidate_good}/2 candidate runs"))
        result = "review" if review else "pass"
    row = {
        "job_id": job_id,
        "v1_good": v1_good,
        "candidate_good": candidate_good,
        "result": result,
        "reasons": "; ".join(reasons),
        "v1_statuses": "/".join(str(run.get("status")) for run in v1_runs),
        "candidate_statuses": "/".join(str(run.get("status")) for run in candidate_runs),
        "tiebreak_status": tiebreak_status,
        "v1_quality_classes": "/".join(str(run.get("quality_class")) for run in v1_runs),
        "candidate_quality_classes": "/".join(str(run.get("quality_class")) for run in candidate_runs),
    }
    return row, review


def _run_meta(run_dir: Path) -> dict[str, Any]:
    try:
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def tiebreak_command(candidate_dir: Path, tiebreak_dir: Path, job_ids: list[str]) -> str:
    """The runner command that replays only the tie-break jobs into tiebreak_dir."""
    meta = _run_meta(candidate_dir)
    label = f"{meta.get('label') or candidate_dir.name}-tiebreak"
    jobs = " ".join(f"--job {job_id}" for job_id in job_ids)
    return (
        "bash -c 'set -a; . " + REPLAY_ENV + "; set +a; "
        f"{RUNNER_PYTHON} scripts/replay/run_preprocess_replay.py "
        f"--corpus {meta.get('corpus_dir') or '<corpus>'} --out {tiebreak_dir} "
        f"--label {label} --planner {meta.get('planner') or 'v1'} {jobs}'"
    )


def evaluate_gate(
    baseline_dirs: list[Path],
    candidate_dirs: list[Path],
    out_dir: Path,
    tiebreak_dir: Path | None = None,
) -> Path:
    """Report the two-run gate for two v1 runs against two candidate runs."""
    baseline_dirs = [Path(path) for path in baseline_dirs]
    candidate_dirs = [Path(path) for path in candidate_dirs]
    out_dir = Path(out_dir)
    tiebreak_dir = Path(tiebreak_dir) if tiebreak_dir else candidate_dirs[0].parent / (
        candidate_dirs[0].name + "-tiebreak"
    )
    all_dirs = baseline_dirs + candidate_dirs
    job_ids = sorted(set().union(*(
        {path.stem for path in (run_dir / "jobs").glob("*.json")} for run_dir in all_dirs
    )))
    rows: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    incomplete: list[str] = []
    v1_good_total = candidate_good_total = 0
    candidate_drops = noise_drops = 0
    for job_id in job_ids:
        runs = [_read_job(run_dir / "jobs" / f"{job_id}.json") for run_dir in all_dirs]
        if any(error for _, error in runs):
            incomplete.append(
                f"{job_id}: " + ", ".join(
                    f"{run_dir.name} {error}" for run_dir, (_, error) in zip(all_dirs, runs) if error
                )
            )
            continue
        v1_runs = [job for job, _ in runs[:2]]
        candidate_runs = [job for job, _ in runs[2:]]
        tiebreak, tiebreak_error = _read_job(tiebreak_dir / "jobs" / f"{job_id}.json")
        row, job_review = gate_job(
            job_id, v1_runs, candidate_runs, tiebreak if tiebreak_error is None else None
        )
        rows.append(row)
        review.extend(job_review)
        v1_good_total += row["v1_good"]
        candidate_good_total += row["candidate_good"]
        candidate_drops += row["v1_good"] == 2 and row["candidate_good"] == 1
        noise_drops += _is_good(v1_runs[0]) and not _is_good(v1_runs[1])

    set_reasons = []
    if candidate_good_total < v1_good_total:
        set_reasons.append(
            f"candidate good runs {candidate_good_total}/{2 * len(rows)} < v1 good runs "
            f"{v1_good_total}/{2 * len(rows)}"
        )
    if candidate_drops > noise_drops:
        set_reasons.append(
            f"jobs going 2/2 -> 1/2: {candidate_drops} > v1 noise ({baseline_dirs[0].name} good, "
            f"{baseline_dirs[1].name} not): {noise_drops}"
        )
    blocked = [row for row in rows if row["result"] == "block"]
    pending = [row["job_id"] for row in rows if row["result"] == "needs_tiebreak"]
    if blocked or set_reasons:
        verdict = "blocked: investigate"
    elif pending:
        verdict = "needs tie-breaker"
    elif incomplete:
        verdict = "incomplete: missing results"
    else:
        verdict = "pass"

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "gate_jobs.csv", GATE_JOB_COLUMNS, rows)
    _write_csv(out_dir / "review.csv", REVIEW_COLUMNS, review)
    lines = [
        "# Preprocess two-run gate",
        "",
        f"Result: {verdict}",
        "A block is a stop to investigate the listed jobs, not a verdict on the candidate.",
        "",
        "v1 runs: " + ", ".join(path.name for path in baseline_dirs),
        "Candidate runs: " + ", ".join(path.name for path in candidate_dirs),
        f"Tie-break run: {tiebreak_dir.name}",
        f"Jobs compared: {len(rows)}",
        "",
        "## Jobs to investigate",
    ]
    lines.extend(f"- {row['job_id']}: {row['reasons']}" for row in blocked)
    lines.extend(f"- set level: {reason}" for reason in set_reasons)
    if not blocked and not set_reasons:
        lines.append("- None")
    lines.extend(["", "## Needs a tie-breaker run"])
    if pending:
        lines.extend(f"- {job_id}" for job_id in pending)
        lines.extend(["", "Run from the planner-v2 worktree:", "",
                      "    " + tiebreak_command(candidate_dirs[0], tiebreak_dir, pending)])
    else:
        lines.append("- None")
    lines.extend(["", "## Review (not blocking)"])
    lines.extend(
        f"- {item['job_id']}: {item['reason']}"
        + (f" {item['rule']} on {item['target']} (run {item['runs']})" if item["rule"] else "")
        for item in review
    )
    if not review:
        lines.append("- None")
    lines.extend(["", "## Missing results"])
    lines.extend(f"- {item}" for item in incomplete)
    if not incomplete:
        lines.append("- None")
    lines.extend([
        "", "## Set level", "",
        f"- Good runs: v1 {v1_good_total}/{2 * len(rows)}, candidate {candidate_good_total}/{2 * len(rows)}",
        f"- Jobs 2/2 -> 1/2: candidate {candidate_drops}, v1 noise {noise_drops}",
        "", "## Run metrics", "",
    ])
    for run_dir in all_dirs:
        jobs = [job for job, error in (
            _read_job(path) for path in sorted((run_dir / "jobs").glob("*.json"))
        ) if error is None]
        metrics = _metric_summary(jobs)
        lines.append(
            f"- {run_dir.name}: median wall_time_s {_display(metrics['median_time'])}, "
            f"p90 {_display(metrics['p90_time'])}, median total tokens "
            f"{_display(metrics['median_tokens'])}, p90 {_display(metrics['p90_tokens'])}"
        )
    summary_path = out_dir / "gate_summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, action="append", required=True,
                        help="A v1 run directory; give two for the two-run gate.")
    parser.add_argument("--candidate", type=Path, action="append", required=True,
                        help="A candidate run directory; give two for the two-run gate.")
    parser.add_argument("--tiebreak", type=Path,
                        help="Third candidate run for tie-break jobs (default: <first candidate>-tiebreak).")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if (len(args.baseline), len(args.candidate)) not in {(1, 1), (2, 2)}:
        parser.error("give one --baseline and one --candidate, or two of each")
    if args.tiebreak and len(args.candidate) != 2:
        parser.error("--tiebreak needs two --candidate runs")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if len(args.baseline) == 2:
        print(evaluate_gate(args.baseline, args.candidate, args.out, args.tiebreak))
    else:
        print(compare_runs(args.baseline[0], args.candidate[0], args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
