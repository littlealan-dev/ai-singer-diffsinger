from __future__ import annotations

"""Stripe Billing Portal session creation flow."""

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_store import get_billing_state
from src.backend.billing_types import BillingHttpError


# The app pages a portal session may return to: the start screen or one Song. Anything else
# falls back to STRIPE_PORTAL_RETURN_URL's own page, so the link can't send users elsewhere.
_RETURN_PATH = re.compile(r"/app(/song/[A-Za-z0-9_-]{1,64})?/?")


def portal_return_url(config: BillingConfig, return_path: str | None = None, **query: str) -> str:
    """STRIPE_PORTAL_RETURN_URL, on the page the user left from, with extra query values."""
    parts = urlsplit(config.portal_return_url)
    path = return_path if return_path and _RETURN_PATH.fullmatch(return_path) else parts.path
    values = parse_qsl(parts.query, keep_blank_values=True) + list(query.items())
    return urlunsplit(parts._replace(path=path, query=urlencode(values)))


def create_portal_session(
    uid: str,
    *,
    return_path: str | None = None,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> str:
    billing = get_billing_state(uid)
    stripe_customer_id = billing.get("stripeCustomerId")
    if not stripe_customer_id:
        raise BillingHttpError(409, "Stripe customer is not set up for this account.")
    billing_config = config or get_billing_config()
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    session = client.billing_portal.sessions.create(
        params={
            "customer": stripe_customer_id,
            "return_url": portal_return_url(billing_config, return_path),
            "configuration": billing_config.portal_configuration_id,
        },
    )
    return str(session.url)
