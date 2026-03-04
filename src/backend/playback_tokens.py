from __future__ import annotations

"""Short-lived signed playback tokens for browser media URLs."""

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import time


class PlaybackTokenError(ValueError):
    """Raised when a playback token is missing, invalid, or expired."""


@dataclass(frozen=True)
class PlaybackTokenClaims:
    """Validated playback token claims."""

    user_id: str
    session_id: str
    file_name: str
    expires_at: int


def issue_playback_token(
    secret: str,
    *,
    user_id: str,
    session_id: str,
    file_name: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """Create a signed playback token scoped to one user/session/file tuple."""
    issued_at = int(time.time() if now is None else now)
    payload = {
        "exp": issued_at + max(1, ttl_seconds),
        "file": file_name,
        "sid": session_id,
        "uid": user_id,
    }
    payload_bytes = _canonical_payload(payload)
    signature = _sign(secret, payload_bytes)
    return f"{_b64encode(payload_bytes)}.{_b64encode(signature)}"


def verify_playback_token(
    token: str,
    secret: str,
    *,
    session_id: str,
    file_name: str,
    now: int | None = None,
) -> PlaybackTokenClaims:
    """Validate a playback token and return its claims."""
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise PlaybackTokenError("Malformed playback token.") from exc
    payload_bytes = _b64decode(payload_part)
    expected_signature = _sign(secret, payload_bytes)
    actual_signature = _b64decode(signature_part)
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise PlaybackTokenError("Invalid playback token signature.")
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise PlaybackTokenError("Invalid playback token payload.") from exc
    token_session_id = payload.get("sid")
    token_file_name = payload.get("file")
    token_user_id = payload.get("uid")
    expires_at = payload.get("exp")
    if (
        not isinstance(token_session_id, str)
        or not isinstance(token_file_name, str)
        or not isinstance(token_user_id, str)
        or not isinstance(expires_at, int)
    ):
        raise PlaybackTokenError("Playback token payload is incomplete.")
    if token_session_id != session_id or token_file_name != file_name:
        raise PlaybackTokenError("Playback token does not match this audio resource.")
    current_time = int(time.time() if now is None else now)
    if expires_at < current_time:
        raise PlaybackTokenError("Playback token expired.")
    return PlaybackTokenClaims(
        user_id=token_user_id,
        session_id=token_session_id,
        file_name=token_file_name,
        expires_at=expires_at,
    )


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(f"{data}{padding}".encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise PlaybackTokenError("Invalid playback token encoding.") from exc
