#!/usr/bin/env python3
"""Patch user marketing opt-in state from a Brevo double-opt-in CSV export.

Default mode is dry-run. Pass --apply to write Firestore changes.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from firebase_admin import auth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend.firebase_app import get_firestore_client, initialize_firebase_app  # noqa: E402


SOURCE = "brevo_export_migration"
BREVO_STATUS = "already_in_list"
DEFAULT_CSV_PATH = "logs/brevo_contact_list_0518.csv"


@dataclass(frozen=True)
class MigrationRow:
    contact_id: str
    email: str
    double_opt_in: str
    added_time: datetime | None


@dataclass
class MigrationStats:
    rows_seen: int = 0
    rows_skipped_not_double_opt_in: int = 0
    rows_skipped_duplicate_email: int = 0
    auth_users_missing: int = 0
    firestore_users_missing: int = 0
    users_already_marked: int = 0
    users_to_patch: int = 0
    users_patched: int = 0
    errors: int = 0


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _parse_added_time(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported ADDED_TIME format: {value!r}")


def _is_double_opt_in(value: str) -> bool:
    return value.strip().casefold() == "yes"


def _read_rows(csv_path: Path) -> list[MigrationRow]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"EMAIL", "DOUBLE_OPT-IN", "ADDED_TIME"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(sorted(missing))}")

        rows: list[MigrationRow] = []
        for row in reader:
            email = _normalize_email(row.get("EMAIL", ""))
            if not email:
                continue
            rows.append(
                MigrationRow(
                    contact_id=str(row.get("CONTACT ID", "")).strip(),
                    email=email,
                    double_opt_in=str(row.get("DOUBLE_OPT-IN", "")).strip(),
                    added_time=_parse_added_time(str(row.get("ADDED_TIME", "")).strip()),
                )
            )
        return rows


def _marketing_payload(email: str, requested_at: datetime) -> dict[str, Any]:
    return {
        "marketing": {
            "emailOptInRequested": True,
            "emailOptInRequestedAt": requested_at,
            "emailOptInSource": SOURCE,
            "emailOptInEmail": email,
            "emailOptInConsentText": "",
            "emailOptInBrevoStatus": BREVO_STATUS,
        }
    }


def _marketing_requested(user_data: dict[str, Any]) -> bool:
    marketing = user_data.get("marketing")
    return isinstance(marketing, dict) and marketing.get("emailOptInRequested") is True


def _resolve_auth_user(email: str) -> auth.UserRecord | None:
    try:
        return auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        return None


def _validate_target(target: str) -> None:
    firestore_emulator = os.getenv("FIRESTORE_EMULATOR_HOST")
    auth_emulator = os.getenv("FIREBASE_AUTH_EMULATOR_HOST")
    if target == "local":
        missing = []
        if not firestore_emulator:
            missing.append("FIRESTORE_EMULATOR_HOST")
        if not auth_emulator:
            missing.append("FIREBASE_AUTH_EMULATOR_HOST")
        if missing:
            raise RuntimeError(
                "Local target requires emulator environment variable(s): "
                + ", ".join(missing)
            )
    elif target == "production":
        if firestore_emulator or auth_emulator:
            raise RuntimeError(
                "Production target requested, but emulator environment variables are set. "
                "Unset FIRESTORE_EMULATOR_HOST and FIREBASE_AUTH_EMULATOR_HOST first."
            )
    else:
        raise ValueError(f"Unsupported target: {target}")


def migrate(csv_path: Path, *, apply: bool, target: str) -> MigrationStats:
    _validate_target(target)
    initialize_firebase_app()
    db = get_firestore_client()
    rows = _read_rows(csv_path)
    stats = MigrationStats(rows_seen=len(rows))
    seen_emails: set[str] = set()

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"mode={mode} target={target} csv={csv_path}")
    if os.getenv("FIRESTORE_EMULATOR_HOST"):
        print(f"firestore_emulator={os.getenv('FIRESTORE_EMULATOR_HOST')}")
    if os.getenv("FIREBASE_AUTH_EMULATOR_HOST"):
        print(f"auth_emulator={os.getenv('FIREBASE_AUTH_EMULATOR_HOST')}")

    for row in rows:
        if not _is_double_opt_in(row.double_opt_in):
            stats.rows_skipped_not_double_opt_in += 1
            print(f"skip_not_double_opt_in email={row.email} value={row.double_opt_in!r}")
            continue
        if row.email in seen_emails:
            stats.rows_skipped_duplicate_email += 1
            print(f"skip_duplicate_email email={row.email}")
            continue
        seen_emails.add(row.email)

        try:
            auth_user = _resolve_auth_user(row.email)
            if auth_user is None:
                stats.auth_users_missing += 1
                print(f"skip_auth_user_not_found email={row.email}")
                continue

            user_ref = db.collection("users").document(auth_user.uid)
            snapshot = user_ref.get()
            if not snapshot.exists:
                stats.firestore_users_missing += 1
                print(f"skip_firestore_user_not_found email={row.email} uid={auth_user.uid}")
                continue

            user_data = snapshot.to_dict() or {}
            if _marketing_requested(user_data):
                stats.users_already_marked += 1
                print(f"skip_already_marked email={row.email} uid={auth_user.uid}")
                continue

            requested_at = row.added_time or datetime.now(timezone.utc)
            payload = _marketing_payload(row.email, requested_at)
            stats.users_to_patch += 1
            print(
                "patch_candidate "
                f"email={row.email} uid={auth_user.uid} "
                f"requested_at={requested_at.isoformat()}"
            )
            if apply:
                user_ref.set(payload, merge=True)
                stats.users_patched += 1
        except Exception as exc:  # noqa: BLE001 - migration should continue per row.
            stats.errors += 1
            print(f"error email={row.email} error={type(exc).__name__}: {exc}", file=sys.stderr)

    print(
        "summary "
        f"rows_seen={stats.rows_seen} "
        f"rows_skipped_not_double_opt_in={stats.rows_skipped_not_double_opt_in} "
        f"rows_skipped_duplicate_email={stats.rows_skipped_duplicate_email} "
        f"auth_users_missing={stats.auth_users_missing} "
        f"firestore_users_missing={stats.firestore_users_missing} "
        f"users_already_marked={stats.users_already_marked} "
        f"users_to_patch={stats.users_to_patch} "
        f"users_patched={stats.users_patched} "
        f"errors={stats.errors}"
    )
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch Firestore user marketing fields from a Brevo DOI contact CSV."
    )
    parser.add_argument(
        "--csv",
        default=DEFAULT_CSV_PATH,
        help=f"Brevo CSV path. Default: {DEFAULT_CSV_PATH}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write Firestore changes. Without this flag, only prints candidates.",
    )
    parser.add_argument(
        "--target",
        choices=("local", "production"),
        required=True,
        help="Required safety switch. Use local for emulators, production for live Firebase.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        csv_path = PROJECT_ROOT / csv_path
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2
    stats = migrate(csv_path, apply=args.apply, target=args.target)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
