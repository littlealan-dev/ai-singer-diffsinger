import os
from datetime import datetime, timedelta, timezone

import pytest

from src.backend.billing_checkout import create_checkout_session
from src.backend.billing_checkout_sync import sync_checkout_session
from src.backend.billing_config import get_billing_config, get_stripe_client
from src.backend.billing_migration import ensure_billing_state_for_login
from src.backend.billing_portal import create_portal_session
from src.backend.billing_refresh import apply_due_refresh, compute_next_monthly_refresh, run_credit_refresh
from src.backend.billing_store import get_billing_state
from src.backend.billing_subscription_sync import sync_current_subscription
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
        self.checkout_session_payload = None
        self.subscription_payload = None
        self.subscription_list_payload = []
        self.v1 = type("V1Api", (), {})()
        self.v1.customers = type("CustomersApi", (), {})()
        self.v1.checkout = type("CheckoutApi", (), {})()
        self.v1.checkout.sessions = type("CheckoutSessionsApi", (), {})()
        self.v1.billing_portal = type("PortalApi", (), {})()
        self.v1.billing_portal.sessions = type("PortalSessionsApi", (), {})()
        self.v1.subscriptions = type("SubscriptionsApi", (), {})()
        self.v1.customers.create = self._create_customer
        self.v1.checkout.sessions.create = self._create_checkout_session
        self.v1.checkout.sessions.retrieve = self._retrieve_checkout_session
        self.v1.billing_portal.sessions.create = self._create_portal_session
        self.v1.subscriptions.retrieve = self._retrieve_subscription
        self.v1.subscriptions.list = self._list_subscriptions

    def _create_customer(self, *, params):
        self.created_customers.append(params)
        return _FakeCustomer("cus_test_123")

    def _create_checkout_session(self, *, params):
        self.created_checkout_sessions.append(params)
        return _FakeSession("cs_test_123", "https://checkout.stripe.test/session")

    def _create_portal_session(self, *, params):
        self.created_portal_sessions.append(params)
        return _FakeSession("bps_test_123", "https://billing.stripe.test/session")

    def _retrieve_checkout_session(self, session_id, *, params=None):
        assert session_id
        return self.checkout_session_payload

    def _retrieve_subscription(self, subscription_id, *, params=None):
        assert subscription_id
        return self.subscription_payload

    def _list_subscriptions(self, *, params=None):
        return {"data": self.subscription_list_payload}

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


def test_sync_checkout_session_updates_paid_state_and_grants_credits():
    uid = "user-checkout-sync"
    get_or_create_credits(uid, "sync@example.com")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {"billing": {"stripeCustomerId": "cus_test_123", "stripeCheckoutSessionId": "cs_test_123"}},
        merge=True,
    )
    fake_stripe = _FakeStripeClient()
    fake_stripe.checkout_session_payload = {
        "id": "cs_test_123",
        "mode": "subscription",
        "status": "complete",
        "payment_status": "paid",
        "customer": "cus_test_123",
        "client_reference_id": uid,
        "metadata": {"firebaseUserId": uid},
        "subscription": {
            "id": "sub_test_123",
            "status": "active",
            "billing_cycle_anchor": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "current_period_start": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "current_period_end": int(datetime(2027, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": "price_solo_annual"}}]},
            "latest_invoice": {
                "id": "in_checkout_sync_123",
                "created": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
                "status_transitions": {
                    "paid_at": int(datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc).timestamp())
                },
            },
        },
    }

    result = sync_checkout_session(
        uid,
        "cs_test_123",
        config=get_billing_config(),
        stripe_client=fake_stripe,
    )

    assert result["synced"] is True
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "solo_annual"
    assert user["billing"]["stripeSubscriptionId"] == "sub_test_123"
    assert user["billing"]["latestCheckoutSessionStatus"] == "complete"
    assert user["billing"]["latestCheckoutPaymentStatus"] == "paid"
    assert user["billing"]["latestInvoiceStatus"] == "paid"
    assert user["billing"]["creditRefreshAnchor"] == datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["nextCreditRefreshAt"] == datetime(2027, 4, 25, 12, 0, tzinfo=timezone.utc)
    assert user["credits"]["balance"] == 30
    assert user["credits"]["lastGrantInvoiceId"] == "in_checkout_sync_123"
    assert db.collection("credit_ledger").document("grant_invoice_in_checkout_sync_123").get().exists


def test_sync_checkout_session_no_payment_required_is_noop_for_v1():
    uid = "user-checkout-no-payment"
    get_or_create_credits(uid, "no-payment@example.com")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {"billing": {"stripeCustomerId": "cus_test_123", "stripeCheckoutSessionId": "cs_no_payment"}},
        merge=True,
    )
    fake_stripe = _FakeStripeClient()
    fake_stripe.checkout_session_payload = {
        "id": "cs_no_payment",
        "mode": "subscription",
        "status": "complete",
        "payment_status": "no_payment_required",
        "customer": "cus_test_123",
        "client_reference_id": uid,
        "metadata": {"firebaseUserId": uid},
    }

    result = sync_checkout_session(
        uid,
        "cs_no_payment",
        config=get_billing_config(),
        stripe_client=fake_stripe,
    )

    assert result["synced"] is False
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "free"
    assert user["billing"]["latestCheckoutSessionStatus"] == "complete"
    assert user["billing"]["latestCheckoutPaymentStatus"] == "no_payment_required"
    assert user["credits"]["balance"] == 8


def test_sync_checkout_session_rejects_other_user_session():
    uid = "user-checkout-owner"
    get_or_create_credits(uid, "owner@example.com")
    fake_stripe = _FakeStripeClient()
    fake_stripe.checkout_session_payload = {
        "id": "cs_wrong_user",
        "mode": "subscription",
        "status": "complete",
        "payment_status": "paid",
        "client_reference_id": "other-user",
        "metadata": {"firebaseUserId": "other-user"},
        "subscription": "sub_test_123",
    }

    with pytest.raises(Exception):
        sync_checkout_session(
            uid,
            "cs_wrong_user",
            config=get_billing_config(),
            stripe_client=fake_stripe,
        )


def test_sync_checkout_session_rejects_metadata_only_session_without_stored_link():
    uid = "user-checkout-metadata-only"
    get_or_create_credits(uid, "metadata-only@example.com")
    fake_stripe = _FakeStripeClient()
    fake_stripe.checkout_session_payload = {
        "id": "cs_metadata_only",
        "mode": "subscription",
        "status": "complete",
        "payment_status": "paid",
        "customer": "cus_unlinked",
        "client_reference_id": uid,
        "metadata": {"firebaseUserId": uid},
        "subscription": {
            "id": "sub_metadata_only",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_solo_monthly"}}]},
            "latest_invoice": {
                "id": "in_metadata_only",
                "created": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            },
        },
    }

    with pytest.raises(Exception):
        sync_checkout_session(
            uid,
            "cs_metadata_only",
            config=get_billing_config(),
            stripe_client=fake_stripe,
        )


def test_create_portal_session_requires_existing_customer():
    ensure_billing_state_for_login("user-2", "user2@example.com")
    with pytest.raises(Exception):
        create_portal_session("user-2", config=get_billing_config(), stripe_client=_FakeStripeClient())


def test_subscription_sync_updates_cancel_at_period_end_from_portal():
    uid = "user-portal-cancel-later"
    get_or_create_credits(uid, "portal@example.com")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": "cus_portal_123",
                "stripeSubscriptionId": "sub_portal_123",
                "activePlanKey": "solo_monthly",
                "stripeSubscriptionStatus": "active",
                "family": "solo",
                "billingInterval": "month",
            }
        },
        merge=True,
    )
    fake_stripe = _FakeStripeClient()
    fake_stripe.subscription_list_payload = [
        {
            "id": "sub_portal_123",
            "status": "active",
            "billing_cycle_anchor": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "created": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "current_period_start": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "current_period_end": int(datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "cancel_at_period_end": True,
            "canceled_at": int(datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc).timestamp()),
            "items": {"data": [{"price": {"id": "price_solo_monthly"}}]},
        }
    ]

    result = sync_current_subscription(uid, config=get_billing_config(), stripe_client=fake_stripe)

    assert result["activePlanKey"] == "solo_monthly"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "solo_monthly"
    assert user["billing"]["cancelAtPeriodEnd"] is True
    assert user["billing"]["creditRefreshAnchor"] == datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["nextCreditRefreshAt"] == datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["stripeSubscriptionStatus"] == "active"


def test_subscription_sync_treats_cancel_at_as_scheduled_cancel():
    uid = "user-portal-cancel-at"
    get_or_create_credits(uid, "portal-cancel-at@example.com")
    db = get_firestore_client()
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": "cus_portal_cancel_at",
                "stripeSubscriptionId": "sub_portal_cancel_at",
                "activePlanKey": "solo_monthly",
                "stripeSubscriptionStatus": "active",
                "family": "solo",
                "billingInterval": "month",
            }
        },
        merge=True,
    )
    fake_stripe = _FakeStripeClient()
    period_end = int(datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc).timestamp())
    fake_stripe.subscription_list_payload = [
        {
            "id": "sub_portal_cancel_at",
            "status": "active",
            "billing_cycle_anchor": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "created": int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()),
            "cancel_at": period_end,
            "cancel_at_period_end": False,
            "canceled_at": None,
            "items": {
                "data": [
                    {
                        "current_period_start": int(
                            datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp()
                        ),
                        "current_period_end": period_end,
                        "price": {"id": "price_solo_monthly"},
                    }
                ]
            },
        }
    ]

    result = sync_current_subscription(uid, config=get_billing_config(), stripe_client=fake_stripe)

    assert result["activePlanKey"] == "solo_monthly"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "solo_monthly"
    assert user["billing"]["cancelAtPeriodEnd"] is True
    assert user["billing"]["creditRefreshAnchor"] == datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["currentPeriodEnd"] == datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["nextCreditRefreshAt"] == datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert user["billing"]["stripeSubscriptionStatus"] == "active"


def test_subscription_sync_reverts_to_free_after_immediate_portal_cancel():
    uid = "user-portal-cancel-now"
    get_or_create_credits(uid, "portal-now@example.com")
    db = get_firestore_client()
    anchor = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": "cus_portal_now",
                "stripeSubscriptionId": "sub_portal_now",
                "activePlanKey": "solo_monthly",
                "stripeSubscriptionStatus": "active",
                "family": "solo",
                "billingInterval": "month",
                "creditRefreshAnchor": anchor,
            }
        },
        merge=True,
    )
    fake_stripe = _FakeStripeClient()
    fake_stripe.subscription_list_payload = [
        {
            "id": "sub_portal_now",
            "status": "canceled",
            "created": int(anchor.timestamp()),
            "items": {"data": [{"price": {"id": "price_solo_monthly"}}]},
        }
    ]

    result = sync_current_subscription(uid, config=get_billing_config(), stripe_client=fake_stripe)

    assert result["activePlanKey"] == "free"
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "free"
    assert user["billing"]["stripeSubscriptionId"] is None
    assert user["billing"]["creditRefreshAnchor"] == anchor


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
                    "status": "paid",
                    "payment_intent": {"status": "succeeded"},
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
    assert billing["latestInvoiceStatus"] == "paid"
    assert billing["latestPaymentIntentStatus"] == "succeeded"
    assert credits["balance"] == 30
    assert credits["lastGrantType"] == "grant_paid_subscription_cycle"


def test_duplicate_invoice_paid_event_does_not_double_grant():
    uid = "user-duplicate-invoice-paid"
    get_or_create_credits(uid, "duplicate-invoice@example.com")
    db = get_firestore_client()
    event_time = int(datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc).timestamp())
    invoice_event = {
        "id": "evt_invoice_paid_duplicate",
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_duplicate_123",
                "customer": "cus_duplicate_123",
                "subscription": "sub_duplicate_123",
                "metadata": {"firebaseUserId": uid},
                "lines": {"data": [{"price": {"id": "price_solo_monthly"}}]},
                "status": "paid",
                "payment_intent": {"status": "succeeded"},
                "status_transitions": {"paid_at": event_time},
            }
        },
    }

    handle_event(invoice_event)
    db.collection("users").document(uid).set({"credits": {"balance": 11}}, merge=True)
    handle_event(invoice_event)

    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["billing"]["activePlanKey"] == "solo_monthly"
    assert user["billing"]["latestInvoiceId"] == "in_duplicate_123"
    assert user["credits"]["balance"] == 11
    assert user["credits"]["lastGrantInvoiceId"] == "in_duplicate_123"
    assert db.collection("credit_ledger").document("grant_invoice_in_duplicate_123").get().exists
    event_audit = db.collection("stripe_events").document("evt_invoice_paid_duplicate").get().to_dict() or {}
    assert event_audit["processed"] is True


def test_invoice_payment_failed_records_payment_status_fields_without_grant():
    uid = "user-payment-failed"
    get_or_create_credits(uid, "failed@example.com")

    handle_event(
        {
            "id": "evt_invoice_failed_1",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_failed_123",
                    "customer": "cus_failed_123",
                    "subscription": "sub_failed_123",
                    "metadata": {"firebaseUserId": uid},
                    "status": "open",
                    "payment_intent": {
                        "status": "requires_payment_method",
                        "last_payment_error": {
                            "code": "card_declined",
                            "decline_code": "generic_decline",
                            "message": "Your card was declined.",
                        },
                    },
                }
            },
        }
    )

    user = get_firestore_client().collection("users").document(uid).get().to_dict() or {}
    billing = user["billing"]
    credits = user["credits"]
    assert billing["latestInvoiceId"] == "in_failed_123"
    assert billing["latestInvoiceStatus"] == "open"
    assert billing["latestPaymentIntentStatus"] == "requires_payment_method"
    assert billing["latestPaymentFailureCode"] == "card_declined"
    assert billing["latestPaymentFailureMessage"] == "Your card was declined."
    assert credits["balance"] == 8


def test_checkout_sync_and_invoice_webhook_race_does_not_double_grant():
    db = get_firestore_client()
    paid_at = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    event_time = int(paid_at.timestamp())

    def _prepare_user(uid: str, email: str, customer_id: str, session_id: str) -> None:
        get_or_create_credits(uid, email)
        db.collection("users").document(uid).set(
            {"billing": {"stripeCustomerId": customer_id, "stripeCheckoutSessionId": session_id}},
            merge=True,
        )

    def _invoice_event(event_id: str, uid: str, customer_id: str, subscription_id: str, invoice_id: str) -> dict:
        return {
            "id": event_id,
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": invoice_id,
                    "customer": customer_id,
                    "subscription": subscription_id,
                    "metadata": {"firebaseUserId": uid},
                    "lines": {"data": [{"price": {"id": "price_solo_monthly"}}]},
                    "status": "paid",
                    "payment_intent": {"status": "succeeded"},
                    "status_transitions": {"paid_at": event_time},
                }
            },
        }

    def _checkout_client(uid: str, customer_id: str, session_id: str, subscription_id: str, invoice_id: str):
        fake_stripe = _FakeStripeClient()
        fake_stripe.checkout_session_payload = {
            "id": session_id,
            "mode": "subscription",
            "status": "complete",
            "payment_status": "paid",
            "customer": customer_id,
            "client_reference_id": uid,
            "metadata": {"firebaseUserId": uid},
            "subscription": {
                "id": subscription_id,
                "status": "active",
                "billing_cycle_anchor": event_time,
                "current_period_start": event_time,
                "current_period_end": int((paid_at + timedelta(days=31)).timestamp()),
                "cancel_at_period_end": False,
                "items": {"data": [{"price": {"id": "price_solo_monthly"}}]},
                "latest_invoice": {
                    "id": invoice_id,
                    "created": event_time,
                    "status": "paid",
                    "payment_intent": {"status": "succeeded"},
                    "status_transitions": {"paid_at": event_time},
                },
            },
        }
        return fake_stripe

    uid_webhook_first = "user-race-webhook-first"
    _prepare_user(uid_webhook_first, "race-webhook@example.com", "cus_race_webhook", "cs_race_webhook")
    handle_event(
        _invoice_event(
            "evt_race_webhook_first_invoice",
            uid_webhook_first,
            "cus_race_webhook",
            "sub_race_webhook",
            "in_race_webhook_first",
        )
    )
    db.collection("users").document(uid_webhook_first).set({"credits": {"balance": 7}}, merge=True)
    sync_checkout_session(
        uid_webhook_first,
        "cs_race_webhook",
        config=get_billing_config(),
        stripe_client=_checkout_client(
            uid_webhook_first,
            "cus_race_webhook",
            "cs_race_webhook",
            "sub_race_webhook",
            "in_race_webhook_first",
        ),
    )
    webhook_first_user = db.collection("users").document(uid_webhook_first).get().to_dict() or {}
    assert webhook_first_user["credits"]["balance"] == 7
    assert webhook_first_user["credits"]["lastGrantInvoiceId"] == "in_race_webhook_first"

    uid_checkout_first = "user-race-checkout-first"
    _prepare_user(uid_checkout_first, "race-checkout@example.com", "cus_race_checkout", "cs_race_checkout")
    sync_checkout_session(
        uid_checkout_first,
        "cs_race_checkout",
        config=get_billing_config(),
        stripe_client=_checkout_client(
            uid_checkout_first,
            "cus_race_checkout",
            "cs_race_checkout",
            "sub_race_checkout",
            "in_race_checkout_first",
        ),
    )
    db.collection("users").document(uid_checkout_first).set({"credits": {"balance": 7}}, merge=True)
    handle_event(
        _invoice_event(
            "evt_race_checkout_first_invoice",
            uid_checkout_first,
            "cus_race_checkout",
            "sub_race_checkout",
            "in_race_checkout_first",
        )
    )
    checkout_first_user = db.collection("users").document(uid_checkout_first).get().to_dict() or {}
    assert checkout_first_user["credits"]["balance"] == 7
    assert checkout_first_user["credits"]["lastGrantInvoiceId"] == "in_race_checkout_first"


def test_dispute_created_records_billing_status_without_revoking_credits():
    uid = "user-dispute-created"
    get_or_create_credits(uid, "dispute@example.com")
    db = get_firestore_client()
    paid_at = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": "cus_dispute_123",
                "activePlanKey": "solo_monthly",
                "family": "solo",
                "billingInterval": "month",
                "stripeSubscriptionStatus": "active",
                "latestInvoiceId": "in_dispute_paid",
                "latestInvoicePaidAt": paid_at,
            },
            "credits": {
                "balance": 17,
                "monthlyAllowance": 30,
                "reserved": 0,
                "lastGrantInvoiceId": "in_dispute_paid",
            },
        },
        merge=True,
    )

    dispute_created_at = int(datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc).timestamp())
    handle_event(
        {
            "id": "evt_dispute_created_1",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "du_test_123",
                    "object": "dispute",
                    "amount": 699,
                    "currency": "usd",
                    "charge": {"id": "ch_dispute_123", "customer": "cus_dispute_123"},
                    "payment_intent": {"id": "pi_dispute_123", "customer": "cus_dispute_123"},
                    "created": dispute_created_at,
                    "reason": "fraudulent",
                    "status": "needs_response",
                }
            },
        }
    )

    user = db.collection("users").document(uid).get().to_dict() or {}
    billing = user["billing"]
    credits = user["credits"]
    assert billing["activePlanKey"] == "solo_monthly"
    assert billing["stripeSubscriptionStatus"] == "active"
    assert billing["latestDisputeId"] == "du_test_123"
    assert billing["latestDisputeStatus"] == "needs_response"
    assert billing["latestDisputeReason"] == "fraudulent"
    assert billing["latestDisputeAmount"] == 699
    assert billing["latestDisputeCurrency"] == "usd"
    assert billing["latestDisputeChargeId"] == "ch_dispute_123"
    assert billing["latestDisputePaymentIntentId"] == "pi_dispute_123"
    assert billing["latestDisputeEventType"] == "charge.dispute.created"
    assert billing["latestDisputeCreatedAt"] == datetime.fromtimestamp(dispute_created_at, tz=timezone.utc)
    assert credits["balance"] == 17
    assert credits["monthlyAllowance"] == 30
    assert credits["lastGrantInvoiceId"] == "in_dispute_paid"
    event_audit = db.collection("stripe_events").document("evt_dispute_created_1").get().to_dict() or {}
    assert event_audit["processed"] is True
    assert event_audit["userId"] == uid
    assert event_audit["relatedStripeCustomerId"] == "cus_dispute_123"


def test_refund_created_records_billing_status_without_changing_credits():
    uid = "user-refund-created"
    get_or_create_credits(uid, "refund@example.com")
    db = get_firestore_client()
    paid_at = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "stripeCustomerId": "cus_refund_123",
                "activePlanKey": "solo_monthly",
                "family": "solo",
                "billingInterval": "month",
                "stripeSubscriptionStatus": "active",
                "latestInvoiceId": "in_refund_paid",
                "latestInvoicePaidAt": paid_at,
            },
            "credits": {
                "balance": 13,
                "monthlyAllowance": 30,
                "reserved": 0,
                "lastGrantInvoiceId": "in_refund_paid",
            },
        },
        merge=True,
    )

    refund_created_at = int(datetime(2026, 5, 1, 11, 15, tzinfo=timezone.utc).timestamp())
    handle_event(
        {
            "id": "evt_refund_created_1",
            "type": "refund.created",
            "data": {
                "object": {
                    "id": "re_test_123",
                    "object": "refund",
                    "amount": 350,
                    "currency": "usd",
                    "charge": {"id": "ch_refund_123", "customer": "cus_refund_123"},
                    "payment_intent": {"id": "pi_refund_123", "customer": "cus_refund_123"},
                    "created": refund_created_at,
                    "reason": "requested_by_customer",
                    "status": "succeeded",
                }
            },
        }
    )

    user = db.collection("users").document(uid).get().to_dict() or {}
    billing = user["billing"]
    credits = user["credits"]
    assert billing["activePlanKey"] == "solo_monthly"
    assert billing["stripeSubscriptionStatus"] == "active"
    assert billing["latestRefundId"] == "re_test_123"
    assert billing["latestRefundStatus"] == "succeeded"
    assert billing["latestRefundReason"] == "requested_by_customer"
    assert billing["latestRefundAmount"] == 350
    assert billing["latestRefundCurrency"] == "usd"
    assert billing["latestRefundChargeId"] == "ch_refund_123"
    assert billing["latestRefundPaymentIntentId"] == "pi_refund_123"
    assert billing["latestRefundEventType"] == "refund.created"
    assert billing["latestRefundCreatedAt"] == datetime.fromtimestamp(refund_created_at, tz=timezone.utc)
    assert credits["balance"] == 13
    assert credits["monthlyAllowance"] == 30
    assert credits["lastGrantInvoiceId"] == "in_refund_paid"
    event_audit = db.collection("stripe_events").document("evt_refund_created_1").get().to_dict() or {}
    assert event_audit["processed"] is True
    assert event_audit["userId"] == uid
    assert event_audit["relatedStripeCustomerId"] == "cus_refund_123"


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
    assert refreshed["billing"]["refreshScheduler"]["lastStatus"] == "applied"


def test_scheduler_limits_due_users_and_records_reserved_status():
    db = get_firestore_client()
    now = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    for index in range(2):
        uid = f"user-refresh-limit-{index}"
        get_or_create_credits(uid, f"refresh-limit-{index}@example.com")
        db.collection("users").document(uid).set(
            {
                "billing": {
                    "activePlanKey": "free",
                    "billingInterval": "none",
                    "creditRefreshAnchor": now - timedelta(days=31),
                    "nextCreditRefreshAt": now - timedelta(minutes=1),
                },
                "credits": {
                    "balance": 0,
                    "reserved": 0,
                },
            },
            merge=True,
        )

    result = run_credit_refresh(now=now, max_users=1, run_id="refresh_test_limit")

    assert result["scanned"] == 1
    assert result["processed"] == 1
    assert result["has_more_due_users"] is True

    refreshed_users = [
        (db.collection("users").document(f"user-refresh-limit-{index}").get().to_dict() or {})
        for index in range(2)
    ]
    applied = [
        user
        for user in refreshed_users
        if (user.get("billing") or {}).get("refreshScheduler", {}).get("lastStatus") == "applied"
    ]
    assert len(applied) == 1


def test_scheduler_records_reserved_status_without_advancing_refresh():
    uid = "user-refresh-reserved"
    get_or_create_credits(uid, "refresh-reserved@example.com")
    db = get_firestore_client()
    now = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    due_at = now - timedelta(minutes=1)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "activePlanKey": "free",
                "billingInterval": "none",
                "creditRefreshAnchor": now - timedelta(days=31),
                "nextCreditRefreshAt": due_at,
            },
            "credits": {
                "balance": 0,
                "reserved": 1,
            },
        },
        merge=True,
    )

    result = run_credit_refresh(now=now, max_users=10, run_id="refresh_test_reserved")

    assert result["skipped_reserved"] == 1
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 0
    assert user["billing"]["nextCreditRefreshAt"] == due_at
    assert user["billing"]["refreshScheduler"]["lastStatus"] == "reserved"
    assert user["billing"]["refreshScheduler"]["lastErrorMessage"] is None


def test_scheduler_monthly_paid_due_without_new_invoice_waits_instead_of_free_refresh():
    uid = "user-monthly-waiting-invoice"
    get_or_create_credits(uid, "monthly-waiting@example.com")
    db = get_firestore_client()
    now = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    due_at = now - timedelta(minutes=1)
    old_invoice_paid_at = now - timedelta(days=31)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "activePlanKey": "solo_monthly",
                "family": "solo",
                "billingInterval": "month",
                "stripeSubscriptionStatus": "active",
                "creditRefreshAnchor": old_invoice_paid_at,
                "lastCreditRefreshAt": old_invoice_paid_at,
                "nextCreditRefreshAt": due_at,
                "latestInvoiceId": "in_old_monthly",
                "latestInvoicePaidAt": old_invoice_paid_at,
            },
            "credits": {
                "balance": 3,
                "reserved": 0,
                "monthlyAllowance": 30,
                "lastGrantInvoiceId": "in_old_monthly",
            },
        },
        merge=True,
    )

    result = run_credit_refresh(now=now, max_users=10, run_id="refresh_test_waiting_invoice")

    assert result["processed"] == 0
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 3
    assert user["credits"]["monthlyAllowance"] == 30
    assert user["billing"]["nextCreditRefreshAt"] == due_at
    assert user["billing"]["refreshScheduler"]["lastStatus"] == "waiting_for_invoice"


def test_scheduler_paid_terminal_status_marks_inconsistent_without_free_refresh():
    uid = "user-paid-terminal-inconsistent"
    get_or_create_credits(uid, "terminal@example.com")
    db = get_firestore_client()
    now = datetime(2026, 4, 25, 13, 0, tzinfo=timezone.utc)
    due_at = now - timedelta(minutes=1)
    db.collection("users").document(uid).set(
        {
            "billing": {
                "activePlanKey": "solo_monthly",
                "family": "solo",
                "billingInterval": "month",
                "stripeSubscriptionStatus": "canceled",
                "nextCreditRefreshAt": due_at,
            },
            "credits": {
                "balance": 3,
                "reserved": 0,
                "monthlyAllowance": 30,
            },
        },
        merge=True,
    )

    result = run_credit_refresh(now=now, max_users=10, run_id="refresh_test_inconsistent")

    assert result["processed"] == 0
    user = db.collection("users").document(uid).get().to_dict() or {}
    assert user["credits"]["balance"] == 3
    assert user["billing"]["activePlanKey"] == "solo_monthly"
    assert user["billing"]["refreshScheduler"]["lastStatus"] == "billing_state_inconsistent"


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
