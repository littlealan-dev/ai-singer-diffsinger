import os
from datetime import datetime, timedelta, timezone

import pytest

from src.backend.billing_checkout import create_checkout_session
from src.backend.billing_config import get_billing_config, get_stripe_client
from src.backend.billing_migration import ensure_billing_state_for_login
from src.backend.billing_portal import create_portal_session
from src.backend.billing_refresh import apply_due_refresh, compute_next_monthly_refresh
from src.backend.billing_store import get_billing_state
from src.backend.billing_webhooks import handle_event
from src.backend.credits import get_or_create_credits
from src.backend.firebase_app import get_firestore_client

os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
os.environ["GCLOUD_PROJECT"] = "demo-project"


class _FakeSession:
    def __init__(self, session_id: str, url: str):
        self.id = session_id
        self.url = url


class _FakeCustomer:
    def __init__(self, customer_id: str):
        self.id = customer_id


class _FakeStripeClient:
    def __init__(self):
        self.created_customers = []
        self.created_checkout_sessions = []
        self.created_portal_sessions = []
        self.v1 = type("V1Api", (), {})()
        self.v1.customers = type("CustomersApi", (), {})()
        self.v1.checkout = type("CheckoutApi", (), {})()
        self.v1.checkout.sessions = type("CheckoutSessionsApi", (), {})()
        self.v1.billing_portal = type("PortalApi", (), {})()
        self.v1.billing_portal.sessions = type("PortalSessionsApi", (), {})()
        self.v1.customers.create = self._create_customer
        self.v1.checkout.sessions.create = self._create_checkout_session
        self.v1.billing_portal.sessions.create = self._create_portal_session

    def _create_customer(self, *, params):
        self.created_customers.append(params)
        return _FakeCustomer("cus_test_123")

    def _create_checkout_session(self, *, params):
        self.created_checkout_sessions.append(params)
        return _FakeSession("cs_test_123", "https://checkout.stripe.test/session")

    def _create_portal_session(self, *, params):
        self.created_portal_sessions.append(params)
        return _FakeSession("bps_test_123", "https://billing.stripe.test/session")

    def create(self, **kwargs):
        if "params" in kwargs and "line_items" in kwargs["params"]:
            self.created_checkout_sessions.append(kwargs["params"])
            return _FakeSession("cs_test_123", "https://checkout.stripe.test/session")
        if "params" in kwargs and "configuration" in kwargs["params"]:
            self.created_portal_sessions.append(kwargs["params"])
            return _FakeSession("bps_test_123", "https://billing.stripe.test/session")
        self.created_customers.append(kwargs.get("params", kwargs))
        return _FakeCustomer("cus_test_123")


@pytest.fixture(autouse=True)
def billing_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_123")
    monkeypatch.setenv("STRIPE_PRODUCT_SOLO", "prod_solo")
    monkeypatch.setenv("STRIPE_PRODUCT_CHOIR", "prod_choir")
    monkeypatch.setenv("STRIPE_PRICE_SOLO_MONTHLY", "price_solo_monthly")
    monkeypatch.setenv("STRIPE_PRICE_SOLO_ANNUAL", "price_solo_annual")
    monkeypatch.setenv("STRIPE_PRICE_CHOIR_EARLY_MONTHLY", "price_choir_early_monthly")
    monkeypatch.setenv("STRIPE_PRICE_CHOIR_EARLY_ANNUAL", "price_choir_early_annual")
    monkeypatch.setenv("STRIPE_PRICE_CHOIR_MONTHLY", "price_choir_monthly")
    monkeypatch.setenv("STRIPE_PRICE_CHOIR_ANNUAL", "price_choir_annual")
    monkeypatch.setenv("CHOIR_EARLY_SUPPORTER_ENABLED", "true")
    monkeypatch.setenv("STRIPE_CHECKOUT_SUCCESS_URL", "https://app.test/success")
    monkeypatch.setenv("STRIPE_CHECKOUT_CANCEL_URL", "https://app.test/cancel")
    monkeypatch.setenv("STRIPE_PORTAL_RETURN_URL", "https://app.test/account")
    monkeypatch.setenv("STRIPE_PORTAL_CONFIGURATION_ID", "bpc_test_123")
    get_billing_config.cache_clear()
    get_stripe_client.cache_clear()

    db = get_firestore_client()
    for collection in ["users", "credit_ledger", "stripe_events", "credit_reservations"]:
        for doc in db.collection(collection).list_documents():
            doc.delete()
    yield


def test_compute_next_monthly_refresh_preserves_anchor_time():
    anchor = datetime(2026, 1, 31, 9, 15, tzinfo=timezone.utc)
    after = datetime(2026, 2, 5, 1, 0, tzinfo=timezone.utc)
    result = compute_next_monthly_refresh(anchor, after)
    assert result == datetime(2026, 2, 28, 9, 15, tzinfo=timezone.utc)


def test_create_checkout_session_creates_customer_and_session():
    ensure_billing_state_for_login("user-1", "user1@example.com")
    fake_stripe = _FakeStripeClient()

    url = create_checkout_session(
        "user-1",
        "user1@example.com",
        "solo_monthly",
        config=get_billing_config(),
        stripe_client=fake_stripe,
    )

    assert url == "https://checkout.stripe.test/session"
    assert len(fake_stripe.created_customers) == 1
    assert len(fake_stripe.created_checkout_sessions) == 1
    billing = get_billing_state("user-1")
    assert billing["stripeCustomerId"] == "cus_test_123"
    assert billing["stripeCheckoutSessionId"] == "cs_test_123"


def test_create_portal_session_requires_existing_customer():
    ensure_billing_state_for_login("user-2", "user2@example.com")
    with pytest.raises(Exception):
        create_portal_session("user-2", config=get_billing_config(), stripe_client=_FakeStripeClient())


def test_invoice_paid_immediately_grants_monthly_plan_and_reanchors():
    uid = "user-3"
    get_or_create_credits(uid, "user3@example.com")
    event_time = int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp())

    handle_event(
        {
            "id": "evt_invoice_paid_1",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_123",
                    "customer": "cus_test_123",
                    "subscription": "sub_123",
                    "metadata": {"firebaseUserId": uid},
                    "lines": {"data": [{"price": {"id": "price_solo_monthly"}}]},
                    "status_transitions": {"paid_at": event_time},
                }
            },
        }
    )

    user = get_firestore_client().collection("users").document(uid).get().to_dict() or {}
    billing = user["billing"]
    credits = user["credits"]
    assert billing["activePlanKey"] == "solo_monthly"
    assert billing["creditRefreshAnchor"] == datetime.fromtimestamp(event_time, tz=timezone.utc)
    assert credits["balance"] == 30
    assert credits["lastGrantType"] == "grant_paid_subscription_cycle"


def test_invoice_paid_defers_when_reserved_then_scheduler_applies():
    uid = "user-4"
    get_or_create_credits(uid, "user4@example.com")
    db = get_firestore_client()
    db.collection("users").document(uid).set({"credits": {"reserved": 2}}, merge=True)
    paid_at = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)

    handle_event(
        {
            "id": "evt_invoice_paid_2",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_456",
                    "customer": "cus_test_456",
                    "subscription": "sub_456",
                    "metadata": {"firebaseUserId": uid},
                    "lines": {"data": [{"price": {"id": "price_solo_monthly"}}]},
                    "status_transitions": {"paid_at": int(paid_at.timestamp())},
                }
            },
        }
    )

    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 8
    assert not db.collection("credit_ledger").document("grant_invoice_in_456").get().exists

    db.collection("users").document(uid).set({"credits": {"reserved": 0}}, merge=True)
    outcome = apply_due_refresh(uid, now=paid_at + timedelta(minutes=1))
    assert outcome == "applied"

    refreshed = db.collection("users").document(uid).get().to_dict() or {}
    assert refreshed["credits"]["balance"] == 30
    assert refreshed["credits"]["lastGrantType"] == "grant_paid_subscription_cycle"


def test_annual_invoice_paid_sets_paid_anchor_and_next_refresh():
    uid = "user-5"
    get_or_create_credits(uid, "user5@example.com")
    paid_at = datetime(2026, 4, 25, 14, 0, tzinfo=timezone.utc)

    handle_event(
        {
            "id": "evt_invoice_paid_3",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_789",
                    "customer": "cus_test_789",
                    "subscription": "sub_789",
                    "metadata": {"firebaseUserId": uid},
                    "lines": {"data": [{"price": {"id": "price_solo_annual"}}]},
                    "status_transitions": {"paid_at": int(paid_at.timestamp())},
                }
            },
        }
    )

    user = get_firestore_client().collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "solo_annual"
    assert user["billing"]["creditRefreshAnchor"] == paid_at
    assert user["billing"]["nextCreditRefreshAt"] == compute_next_monthly_refresh(paid_at, paid_at)
    assert user["credits"]["balance"] == 30
