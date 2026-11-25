"""Read-only aggregate report for stored Sing All jobs (dates are UTC)."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

_OUTCOMES = ("completed", "partial", "failed", "cancelled")
_ENDED_ITEMS = {"done", "failed", "stopped", "skipped"}
_END_REASONS = {
    "decision_timeout", "decision_cancelled", "stopped", "stuck",
    "score_replaced", "quote_changed", "split_failed", "solfege_failed",
    "nothing_to_sing", "instrumental_failed", "instrumentals_failed",
}
_PAUSE_REASONS = {"split_failed", "split_review", "parts_changed", "solfege_failed"}
_PACKAGE_ERRORS = {
    "no_takes", "missing_wav", "missing_midi", "render_failed",
    "mix_failed", "upload_failed", "package_failed",
}
_EXCEPTION_TYPES = {
    "RuntimeError", "ValueError", "FileNotFoundError", "OSError", "PermissionError",
    "TypeError", "KeyError", "TimeoutError", "CalledProcessError", "GoogleAPIError",
    "NotFound", "Forbidden", "ServiceUnavailable", "DeadlineExceeded", "RetryError",
}
Job = Mapping[str, Any]


def _instant(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _count_range(count: int) -> str:
    for limit, label in ((0, "0"), (1, "1"), (4, "2-4"), (8, "5-8"), (16, "9-16")):
        if count <= limit:
            return label
    return "17+"


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _category(value: object, allowed: set[str]) -> str:
    if value is None:
        return "unspecified"
    return value if isinstance(value, str) and value in allowed else "other"


def _package_error(value: object) -> str:
    if not isinstance(value, str):
        return "unspecified"
    code, _, exception = value.partition(":")
    if code not in _PACKAGE_ERRORS:
        return "other"
    return value if exception in _EXCEPTION_TYPES else code


def summarize(jobs: Iterable[Job], start: datetime, end: datetime) -> dict[str, Any]:
    """Summarize jobs created in the half-open UTC interval [start, end)."""
    start, end = _instant(start), _instant(end)
    if start is None or end is None or start >= end:
        raise ValueError("Start must be before end.")

    started = retries = paused = planned = made = 0
    reserved = charged = refunded = zip_ready = zip_downloaded = 0
    outcomes = dict.fromkeys(_OUTCOMES, 0)
    end_reasons: Counter[str] = Counter()
    pause_reasons: Counter[str] = Counter()
    pause_answers: Counter[str] = Counter()
    package_failures: Counter[str] = Counter()
    choices: dict[str, Counter[str]] = {
        key: Counter() for key in ("mode", "mixes", "repeats")
    }
    durations: dict[str, list[float]] = defaultdict(list)

    for job in jobs:
        created = _instant(job.get("createdAt"))
        if created is None or not start <= created < end:
            continue
        started += 1
        retries += bool(job.get("retryOf"))
        status = job.get("status")
        reason = job.get("endReason")
        if status in outcomes:
            outcomes[status] += 1
            end_reasons[_category(reason, _END_REASONS)] += 1

        decision = job.get("decision")
        if decision:
            paused += 1
            reasons = {_category(part.get("reason"), _PAUSE_REASONS)
                       for part in decision.get("parts") or []}
            pause_reasons.update(reasons or {"unspecified"})
            answer = (decision.get("answer") or {}).get("action")
            if answer in {"continue", "cancel"}:
                pause_answers[answer] += 1
            elif reason == "decision_timeout":
                pause_answers["timed_out"] += 1

        take_count = 0
        for item in job.get("items") or []:
            item_status = item.get("status")
            if item.get("type") == "take" and not item.get("carried"):
                take_count += 1
                made += item_status == "done"
            item_reserved = int(item.get("creditsReserved") or 0)
            item_charged = int(item.get("creditsCharged") or 0)
            reserved += item_reserved
            charged += item_charged
            if item_status in _ENDED_ITEMS:
                refunded += max(0, item_reserved - item_charged)
        planned += take_count

        package = job.get("package") or {}
        if package.get("state") == "failed":
            package_failures[_package_error(package.get("error"))] += 1
        if status == "completed":
            ended = _instant(job.get("endedAt"))
            if ended is not None and ended >= created:
                durations[_count_range(take_count)].append((ended - created).total_seconds())
            if package.get("state") == "done":
                zip_ready += 1
                zip_downloaded += int(job.get("zipDownloads") or 0) >= 1

        options = job.get("choices") or {}
        choices["mode"][_category(options.get("mode"), {"lyrics", "solfege", "both"})] += 1
        choices["mixes"][_category(options.get("mixes") or "none", {"all", "full", "none"})] += 1
        repeats = options.get("repeats")
        choices["repeats"]["true" if repeats is True else "false" if repeats is False
                           else "unspecified"] += 1

    finish_times = {}
    for label, samples in durations.items():
        samples.sort()
        finish_times[label] = {
            "jobs": len(samples),
            "median_seconds": median(samples),
            "p90_seconds": samples[math.ceil(len(samples) * 0.9) - 1],
        }

    return {
        "jobs_started": started,
        "retry_jobs": retries,
        "retry_share": _rate(retries, started),
        "outcomes": outcomes,
        "end_reasons": dict(end_reasons),
        "paused_jobs": paused,
        "pause_rate": _rate(paused, started),
        "pause_reasons": dict(pause_reasons),
        "pause_answers": dict(pause_answers),
        "takes": {"done": made, "planned": planned, "share": _rate(made, planned)},
        "credits": {"reserved": reserved, "charged": charged, "refunded": refunded,
                    "refund_share": _rate(refunded, reserved)},
        "time_to_finish_by_takes": finish_times,
        "choices": {key: dict(counts) for key, counts in choices.items()},
        "zip_downloads": {"completed_with_download": zip_downloaded,
                          "completed_with_zip": zip_ready, "share": _rate(zip_downloaded, zip_ready)},
        "packaging_failures": dict(package_failures),
    }


def _periods(start: datetime, end: datetime, by: str) -> Iterable[tuple[datetime, datetime]]:
    while start < end:
        days = 1 if by == "day" else 7 - start.weekday()
        next_start = min(end, start + timedelta(days=days))
        yield start, next_start
        start = next_start


def _read_jobs(start: datetime, end: datetime) -> list[Job]:
    # Allow direct invocation from any directory.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from google.cloud.firestore_v1.base_query import FieldFilter
    from src.backend.firebase_app import get_firestore_client, initialize_firebase_app

    initialize_firebase_app()
    query = (get_firestore_client().collection("singAllJobs")
             .where(filter=FieldFilter("createdAt", ">=", start))
             .where(filter=FieldFilter("createdAt", "<", end)))
    return [doc.to_dict() for doc in query.stream()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, help="Number of UTC dates through today (default 7).")
    parser.add_argument("--start", type=date.fromisoformat, help="First UTC date, inclusive.")
    parser.add_argument("--end", type=date.fromisoformat, help="Last UTC date, inclusive.")
    parser.add_argument("--by", choices=("day", "week"), help="Also show totals by day or ISO week.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()
    if args.start is not None or args.end is not None:
        if args.days is not None or args.start is None or args.end is None or args.start > args.end:
            parser.error("Use --days or both --start and --end with start on or before end.")
        first, last = args.start, args.end
    else:
        days = args.days if args.days is not None else 7
        if days <= 0:
            parser.error("--days must be positive.")
        last = datetime.now(timezone.utc).date()
        first = last - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, timezone.utc)
    jobs = _read_jobs(start, end)
    report: dict[str, Any] = {
        "start": first.isoformat(), "end": last.isoformat(), "totals": summarize(jobs, start, end),
    }
    if args.by:
        report["periods"] = [
            {"start": period_start.date().isoformat(),
             "end": (period_end.date() - timedelta(days=1)).isoformat(),
             "totals": summarize(jobs, period_start, period_end)}
            for period_start, period_end in _periods(start, end, args.by)
        ]
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_rows(report)


def _print_rows(values: Mapping[str, Any], indent: str = "") -> None:
    for name, value in values.items():
        label = name.replace("_", " ")
        if isinstance(value, dict):
            print(f"{indent}{label}:")
            _print_rows(value, indent + "  ")
        elif isinstance(value, list):
            for period in value:
                print(f"\n{indent}{label}:")
                _print_rows(period, indent + "  ")
        else:
            print(f"{indent}{label}: {value}")


if __name__ == "__main__":
    main()
