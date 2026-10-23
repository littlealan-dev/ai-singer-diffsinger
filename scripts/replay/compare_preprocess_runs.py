#!/usr/bin/env python3
"""Compare two offline preprocess replay runs and report the ship gate."""

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    print(compare_runs(args.baseline, args.candidate, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
