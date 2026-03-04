from __future__ import annotations

"""Waiting list service backed by Brevo double opt-in."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import os
import random

import httpx
from firebase_admin import app_check

from src.backend.config import Settings
from src.backend.secret_manager import read_secret
from src.mcp.logging_utils import get_logger


logger = get_logger(__name__)

BREVO_DOI_ENDPOINT = "https://api.brevo.com/v3/contacts/doubleOptinConfirmation"
BREVO_TIMEOUT_SECONDS = 10.0
BREVO_MAX_ATTEMPTS = 2
BREVO_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
BREVO_RETRY_BASE_DELAY_SECONDS = 0.25
BREVO_RETRY_JITTER_SECONDS = 0.1


@dataclass(frozen=True)
class WaitlistResult:
    success: bool
    message: str
    requires_confirmation: bool = True
    status_code: int = 200


def verify_app_check_token(token: str) -> bool:
    """Return True if the App Check token is valid."""
    try:
        app_check.verify_token(token)
        return True
    except Exception as exc:
        logger.warning("app_check_verification_failed error=%s", exc)
        return False


def _load_brevo_api_key(settings: Settings) -> str:
    """Fetch the Brevo API key from Secret Manager."""
    app_env = settings.app_env.lower()
    env_api_key = os.getenv("BREVO_WAITLIST_API_KEY")
    if app_env in {"dev", "development", "local", "test"} and env_api_key:
        return env_api_key
    return read_secret(
        settings,
        settings.brevo_waitlist_api_key_secret,
        settings.brevo_waitlist_api_key_secret_version,
    )


def _retry_delay_seconds(attempt: int) -> float:
    """Return a short capped delay for retryable waitlist failures."""
    return BREVO_RETRY_BASE_DELAY_SECONDS + random.uniform(0.0, BREVO_RETRY_JITTER_SECONDS * attempt)


def _dependency_failure_result() -> WaitlistResult:
    """Return a stable dependency-failure contract for waitlist calls."""
    return WaitlistResult(
        success=False,
        message="Waitlist service temporarily unavailable. Please try again later.",
        requires_confirmation=False,
        status_code=503,
    )


async def subscribe_to_waitlist(
    settings: Settings,
    *,
    email: str,
    first_name: Optional[str],
    feedback: Optional[str],
    gdpr_consent: bool,
    consent_text: str,
    source: str,
) -> WaitlistResult:
    """Submit a DOI request to Brevo."""
    if not gdpr_consent:
        return WaitlistResult(
            success=False,
            message="GDPR consent is required.",
            requires_confirmation=False,
            status_code=400,
        )

    api_key = _load_brevo_api_key(settings)
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "email": email,
        "includeListIds": [settings.brevo_waitlist_list_id],
        "templateId": settings.brevo_doi_template_id,
        "redirectionUrl": settings.brevo_doi_redirect_url,
        "attributes": {
            "FIRSTNAME": first_name or "",
            "FEEDBACK": feedback or "",
            "SIGNUP_SOURCE": source,
            "GDPR_CONSENT": True,
            "GDPR_CONSENT_TEXT": consent_text,
            "GDPR_CONSENT_DATE": datetime.now(timezone.utc).isoformat(),
        },
    }

    async with httpx.AsyncClient(timeout=BREVO_TIMEOUT_SECONDS) as client:
        for attempt in range(1, BREVO_MAX_ATTEMPTS + 1):
            try:
                response = await client.post(BREVO_DOI_ENDPOINT, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                should_retry = attempt < BREVO_MAX_ATTEMPTS
                logger.warning(
                    "brevo_transport_error attempt=%s retry=%s error=%s",
                    attempt,
                    should_retry,
                    exc,
                )
                if should_retry:
                    await asyncio.sleep(_retry_delay_seconds(attempt))
                    continue
                return _dependency_failure_result()

            if response.status_code in (200, 201, 204):
                return WaitlistResult(
                    success=True,
                    message="If this email isn't already subscribed, you'll receive a confirmation shortly.",
                    requires_confirmation=True,
                )
            if response.status_code == 400 and "already exists" in response.text.lower():
                return WaitlistResult(
                    success=True,
                    message="If this email isn't already subscribed, you'll receive a confirmation shortly.",
                    requires_confirmation=True,
                )
            if response.status_code in BREVO_RETRYABLE_STATUS_CODES and attempt < BREVO_MAX_ATTEMPTS:
                logger.warning(
                    "brevo_retryable_status attempt=%s status=%s response=%s",
                    attempt,
                    response.status_code,
                    response.text,
                )
                await asyncio.sleep(_retry_delay_seconds(attempt))
                continue
            logger.warning(
                "brevo_api_error attempt=%s status=%s response=%s",
                attempt,
                response.status_code,
                response.text,
            )
            return _dependency_failure_result()

    return _dependency_failure_result()
