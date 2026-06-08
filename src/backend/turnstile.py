from __future__ import annotations

"""Cloudflare Turnstile verification helpers."""

from dataclasses import dataclass
import os

import httpx

from src.backend.config import Settings
from src.backend.secret_manager import read_secret
from src.mcp.logging_utils import get_logger


logger = get_logger(__name__)

TURNSTILE_SITEVERIFY_ENDPOINT = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass(frozen=True)
class TurnstileVerificationResult:
    success: bool
    error_codes: list[str]


def _load_turnstile_secret(settings: Settings) -> str:
    env_secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret
    if not settings.turnstile_secret_key_secret:
        return ""
    return read_secret(
        settings,
        settings.turnstile_secret_key_secret,
        settings.turnstile_secret_key_secret_version,
    ).strip()


async def verify_turnstile_token(
    settings: Settings,
    *,
    token: str,
    remote_ip: str | None = None,
) -> TurnstileVerificationResult:
    """Verify one Turnstile response token with Cloudflare."""
    secret = _load_turnstile_secret(settings)
    if not secret:
        logger.warning("turnstile_secret_missing")
        return TurnstileVerificationResult(success=False, error_codes=["missing-secret"])

    data = {
        "secret": secret,
        "response": token,
    }
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=settings.turnstile_timeout_seconds) as client:
            response = await client.post(TURNSTILE_SITEVERIFY_ENDPOINT, data=data)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("turnstile_transport_error error=%s", exc)
        return TurnstileVerificationResult(success=False, error_codes=["transport-error"])

    if response.status_code != 200:
        logger.warning(
            "turnstile_api_error status=%s response=%s",
            response.status_code,
            response.text[:500],
        )
        return TurnstileVerificationResult(success=False, error_codes=["api-error"])

    try:
        payload = response.json()
    except ValueError:
        logger.warning("turnstile_invalid_json response=%s", response.text[:500])
        return TurnstileVerificationResult(success=False, error_codes=["invalid-json"])

    error_codes = payload.get("error-codes")
    if not isinstance(error_codes, list):
        error_codes = []
    return TurnstileVerificationResult(
        success=bool(payload.get("success")),
        error_codes=[str(code) for code in error_codes],
    )
