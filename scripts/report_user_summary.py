from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from firebase_admin import auth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend.firebase_app import get_firestore_client, initialize_firebase_app


CSV_COLUMNS = [
    "user_name",
    "email",
    "first_created_at",
    "last_logged_in_at",
    "completed_synthesis_jobs",
    "credit_balance",
    "credit_reserved",
    "available_credit_balance",
    "trial_expires_at",
    "is_trial_expired",
    "last_completed_synthesis_at",
    "trial_status",
    "engagement_bucket",
    "email_batch_candidate",
]


@dataclass(frozen=True)
class AuthUserSummary:
    user_name: str
    email: str
    first_created_at: str
    last_logged_in_at: str


@dataclass(frozen=True)
class JobAggregate:
    completed_synthesis_jobs: int
    last_completed_synthesis_at: str


def _require_credentials() -> Path:
    """Validate local service-account-based auth env before touching Firebase."""
    service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
    google_adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    credential_files = [value for value in (service_account_path, google_adc_path) if value]
    if not credential_files:
        raise SystemExit(
            "Missing credentials. Set GOOGLE_APPLICATION_CREDENTIALS or "
            "FIREBASE_SERVICE_ACCOUNT_FILE to a local Firebase service account JSON path."
        )

    resolved = Path(credential_files[0]).expanduser()
    if not resolved.is_file():
        raise SystemExit(f"Credential file not found: {resolved}")

    if service_account_path:
        os.environ["FIREBASE_SERVICE_ACCOUNT_FILE"] = str(resolved)
    if google_adc_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolved)
    if not google_adc_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolved)
    return resolved


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a local user summary CSV report from Firebase Auth and Firestore."
    )
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID"),
        help="Google Cloud / Firebase project ID.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output CSV file path.",
    )
    return parser.parse_args()


def _iso_utc(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _iso_utc_from_millis(value: Optional[int]) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _bool_text(value: Optional[bool]) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return ""


def _trial_status(is_trial_expired: Optional[bool]) -> str:
    if is_trial_expired is True:
        return "expired"
    if is_trial_expired is False:
        return "active"
    return "unknown"


def _engagement_bucket(completed_synthesis_jobs: int) -> str:
    if completed_synthesis_jobs <= 0:
        return "none"
    if completed_synthesis_jobs <= 2:
        return "light"
    if completed_synthesis_jobs <= 9:
        return "engaged"
    return "power"


def _email_batch_candidate(
    *,
    email: str,
    completed_synthesis_jobs: int,
    is_trial_expired: Optional[bool],
    available_credit_balance: int,
) -> bool:
    if not email or completed_synthesis_jobs < 1:
        return False
    return bool(is_trial_expired is True or available_credit_balance <= 0)


def _is_anonymous_or_unusable(user: auth.ExportedUserRecord) -> bool:
    if not getattr(user, "email", None):
        return True
    provider_data = getattr(user, "provider_data", None) or []
    return len(provider_data) == 0


def _list_auth_users() -> Dict[str, AuthUserSummary]:
    auth_users: Dict[str, AuthUserSummary] = {}
    page = auth.list_users()
    while page is not None:
        for user in page.users:
            if _is_anonymous_or_unusable(user):
                continue
            metadata = user.user_metadata
            auth_users[user.uid] = AuthUserSummary(
                user_name=user.display_name or "",
                email=user.email or "",
                first_created_at=_iso_utc_from_millis(getattr(metadata, "creation_timestamp", None)),
                last_logged_in_at=_iso_utc_from_millis(getattr(metadata, "last_sign_in_timestamp", None)),
            )
        page = page.get_next_page()
    return auth_users


def _load_credit_users() -> tuple[Dict[str, Dict[str, Any]], int]:
    db = get_firestore_client()
    docs = db.collection("users").stream()
    credit_users: Dict[str, Dict[str, Any]] = {}
    count = 0
    for doc in docs:
        count += 1
        credit_users[doc.id] = doc.to_dict() or {}
    return credit_users, count


def _is_completed_synthesis_job(data: Dict[str, Any]) -> bool:
    if data.get("status") != "completed":
        return False
    job_kind = data.get("jobKind")
    if job_kind == "synthesis":
        return True
    if job_kind:
        return False
    return bool(data.get("audioUrl") or data.get("outputPath"))


def _aggregate_jobs() -> tuple[Dict[str, JobAggregate], int]:
    db = get_firestore_client()
    docs = db.collection("jobs").stream()
    aggregates: Dict[str, Dict[str, Any]] = {}
    scanned = 0
    for doc in docs:
        scanned += 1
        data = doc.to_dict() or {}
        if not _is_completed_synthesis_job(data):
            continue
        user_id = data.get("userId")
        if not user_id:
            continue
        aggregate = aggregates.setdefault(
            user_id,
            {"completed_synthesis_jobs": 0, "updated_ats": []},
        )
        aggregate["completed_synthesis_jobs"] += 1
        updated_at = data.get("updatedAt")
        if isinstance(updated_at, datetime):
            aggregate["updated_ats"].append(updated_at)

    job_aggregates: Dict[str, JobAggregate] = {}
    for user_id, aggregate in aggregates.items():
        updated_ats = aggregate["updated_ats"]
        latest = max(updated_ats) if updated_ats else None
        job_aggregates[user_id] = JobAggregate(
            completed_synthesis_jobs=int(aggregate["completed_synthesis_jobs"]),
            last_completed_synthesis_at=_iso_utc(latest),
        )
    return job_aggregates, scanned


def _iter_rows(
    auth_users: Dict[str, AuthUserSummary],
    credit_users: Dict[str, Dict[str, Any]],
    job_aggregates: Dict[str, JobAggregate],
) -> Iterable[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    for uid in sorted(auth_users.keys()):
        auth_user = auth_users[uid]
        user_doc = credit_users.get(uid, {})
        credits = user_doc.get("credits") or {}

        credit_balance = int(credits.get("balance", 0) or 0)
        credit_reserved = int(credits.get("reserved", 0) or 0)
        available_credit_balance = credit_balance - credit_reserved

        trial_expires_at_dt = credits.get("expiresAt")
        trial_expires_at = _iso_utc(trial_expires_at_dt)
        compare_dt: Optional[datetime]
        if isinstance(trial_expires_at_dt, datetime):
            compare_dt = (
                trial_expires_at_dt.replace(tzinfo=timezone.utc)
                if trial_expires_at_dt.tzinfo is None
                else trial_expires_at_dt.astimezone(timezone.utc)
            )
        else:
            compare_dt = None

        is_trial_expired: Optional[bool]
        if compare_dt is None:
            is_trial_expired = None
        else:
            is_trial_expired = compare_dt < now

        job_aggregate = job_aggregates.get(
            uid,
            JobAggregate(completed_synthesis_jobs=0, last_completed_synthesis_at=""),
        )

        row = {
            "user_name": auth_user.user_name,
            "email": auth_user.email,
            "first_created_at": auth_user.first_created_at or _iso_utc(user_doc.get("createdAt")),
            "last_logged_in_at": auth_user.last_logged_in_at,
            "completed_synthesis_jobs": job_aggregate.completed_synthesis_jobs,
            "credit_balance": credit_balance,
            "credit_reserved": credit_reserved,
            "available_credit_balance": available_credit_balance,
            "trial_expires_at": trial_expires_at,
            "is_trial_expired": _bool_text(is_trial_expired),
            "last_completed_synthesis_at": job_aggregate.last_completed_synthesis_at,
            "trial_status": _trial_status(is_trial_expired),
            "engagement_bucket": _engagement_bucket(job_aggregate.completed_synthesis_jobs),
            "email_batch_candidate": _bool_text(
                _email_batch_candidate(
                    email=auth_user.email,
                    completed_synthesis_jobs=job_aggregate.completed_synthesis_jobs,
                    is_trial_expired=is_trial_expired,
                    available_credit_balance=available_credit_balance,
                )
            ),
        }
        yield row


def _write_csv(rows: Iterable[Dict[str, Any]], output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            row_count += 1
    return row_count


def main() -> None:
    args = _parse_args()
    _require_credentials()
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
        os.environ["PROJECT_ID"] = args.project

    initialize_firebase_app()

    auth_users = _list_auth_users()
    credit_users, firestore_user_count = _load_credit_users()
    job_aggregates, job_scan_count = _aggregate_jobs()
    output_path = Path(args.output).expanduser()
    row_count = _write_csv(_iter_rows(auth_users, credit_users, job_aggregates), output_path)

    project_id = args.project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID") or ""
    print(f"project_id={project_id}")
    print(f"auth_users_scanned={len(auth_users)}")
    print(f"firestore_users_scanned={firestore_user_count}")
    print(f"firestore_jobs_scanned={job_scan_count}")
    print(f"rows_written={row_count}")
    print(f"output_path={output_path}")


if __name__ == "__main__":
    main()
