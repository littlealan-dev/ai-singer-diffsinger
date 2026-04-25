from __future__ import annotations

import json
import os
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
import stripe

from src.backend.billing_config import get_billing_config, get_stripe_client, get_stripe_v1_client
from src.backend.credits import get_or_create_credits
from src.backend.firebase_app import get_firestore_client
from src.backend.main import create_app


def _load_local_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env_file(".env")
_load_local_env_file("env/local.env")
_load_local_env_file("env/dev.env")


def _missing_realtime_env() -> list[str]:
    required = [
        "STRIPE_SECRET_KEY",
        "STRIPE_PORTAL_CONFIGURATION_ID",
        "STRIPE_PRODUCT_SOLO",
        "STRIPE_PRODUCT_CHOIR",
        "STRIPE_PRICE_SOLO_MONTHLY",
        "STRIPE_PRICE_SOLO_ANNUAL",
        "STRIPE_PRICE_CHOIR_EARLY_MONTHLY",
        "STRIPE_PRICE_CHOIR_EARLY_ANNUAL",
        "STRIPE_PRICE_CHOIR_MONTHLY",
        "STRIPE_PRICE_CHOIR_ANNUAL",
        "STRIPE_CHECKOUT_SUCCESS_URL",
        "STRIPE_CHECKOUT_CANCEL_URL",
        "STRIPE_PORTAL_RETURN_URL",
    ]
    return [name for name in required if not os.getenv(name)]


pytestmark = pytest.mark.skipif(
    bool(_missing_realtime_env()),
    reason=f"Missing Stripe integration env vars: {', '.join(_missing_realtime_env())}",
)


@pytest.fixture(autouse=True)
def cleanup_firestore(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BACKEND_AUTH_DISABLED", "false")
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "false")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("BACKEND_DATA_DIR", "tests/output/billing_stripe_api")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", os.getenv("FIRESTORE_EMULATOR_HOST", "localhost:8080"))
    monkeypatch.setenv("GCLOUD_PROJECT", os.getenv("GCLOUD_PROJECT", "demo-project"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", os.getenv("STRIPE_SECRET_KEY", "sk_test_dummy"))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_backend_integration")
    for name in [
        "STRIPE_PRODUCT_SOLO",
        "STRIPE_PRODUCT_CHOIR",
        "STRIPE_PRICE_SOLO_MONTHLY",
        "STRIPE_PRICE_SOLO_ANNUAL",
        "STRIPE_PRICE_CHOIR_EARLY_MONTHLY",
        "STRIPE_PRICE_CHOIR_EARLY_ANNUAL",
        "STRIPE_PRICE_CHOIR_MONTHLY",
        "STRIPE_PRICE_CHOIR_ANNUAL",
        "CHOIR_EARLY_SUPPORTER_ENABLED",
        "STRIPE_CHECKOUT_SUCCESS_URL",
        "STRIPE_CHECKOUT_CANCEL_URL",
        "STRIPE_PORTAL_RETURN_URL",
        "STRIPE_PORTAL_CONFIGURATION_ID",
        "STRIPE_API_VERSION",
    ]:
        value = os.getenv(name)
        if value:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.start", lambda self: None)
    monkeypatch.setattr("src.backend.mcp_client.McpRouter.stop", lambda self: None)
    monkeypatch.setattr("src.backend.main.verify_id_token_claims", lambda token: {"uid": "stripe-test-user", "email": "stripe-test@example.com"})
    monkeypatch.setattr("src.backend.main.verify_id_token", lambda token: "stripe-test-user")
    get_billing_config.cache_clear()
    get_stripe_client.cache_clear()
    db = get_firestore_client()
    for collection in ["users", "credit_ledger", "credit_reservations", "stripe_events"]:
        for doc in db.collection(collection).list_documents():
            doc.delete()
    yield


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _signed_webhook_headers(payload: bytes, secret: str) -> dict[str, str]:
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
    signature = stripe.WebhookSignature._compute_signature(signed_payload, secret)
    return {
        "Stripe-Signature": f"t={timestamp},v1={signature}",
        "Content-Type": "application/json",
    }


def _post_webhook(client: TestClient, event: dict, *, secret: str = "whsec_test_backend_integration"):
    payload_event = {"object": "event", **event}
    payload = json.dumps(payload_event, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return client.post(
        "/billing/webhook",
        content=payload,
        headers=_signed_webhook_headers(payload, secret),
    )


def test_checkout_and_portal_with_real_stripe_api():
    app = create_app()
    with TestClient(app) as client:
        checkout_response = client.post(
            "/billing/checkout-session",
            headers=_auth_headers(),
            json={"planKey": "solo_monthly"},
        )
        assert checkout_response.status_code == 200, checkout_response.text
        checkout_payload = checkout_response.json()
        assert checkout_payload["url"].startswith("https://checkout.stripe.com/")

        db = get_firestore_client()
        user = db.collection("users").document("stripe-test-user").get().to_dict() or {}
        billing = user["billing"]
        assert str(billing["stripeCustomerId"]).startswith("cus_")
        assert str(billing["stripeCheckoutSessionId"]).startswith("cs_")

        portal_response = client.post(
            "/billing/portal-session",
            headers=_auth_headers(),
        )
        assert portal_response.status_code == 200, portal_response.text
        portal_payload = portal_response.json()
        assert portal_payload["url"].startswith("https://billing.stripe.com/")


def test_webhook_checkout_session_completed_route_persists_refs():
    app = create_app()
    with TestClient(app) as client:
        response = _post_webhook(
            client,
            {
                "id": "evt_checkout_completed_backend",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "cs_test_backend",
                        "object": "checkout.session",
                        "client_reference_id": "stripe-test-user",
                        "customer": "cus_backend_123",
                        "subscription": "sub_backend_123",
                        "metadata": {
                            "firebaseUserId": "stripe-test-user",
                            "planKey": "solo_monthly",
                        },
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        user = get_firestore_client().collection("users").document("stripe-test-user").get().to_dict() or {}
        assert user["billing"]["stripeCustomerId"] == "cus_backend_123"
        assert user["billing"]["stripeSubscriptionId"] == "sub_backend_123"
        assert user["billing"]["stripeCheckoutSessionId"] == "cs_test_backend"


def test_webhook_invoice_paid_route_grants_monthly_plan():
    get_or_create_credits("stripe-test-user", "stripe-test@example.com")
    paid_at = int(datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc).timestamp())
    app = create_app()
    with TestClient(app) as client:
        response = _post_webhook(
            client,
            {
                "id": "evt_invoice_paid_backend",
                "type": "invoice.paid",
                "data": {
                    "object": {
                        "id": "in_backend_123",
                        "object": "invoice",
                        "customer": "cus_backend_123",
                        "subscription": "sub_backend_123",
                        "metadata": {"firebaseUserId": "stripe-test-user"},
                        "lines": {"data": [{"price": {"id": os.environ["STRIPE_PRICE_SOLO_MONTHLY"]}}]},
                        "status_transitions": {"paid_at": paid_at},
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        user = get_firestore_client().collection("users").document("stripe-test-user").get().to_dict() or {}
        assert user["billing"]["activePlanKey"] == "solo_monthly"
        assert user["credits"]["balance"] == 30
        assert user["credits"]["lastGrantType"] == "grant_paid_subscription_cycle"
        assert user["credits"]["lastGrantInvoiceId"] == "in_backend_123"


def test_webhook_invoice_payment_failed_route_updates_billing():
    get_or_create_credits("stripe-test-user", "stripe-test@example.com")
    app = create_app()
    with TestClient(app) as client:
        response = _post_webhook(
            client,
            {
                "id": "evt_invoice_failed_backend",
                "type": "invoice.payment_failed",
                "data": {
                    "object": {
                        "id": "in_failed_123",
                        "object": "invoice",
                        "customer": "cus_backend_123",
                        "metadata": {"firebaseUserId": "stripe-test-user"},
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        user = get_firestore_client().collection("users").document("stripe-test-user").get().to_dict() or {}
        assert user["billing"]["latestInvoiceId"] == "in_failed_123"
        assert user["billing"]["latestInvoicePaymentFailedAt"] is not None


def test_webhook_subscription_updated_route_updates_mirror():
    get_or_create_credits("stripe-test-user", "stripe-test@example.com")
    current_period_start = int(datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc).timestamp())
    current_period_end = int(datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc).timestamp())
    app = create_app()
    with TestClient(app) as client:
        response = _post_webhook(
            client,
            {
                "id": "evt_subscription_updated_backend",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_backend_456",
                        "object": "subscription",
                        "customer": "cus_backend_123",
                        "status": "active",
                        "cancel_at_period_end": True,
                        "canceled_at": None,
                        "current_period_start": current_period_start,
                        "current_period_end": current_period_end,
                        "metadata": {"firebaseUserId": "stripe-test-user"},
                        "items": {"data": [{"price": {"id": os.environ["STRIPE_PRICE_SOLO_MONTHLY"]}}]},
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        user = get_firestore_client().collection("users").document("stripe-test-user").get().to_dict() or {}
        billing = user["billing"]
        assert billing["activePlanKey"] == "solo_monthly"
        assert billing["stripeSubscriptionStatus"] == "active"
        assert billing["stripeSubscriptionId"] == "sub_backend_456"
        assert billing["cancelAtPeriodEnd"] is True


def test_webhook_subscription_deleted_route_reverts_to_free_and_preserves_anchor():
    db = get_firestore_client()
    anchor = datetime(2026, 4, 15, 9, 0, tzinfo=timezone.utc)
    next_refresh = anchor + timedelta(days=30)
    db.collection("users").document("stripe-test-user").set(
        {
            "billing": {
                "activePlanKey": "solo_monthly",
                "stripeSubscriptionStatus": "active",
                "family": "solo",
                "billingInterval": "month",
                "stripeCustomerId": "cus_backend_123",
                "stripeSubscriptionId": "sub_backend_789",
                "creditRefreshAnchor": anchor,
                "lastCreditRefreshAt": anchor,
                "nextCreditRefreshAt": next_refresh,
            },
            "credits": {
                "balance": 30,
                "reserved": 0,
                "overdrafted": False,
                "expiresAt": None,
                "monthlyAllowance": 30,
            },
        },
        merge=True,
    )
    app = create_app()
    with TestClient(app) as client:
        response = _post_webhook(
            client,
            {
                "id": "evt_subscription_deleted_backend",
                "type": "customer.subscription.deleted",
                "data": {
                    "object": {
                        "id": "sub_backend_789",
                        "object": "subscription",
                        "customer": "cus_backend_123",
                        "status": "canceled",
                        "metadata": {"firebaseUserId": "stripe-test-user"},
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        user = db.collection("users").document("stripe-test-user").get().to_dict() or {}
        billing = user["billing"]
        assert billing["activePlanKey"] == "free"
        assert billing["billingInterval"] == "none"
        assert billing["stripeSubscriptionId"] is None
        assert billing["creditRefreshAnchor"] == anchor
