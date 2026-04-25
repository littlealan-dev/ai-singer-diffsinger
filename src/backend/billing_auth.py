from __future__ import annotations

"""Firebase-auth helpers for billing endpoints."""

from src.backend.billing_types import AuthContext, BillingHttpError
from src.backend.firebase_app import verify_id_token_claims


def authenticate_billing_request(authorization_header: str | None) -> AuthContext:
    if not authorization_header:
        raise BillingHttpError(401, "Missing Authorization header.")
    parts = authorization_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise BillingHttpError(401, "Invalid Authorization header.")
    claims = verify_id_token_claims(parts[1])
    uid = str(claims.get("uid") or "").strip()
    if not uid:
        raise BillingHttpError(401, "Invalid Firebase token.")
    email = str(claims.get("email") or "").strip()
    return {
        "uid": uid,
        "email": email,
        "claims": claims,
    }
