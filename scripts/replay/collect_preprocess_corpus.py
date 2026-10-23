#!/usr/bin/env python3
"""Collect recent preprocess inputs for offline planner replay.

Firestore and Storage are only read. The destination must be supplied explicitly.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_BUCKET = "sightsinger-app.firebasestorage.app"
INDEX_COLUMNS = (
    "job_id", "created_at", "status", "score_title", "demo_song_id",
    "input_sha256", "duration_s", "processing_s", "processing_source", "dispatch_attempts",
)
PRIVATE_KEYS = {"userid", "sessionid", "useremail", "originatingturnid", "llmcallsused"}
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Selection:
    jobs: list[dict[str, Any]]
    scanned: int
    deduped: int


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _spec(job: dict[str, Any]) -> dict[str, Any] | None:
    try:
        parsed = json.loads(job.get("preprocessSpecJson") or "")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _dedupe_key(job: dict[str, Any], spec: dict[str, Any]) -> tuple[str, str, str, str]:
    context = spec.get("planningContext")
    if not isinstance(context, dict):
        context = {}
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (
        str(job.get("inputSha256") or ""),
        canonical(context.get("target")),
        canonical(context.get("other_instruction")),
        canonical(context.get("user_request")),
    )


def select_jobs(
    job_dicts: Iterable[dict[str, Any]], days: int, max_jobs: int, now: datetime
) -> Selection:
    """Keep newest eligible job for each score and planning request."""
    if days < 0 or max_jobs < 1:
        raise ValueError("days must be non-negative and max_jobs must be positive")
    cutoff = _utc(now) - timedelta(days=days)
    fetched = list(job_dicts)
    ordered = sorted(fetched, key=lambda job: _utc(job.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    deduped = 0
    for job in ordered:
        created_at = _utc(job.get("createdAt"))
        if created_at is None or created_at < cutoff:
            continue
        if job.get("jobKind") != "preprocess" or job.get("status") not in {"completed", "failed"}:
            continue
        if not isinstance(job.get("inputPath"), str) or not job["inputPath"].strip():
            continue
        spec = _spec(job)
        if spec is None:
            continue
        key = _dedupe_key(job, spec)
        if key in seen:
            deduped += 1
            continue
        seen.add(key)
        selected.append(job)
        if len(selected) >= max_jobs:
            break
    return Selection(selected, len(fetched), deduped)


def _without_private_data(value: Any, private_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_private_data(item, private_values)
            for key, item in value.items()
            if isinstance(key, str) and key.lower() not in PRIVATE_KEYS and "email" not in key.lower()
        }
    if isinstance(value, list):
        return [_without_private_data(item, private_values) for item in value]
    if isinstance(value, str):
        cleaned = EMAIL_PATTERN.sub("[redacted email]", value)
        for private_value in private_values:
            cleaned = cleaned.replace(private_value, "[redacted id]")
        return cleaned
    return value


def _duration(job: dict[str, Any]) -> float | None:
    start = _utc(job.get("createdAt"))
    end = _utc(job.get("completedAt")) or _utc(job.get("updatedAt"))
    return (end - start).total_seconds() if start and end else None


def _job_metadata(
    job_id: str, job: dict[str, Any], processing_started_at: datetime | None = None
) -> dict[str, Any]:
    spec = _spec(job)
    if spec is None:
        raise ValueError("preprocessSpecJson must be a JSON object")
    created_at = _utc(job.get("createdAt"))
    if created_at is None:
        raise ValueError("createdAt is required")
    completed_at = _utc(job.get("completedAt"))
    end = completed_at or _utc(job.get("updatedAt"))
    worker_start = _utc(processing_started_at)
    worker_timed = worker_start is not None and end is not None
    processing_s = (end - worker_start).total_seconds() if worker_timed else _duration(job)
    dispatch_attempts = job.get("dispatchAttempt")
    if not isinstance(dispatch_attempts, int) or isinstance(dispatch_attempts, bool):
        dispatch_attempts = None
    replay_spec = {
        "toolCalls": spec.get("toolCalls", []),
        "planningContext": spec.get("planningContext"),
        "initialMessage": spec.get("initialMessage", ""),
        "initialThoughtSummary": spec.get("initialThoughtSummary", ""),
    }
    metadata = {
        "job_id": job_id,
        "created_at": created_at.isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else None,
        "expand_repeats": job.get("expandRepeats"),
        "input_sha256": job.get("inputSha256"),
        "input_file_name": job.get("inputFileName"),
        "score_title": job.get("scoreTitle"),
        "demo_song_id": job.get("demoSongId"),
        "spec": replay_spec,
        "prod_outcome": {
            "status": job["status"],
            "step": job.get("step"),
            "review_required": job.get("reviewRequired"),
            "message": job.get("message"),
            "error_message": job.get("errorMessage"),
            "warning_message": job.get("warningMessage"),
            "details": job.get("details"),
            "duration_s": _duration(job),
            "processing_s": processing_s,
            "processing_source": "worker_logs" if worker_timed else "created_at_fallback",
            "dispatch_attempts": dispatch_attempts,
        },
    }
    private_values = tuple(
        sorted(
            {
                value for value in (
                    job.get("userId"), job.get("sessionId"),
                    spec.get("originatingTurnId"),
                ) if isinstance(value, str) and value
            },
            key=len,
            reverse=True,
        )
    )
    return _without_private_data(metadata, private_values)


def write_corpus_entry(
    out_dir: Path, job_id: str, job: dict[str, Any], xml_bytes: bytes,
    *, processing_started_at: datetime | None = None,
) -> dict[str, Any]:
    """Write an exact score copy and the fixed replay metadata interface."""
    if not job_id or job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
        raise ValueError("unsafe job id")
    metadata = _job_metadata(job_id, job, processing_started_at)
    entry = Path(out_dir) / job_id
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "input.xml").write_bytes(xml_bytes)
    (entry / "job.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def _read_first_worker_log(
    job_id: str, created_at: datetime, end: datetime, project: str
) -> datetime | None:
    def utc_z(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    start_bound = utc_z(created_at - timedelta(minutes=2))
    end_bound = utc_z(end + timedelta(minutes=2))
    filter_text = (
        'resource.type="cloud_run_revision" '
        'AND resource.labels.service_name="sightsinger-preprocess-worker" '
        f"AND jsonPayload.job_id={json.dumps(job_id)} "
        f'AND timestamp>="{start_bound}" AND timestamp<="{end_bound}"'
    )
    result = subprocess.run(
        ["gcloud", "logging", "read", filter_text, "--project", project,
         "--order=asc", "--limit=1", "--format=value(timestamp)"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    first_line = result.stdout.strip().splitlines()
    return _utc(first_line[0]) if first_line else None


def collect(
    job_dicts: Iterable[dict[str, Any]], *, days: int, max_jobs: int,
    out_dir: Path, bucket: str, dry_run: bool, now: datetime,
    download: Callable[[str, str], bytes],
    project: str = "sightsinger-app",
    read_first_log: Callable[[str, datetime, datetime, str], datetime | None] | None = None,
) -> dict[str, int]:
    selection = select_jobs(job_dicts, days, max_jobs, now)
    counts = {"scanned": selection.scanned, "kept": 0, "deduped": selection.deduped, "failed": 0}
    if dry_run:
        for job in selection.jobs:
            print(f"{job['job_id']} {job['createdAt']} {job['status']}")
        counts["kept"] = len(selection.jobs)
    else:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "index.csv").open("w", newline="", encoding="utf-8") as index_file:
            writer = csv.DictWriter(index_file, fieldnames=INDEX_COLUMNS)
            writer.writeheader()
            for job in selection.jobs:
                job_id = str(job["job_id"])
                try:
                    xml_bytes = download(bucket, job["inputPath"])
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"download failed for {job_id}: {type(exc).__name__}", file=sys.stderr)
                    continue
                created_at = _utc(job.get("createdAt"))
                end = _utc(job.get("completedAt")) or _utc(job.get("updatedAt"))
                first_log = None
                if created_at and end:
                    try:
                        reader = read_first_log or _read_first_worker_log
                        first_log = reader(job_id, created_at, end, project)
                    except Exception:
                        # A failed or timed-out log read must not stop collection.
                        pass
                metadata = write_corpus_entry(
                    out_dir, job_id, job, xml_bytes, processing_started_at=first_log
                )
                outcome = metadata["prod_outcome"]
                writer.writerow({
                    "job_id": metadata["job_id"],
                    "created_at": metadata["created_at"],
                    "status": outcome["status"],
                    "score_title": metadata["score_title"],
                    "demo_song_id": metadata["demo_song_id"],
                    "input_sha256": metadata["input_sha256"],
                    "duration_s": outcome["duration_s"],
                    "processing_s": outcome["processing_s"],
                    "processing_source": outcome["processing_source"],
                    "dispatch_attempts": outcome["dispatch_attempts"],
                })
                counts["kept"] += 1
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    return counts


def _require_credentials() -> Path:
    service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
    google_adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    files = [value for value in (service_account_path, google_adc_path) if value]
    if not files:
        raise SystemExit(
            "Missing credentials. Set GOOGLE_APPLICATION_CREDENTIALS or "
            "FIREBASE_SERVICE_ACCOUNT_FILE to a local Firebase service account JSON path."
        )
    resolved = Path(files[0]).expanduser()
    if not resolved.is_file():
        raise SystemExit(f"Credential file not found: {resolved}")
    if service_account_path:
        os.environ["FIREBASE_SERVICE_ACCOUNT_FILE"] = str(resolved)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolved)
    return resolved


def _validate_target(target: str) -> None:
    firestore_emulator = os.getenv("FIRESTORE_EMULATOR_HOST")
    storage_emulator = os.getenv("FIREBASE_STORAGE_EMULATOR_HOST") or os.getenv("STORAGE_EMULATOR_HOST")
    if target == "production":
        if firestore_emulator or storage_emulator:
            raise RuntimeError(
                "Production target requested, but emulator environment variables are set. "
                "Unset FIRESTORE_EMULATOR_HOST/FIREBASE_STORAGE_EMULATOR_HOST/STORAGE_EMULATOR_HOST first."
            )
        return
    if target == "local":
        if not firestore_emulator:
            raise RuntimeError("Local target requires FIRESTORE_EMULATOR_HOST.")
        return
    raise ValueError(f"Unsupported target: {target}")


def _query_jobs(
    now: datetime, days: int, *, client: Any = None, firestore_module: Any = None
) -> Iterable[dict[str, Any]]:
    if firestore_module is None:
        from firebase_admin import firestore as firestore_module
    if client is None:
        from src.backend.firebase_app import get_firestore_client

        client = get_firestore_client()

    jobs = client.collection("jobs")
    cutoff = now - timedelta(days=days)
    for status in ("completed", "failed"):
        query = (
            jobs.where(filter=firestore_module.FieldFilter("jobKind", "==", "preprocess"))
            .where(filter=firestore_module.FieldFilter("status", "==", status))
            .where(filter=firestore_module.FieldFilter("updatedAt", ">=", cutoff))
            .order_by("updatedAt", direction=firestore_module.Query.DESCENDING)
        )
        for snapshot in query.stream():
            yield {"job_id": snapshot.id, **(snapshot.to_dict() or {})}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("production", "local"), required=True)
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID") or "sightsinger-app")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-jobs", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.days < 0 or args.max_jobs < 1:
        raise SystemExit("--days must be non-negative and --max-jobs must be positive")
    _validate_target(args.target)
    _require_credentials()
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    from src.backend.firebase_app import initialize_firebase_app
    from src.backend.storage_client import download_bytes

    initialize_firebase_app()
    now = datetime.now(timezone.utc)
    collect(
        _query_jobs(now, args.days), days=args.days, max_jobs=args.max_jobs,
        out_dir=args.out, bucket=args.bucket, dry_run=args.dry_run, now=now,
        download=download_bytes, project=args.project,
    )


if __name__ == "__main__":
    main()
