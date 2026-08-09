from __future__ import annotations

"""Reconcile marketing DOI state from Brevo and send at most one reminder."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from google.cloud import firestore

from src.backend.config import Settings
from src.backend.firebase_app import get_firestore_client
from src.backend.secret_manager import read_secret
from src.backend.waitlist import BREVO_DOI_ENDPOINT
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

_ACTIVE_STATUSES = {"doi_requested", "doi_reminder_sent"}
_LEASE_DURATION = timedelta(minutes=10)
_REMINDER_RETRY_DELAY = timedelta(days=1)
_CONTACTS_ENDPOINT = "https://api.brevo.com/v3/contacts"
_CONTACT_PAGE_SIZE = 1000


class BrevoSnapshotError(RuntimeError):
    """The provider contact snapshot cannot be used safely."""


class BrevoUnknownDeliveryError(RuntimeError):
    """A DOI send may have reached Brevo, so it must not be retried automatically."""


class BrevoReminderRejectedError(RuntimeError):
    """Brevo definitively rejected a DOI send before acceptance."""


@dataclass(frozen=True)
class BrevoContact:
    email: str
    list_ids: frozenset[int]
    email_blacklisted: bool
    list_unsubscribed: bool
    double_opt_in: Any


def run_marketing_doi_reconciliation(
    settings: Settings,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, int | bool | str]:
    """Run one bounded DOI reconciliation. The caller must enforce scheduler OIDC."""
    current_time = _ensure_utc(now or datetime.now(timezone.utc))
    current_run_id = run_id or f"marketing_doi_{current_time.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
    result: dict[str, int | bool | str] = _empty_result(current_run_id, settings)
    if not settings.marketing_doi_reconcile_enabled:
        result["status"] = "disabled"
        return result

    if not _acquire_run_lease(current_run_id, current_time):
        result["status"] = "already_running"
        return result

    try:
        contacts = fetch_brevo_contacts(settings)
        result["brevo_snapshot_contact_count"] = len(contacts)
        candidates = _fetch_active_candidates(settings.marketing_doi_max_active_users)
        result["has_more_candidates"] = len(candidates) >= settings.marketing_doi_max_active_users

        for snapshot in candidates:
            result["firestore_candidates_scanned"] = int(result["firestore_candidates_scanned"]) + 1
            _process_candidate(
                snapshot,
                contacts=contacts,
                settings=settings,
                now=current_time,
                result=result,
            )
        result["status"] = "ok"
        return result
    except Exception:
        result["status"] = "failed"
        logger.exception("marketing_doi_reconcile_failed run_id=%s", current_run_id)
        raise
    finally:
        _finish_run_lease(current_run_id, datetime.now(timezone.utc), result)


def fetch_brevo_contacts(settings: Settings) -> dict[str, BrevoContact]:
    """Fetch a complete, ephemeral contact map. A failed page fails the whole run."""
    api_key = read_secret(
        settings,
        settings.brevo_waitlist_api_key_secret,
        settings.brevo_waitlist_api_key_secret_version,
    )
    headers = {"api-key": api_key, "accept": "application/json"}
    contacts: dict[str, BrevoContact] = {}
    offset = 0
    try:
        with httpx.Client(timeout=settings.brevo_waitlist_timeout_seconds) as client:
            while True:
                response = client.get(
                    _CONTACTS_ENDPOINT,
                    headers=headers,
                    params={"limit": _CONTACT_PAGE_SIZE, "offset": offset},
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise BrevoSnapshotError("Brevo contacts response was not an object.")
                page = body.get("contacts")
                if not isinstance(page, list):
                    raise BrevoSnapshotError("Brevo contacts response did not contain a contacts list.")
                for raw_contact in page:
                    contact = _parse_brevo_contact(raw_contact)
                    if contact is not None:
                        contacts[contact.email] = contact
                if len(page) < _CONTACT_PAGE_SIZE:
                    break
                offset += len(page)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise BrevoSnapshotError("Unable to fetch a complete Brevo contact snapshot.") from exc
    return contacts


def _parse_brevo_contact(raw_contact: Any) -> BrevoContact | None:
    if not isinstance(raw_contact, dict):
        return None
    email = _normalize_email(raw_contact.get("email"))
    if not email:
        return None
    list_ids: set[int] = set()
    for value in raw_contact.get("listIds") or []:
        try:
            list_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    attributes = raw_contact.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    return BrevoContact(
        email=email,
        list_ids=frozenset(list_ids),
        email_blacklisted=bool(raw_contact.get("emailBlacklisted")),
        list_unsubscribed=bool(raw_contact.get("listUnsubscribed")),
        double_opt_in=attributes.get("DOUBLE-OPT-IN", attributes.get("DOUBLE_OPT_IN")),
    )


def _fetch_active_candidates(limit: int):
    db = get_firestore_client()
    query = db.collection("users").where(
        filter=firestore.FieldFilter(
            "marketing.emailOptInBrevoStatus",
            "in",
            sorted(_ACTIVE_STATUSES),
        )
    ).limit(limit)
    return list(query.stream())


def _process_candidate(
    snapshot,
    *,
    contacts: dict[str, BrevoContact],
    settings: Settings,
    now: datetime,
    result: dict[str, int | bool | str],
) -> None:
    data = snapshot.to_dict() or {}
    marketing = _marketing_data(data)
    normalized = _normalize_legacy_candidate(snapshot.reference, marketing, settings, now)
    if normalized is None:
        result["skipped_invalid"] = int(result["skipped_invalid"]) + 1
        return
    marketing = normalized
    email = _normalize_email(marketing.get("emailOptInEmail") or data.get("email"))
    contact = contacts.get(email) if email else None

    if contact and (contact.email_blacklisted or contact.list_unsubscribed):
        if _set_terminal_state(
            snapshot.reference,
            marketing,
            now=now,
            status="doi_suppressed",
            updates={
                "marketing.emailOptInBrevoSuppressedAt": now,
                "marketing.emailOptInBrevoSuppressionReason": (
                    "email_blacklisted" if contact.email_blacklisted else "list_unsubscribed"
                ),
                "marketing.emailOptInBrevoLastProviderOutcome": "brevo_suppressed",
            },
        ):
            result["suppressed_reconciled"] = int(result["suppressed_reconciled"]) + 1
        return

    if contact and settings.brevo_waitlist_list_id in contact.list_ids:
        if _set_terminal_state(
            snapshot.reference,
            marketing,
            now=now,
            status="doi_confirmed",
            updates={
                "marketing.emailOptInBrevoConfirmedAt": now,
                "marketing.emailOptInBrevoLastProviderOutcome": "brevo_final_list_member",
            },
        ):
            result["confirmed_reconciled"] = int(result["confirmed_reconciled"]) + 1
        return

    expires_at = _as_utc(marketing.get("emailOptInBrevoExpiresAt"))
    if expires_at and expires_at <= now:
        if settings.marketing_doi_reconcile_mode == "send":
            if _set_terminal_state(
                snapshot.reference,
                marketing,
                now=now,
                status="doi_expired",
                updates={"marketing.emailOptInBrevoLastProviderOutcome": "doi_expired"},
            ):
                result["expired"] = int(result["expired"]) + 1
        else:
            result["skipped_observe_mode"] = int(result["skipped_observe_mode"]) + 1
        return

    next_action_at = _as_utc(marketing.get("emailOptInBrevoNextActionAt"))
    status = str(marketing.get("emailOptInBrevoStatus") or "")
    if status != "doi_requested" or next_action_at is None or next_action_at > now:
        result["skipped_not_due"] = int(result["skipped_not_due"]) + 1
        return
    if str(marketing.get("emailOptInBrevoLastProviderOutcome") or "") == "unknown_delivery":
        result["unknown_delivery"] = int(result["unknown_delivery"]) + 1
        return
    if settings.marketing_doi_reconcile_mode != "send":
        result["skipped_observe_mode"] = int(result["skipped_observe_mode"]) + 1
        return
    if not email:
        result["skipped_invalid"] = int(result["skipped_invalid"]) + 1
        return
    if not _claim_reminder(snapshot.reference, marketing, now):
        result["skipped_lease_held"] = int(result["skipped_lease_held"]) + 1
        return

    try:
        send_doi_reminder(settings, email=email, marketing=marketing)
    except BrevoUnknownDeliveryError:
        _record_reminder_outcome(
            snapshot.reference,
            marketing,
            now=now,
            outcome="unknown_delivery",
            next_action_at=expires_at,
            retain_claim=True,
        )
        result["unknown_delivery"] = int(result["unknown_delivery"]) + 1
    except BrevoReminderRejectedError:
        _record_reminder_outcome(
            snapshot.reference,
            marketing,
            now=now,
            outcome="reminder_rejected",
            next_action_at=min(now + _REMINDER_RETRY_DELAY, expires_at) if expires_at else now + _REMINDER_RETRY_DELAY,
            retain_claim=False,
        )
        result["provider_failures"] = int(result["provider_failures"]) + 1
    else:
        if _mark_reminder_sent(snapshot.reference, marketing, now):
            result["reminders_sent"] = int(result["reminders_sent"]) + 1


def send_doi_reminder(settings: Settings, *, email: str, marketing: dict[str, Any]) -> None:
    """Ask Brevo for one new DOI email without changing original consent evidence."""
    api_key = read_secret(
        settings,
        settings.brevo_waitlist_api_key_secret,
        settings.brevo_waitlist_api_key_secret_version,
    )
    payload = {
        "email": email,
        "includeListIds": [settings.brevo_waitlist_list_id],
        "templateId": settings.brevo_doi_template_id,
        "redirectionUrl": settings.brevo_doi_redirect_url,
        "attributes": _reminder_attributes(marketing),
    }
    try:
        with httpx.Client(timeout=settings.brevo_waitlist_timeout_seconds) as client:
            response = client.post(
                BREVO_DOI_ENDPOINT,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise BrevoUnknownDeliveryError("DOI reminder delivery is ambiguous.") from exc
    if response.status_code in {200, 201, 204}:
        return
    if response.status_code == 400 and "already exists" in response.text.lower():
        raise BrevoUnknownDeliveryError("Brevo did not confirm a fresh DOI reminder.")
    raise BrevoReminderRejectedError(f"Brevo rejected DOI reminder with HTTP {response.status_code}.")


def _reminder_attributes(marketing: dict[str, Any]) -> dict[str, Any]:
    """Preserve consent provenance and omit the original consent timestamp on resend."""
    attributes: dict[str, Any] = {}
    source = marketing.get("emailOptInSource")
    if source:
        attributes["SIGNUP_SOURCE"] = str(source)
    consent_text = marketing.get("emailOptInConsentText")
    if consent_text:
        attributes["GDPR_CONSENT_TEXT"] = str(consent_text)
    return attributes


def _normalize_legacy_candidate(user_ref, marketing: dict[str, Any], settings: Settings, now: datetime) -> dict[str, Any] | None:
    original = _as_utc(
        marketing.get("emailOptInBrevoOriginalRequestedAt")
        or marketing.get("emailOptInRequestedAt")
    )
    if original is None:
        return None
    cycle_id = str(marketing.get("emailOptInBrevoCycleId") or "")
    expires_at = _as_utc(marketing.get("emailOptInBrevoExpiresAt"))
    next_action_at = _as_utc(marketing.get("emailOptInBrevoNextActionAt"))
    if cycle_id and expires_at and next_action_at:
        return marketing
    normalized = {
        **marketing,
        "emailOptInBrevoCycleId": cycle_id or str(uuid4()),
        "emailOptInBrevoOriginalRequestedAt": original,
        "emailOptInBrevoInitialDoiSentAt": _as_utc(marketing.get("emailOptInBrevoInitialDoiSentAt")) or original,
        "emailOptInBrevoReminderSentAt": _as_utc(marketing.get("emailOptInBrevoReminderSentAt")),
        "emailOptInBrevoNextActionAt": next_action_at or original + timedelta(days=settings.marketing_doi_reminder_delay_days),
        "emailOptInBrevoExpiresAt": expires_at or original + timedelta(days=settings.marketing_doi_expiry_days),
        "emailOptInBrevoLastProviderOutcome": marketing.get("emailOptInBrevoLastProviderOutcome") or "legacy_normalized",
    }
    get_firestore_client().collection("users").document(user_ref.id).set(
        {"marketing": normalized},
        merge=True,
    )
    return normalized


def _claim_reminder(user_ref, marketing: dict[str, Any], now: datetime) -> bool:
    db = get_firestore_client()
    cycle_id = str(marketing["emailOptInBrevoCycleId"])
    original = _as_utc(marketing["emailOptInBrevoOriginalRequestedAt"])

    @firestore.transactional
    def _claim(transaction):
        snapshot = user_ref.get(transaction=transaction)
        current = _marketing_data(snapshot.to_dict() or {})
        if not _matches_cycle(current, cycle_id, original) or current.get("emailOptInBrevoStatus") != "doi_requested":
            return False
        claimed_at = _as_utc(current.get("emailOptInBrevoReminderClaimedAt"))
        if claimed_at and claimed_at + _LEASE_DURATION > now:
            return False
        transaction.update(
            user_ref,
            {
                "marketing.emailOptInBrevoReminderClaimedAt": now,
                "marketing.emailOptInBrevoLastProviderOutcome": "reminder_claimed",
            },
        )
        return True

    return bool(_claim(db.transaction()))


def _mark_reminder_sent(user_ref, marketing: dict[str, Any], now: datetime) -> bool:
    return _set_state_if_current(
        user_ref,
        marketing,
        {
            "marketing.emailOptInBrevoStatus": "doi_reminder_sent",
            "marketing.emailOptInBrevoReminderSentAt": now,
            "marketing.emailOptInBrevoNextActionAt": _as_utc(marketing.get("emailOptInBrevoExpiresAt")),
            "marketing.emailOptInBrevoReminderClaimedAt": None,
            "marketing.emailOptInBrevoLastProviderOutcome": "reminder_doi_accepted",
        },
    )


def _record_reminder_outcome(
    user_ref,
    marketing: dict[str, Any],
    *,
    now: datetime,
    outcome: str,
    next_action_at: datetime | None,
    retain_claim: bool,
) -> bool:
    return _set_state_if_current(
        user_ref,
        marketing,
        {
            "marketing.emailOptInBrevoNextActionAt": next_action_at,
            "marketing.emailOptInBrevoReminderClaimedAt": now if retain_claim else None,
            "marketing.emailOptInBrevoLastProviderOutcome": outcome,
        },
    )


def _set_terminal_state(
    user_ref,
    marketing: dict[str, Any],
    *,
    now: datetime,
    status: str,
    updates: dict[str, Any],
) -> bool:
    return _set_state_if_current(
        user_ref,
        marketing,
        {
            "marketing.emailOptInBrevoStatus": status,
            "marketing.emailOptInBrevoNextActionAt": None,
            "marketing.emailOptInBrevoReminderClaimedAt": None,
            **updates,
        },
    )


def _set_state_if_current(user_ref, marketing: dict[str, Any], updates: dict[str, Any]) -> bool:
    db = get_firestore_client()
    cycle_id = str(marketing["emailOptInBrevoCycleId"])
    original = _as_utc(marketing["emailOptInBrevoOriginalRequestedAt"])

    @firestore.transactional
    def _update(transaction):
        snapshot = user_ref.get(transaction=transaction)
        current = _marketing_data(snapshot.to_dict() or {})
        if not _matches_cycle(current, cycle_id, original):
            return False
        transaction.update(user_ref, updates)
        return True

    return bool(_update(db.transaction()))


def _matches_cycle(marketing: dict[str, Any], cycle_id: str, original: datetime | None) -> bool:
    return (
        str(marketing.get("emailOptInBrevoCycleId") or "") == cycle_id
        and _as_utc(marketing.get("emailOptInBrevoOriginalRequestedAt")) == original
    )


def _acquire_run_lease(run_id: str, now: datetime) -> bool:
    db = get_firestore_client()
    lease_ref = db.collection("scheduler_runs").document("marketing_doi_reconcile")

    @firestore.transactional
    def _acquire(transaction):
        snapshot = lease_ref.get(transaction=transaction)
        current = snapshot.to_dict() or {}
        expires_at = _as_utc(current.get("leaseExpiresAt"))
        if expires_at and expires_at > now:
            return False
        transaction.set(
            lease_ref,
            {"runId": run_id, "startedAt": now, "leaseExpiresAt": now + _LEASE_DURATION},
            merge=True,
        )
        return True

    return bool(_acquire(db.transaction()))


def _finish_run_lease(run_id: str, finished_at: datetime, result: dict[str, int | bool | str]) -> None:
    try:
        get_firestore_client().collection("scheduler_runs").document("marketing_doi_reconcile").set(
            {
                "runId": run_id,
                "finishedAt": finished_at,
                "leaseExpiresAt": finished_at,
                "lastResult": dict(result),
            },
            merge=True,
        )
    except Exception:
        logger.exception("marketing_doi_reconcile_lease_finish_failed run_id=%s", run_id)


def _empty_result(run_id: str, settings: Settings) -> dict[str, int | bool | str]:
    return {
        "run_id": run_id,
        "mode": settings.marketing_doi_reconcile_mode,
        "status": "starting",
        "brevo_snapshot_contact_count": 0,
        "firestore_candidates_scanned": 0,
        "confirmed_reconciled": 0,
        "suppressed_reconciled": 0,
        "reminders_sent": 0,
        "expired": 0,
        "skipped_not_due": 0,
        "skipped_observe_mode": 0,
        "skipped_lease_held": 0,
        "skipped_invalid": 0,
        "provider_failures": 0,
        "unknown_delivery": 0,
        "has_more_candidates": False,
    }


def _marketing_data(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("marketing")
    return value if isinstance(value, dict) else {}


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    normalized = _as_utc(value)
    if normalized is None:
        raise TypeError("Expected a datetime value.")
    return normalized
