from __future__ import annotations

"""Authenticated marketing email opt-in."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from src.backend.config import Settings
from src.backend.firebase_app import get_firestore_client
from src.backend.waitlist import WaitlistResult, subscribe_to_waitlist

MARKETING_STATUS_ALREADY_REQUESTED = "already_requested"
MARKETING_STATUS_DOI_REQUESTED = "doi_requested"
MARKETING_STATUS_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"

MarketingOptInStatus = Literal[
    "already_requested",
    "doi_requested",
    "dependency_unavailable",
]


@dataclass(frozen=True)
class MarketingOptInResult:
    success: bool
    status: MarketingOptInStatus
    message: str
    requires_confirmation: bool
    status_code: int = 200


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _marketing_data(user_data: dict[str, Any]) -> dict[str, Any]:
    marketing = user_data.get("marketing")
    return marketing if isinstance(marketing, dict) else {}


def _has_requested_marketing_opt_in(user_data: dict[str, Any]) -> bool:
    marketing = _marketing_data(user_data)
    if not marketing.get("emailOptInRequested"):
        return False
    return marketing.get("emailOptInBrevoStatus") not in {"doi_expired", "doi_suppressed"}


def _marketing_payload(
    *,
    email: str,
    source: str,
    consent_text: str,
    brevo_status: str,
    now: datetime,
    reminder_delay_days: int,
    expiry_days: int,
) -> dict[str, Any]:
    expires_at = now + timedelta(days=expiry_days)
    return {
        "marketing": {
            "emailOptInRequested": True,
            "emailOptInRequestedAt": now,
            "emailOptInSource": source,
            "emailOptInEmail": email,
            "emailOptInConsentText": consent_text,
            "emailOptInBrevoStatus": brevo_status,
            "emailOptInBrevoCycleId": str(uuid4()),
            "emailOptInBrevoOriginalRequestedAt": now,
            "emailOptInBrevoInitialDoiSentAt": now,
            "emailOptInBrevoReminderSentAt": None,
            "emailOptInBrevoNextActionAt": now + timedelta(days=reminder_delay_days),
            "emailOptInBrevoExpiresAt": expires_at,
            "emailOptInBrevoConfirmedAt": None,
            "emailOptInBrevoSuppressedAt": None,
            "emailOptInBrevoSuppressionReason": None,
            "emailOptInBrevoLastProviderOutcome": "initial_doi_accepted",
            "emailOptInBrevoReminderClaimedAt": None,
        }
    }


def mark_marketing_opt_in_requested(
    *,
    uid: str,
    email: str,
    source: str,
    consent_text: str,
    brevo_status: str,
    settings: Settings,
) -> bool:
    """Persist a fresh successful request without overwriting an active DOI cycle."""
    now = datetime.now(timezone.utc)
    user_ref = get_firestore_client().collection("users").document(uid)
    existing = user_ref.get()
    existing_data = existing.to_dict() if existing.exists else {}
    if existing_data and _has_requested_marketing_opt_in(existing_data):
        return False
    user_ref.set(
        _marketing_payload(
            email=_normalize_email(email),
            source=source,
            consent_text=consent_text,
            brevo_status=brevo_status,
            now=now,
            reminder_delay_days=settings.marketing_doi_reminder_delay_days,
            expiry_days=settings.marketing_doi_expiry_days,
        ),
        merge=True,
    )
    return True


async def request_authenticated_marketing_opt_in(
    settings: Settings,
    *,
    uid: str,
    email: str,
    source: str,
    consent_text: str,
) -> MarketingOptInResult:
    """Idempotently request marketing opt-in for an authenticated user."""
    normalized_email = _normalize_email(email)
    user_ref = get_firestore_client().collection("users").document(uid)
    user_snapshot = user_ref.get()
    user_data = user_snapshot.to_dict() if user_snapshot.exists else {}
    user_data = user_data or {}
    if _has_requested_marketing_opt_in(user_data):
        return MarketingOptInResult(
            success=True,
            status=MARKETING_STATUS_ALREADY_REQUESTED,
            message="Marketing opt-in already requested.",
            requires_confirmation=True,
        )

    waitlist_result: WaitlistResult = await subscribe_to_waitlist(
        settings,
        email=normalized_email,
        first_name=None,
        feedback=None,
        gdpr_consent=True,
        consent_text=consent_text,
        source=source,
    )
    if not waitlist_result.success:
        return MarketingOptInResult(
            success=False,
            status=MARKETING_STATUS_DEPENDENCY_UNAVAILABLE,
            message=waitlist_result.message,
            requires_confirmation=False,
            status_code=waitlist_result.status_code,
        )

    mark_marketing_opt_in_requested(
        uid=uid,
        email=normalized_email,
        source=source,
        consent_text=consent_text,
        brevo_status="doi_requested",
        settings=settings,
    )
    return MarketingOptInResult(
        success=True,
        status=MARKETING_STATUS_DOI_REQUESTED,
        message="Check your inbox to confirm your email subscription.",
        requires_confirmation=True,
    )
