from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import src.backend.marketing_doi_reconcile as reconcile


def test_parse_brevo_contact_normalizes_email_and_status_fields():
    contact = reconcile._parse_brevo_contact(
        {
            "email": " Person@Example.com ",
            "listIds": ["3", 10, "not-a-list"],
            "emailBlacklisted": True,
            "listUnsubscribed": False,
            "attributes": {"DOUBLE-OPT-IN": "Yes"},
        }
    )

    assert contact is not None
    assert contact.email == "person@example.com"
    assert contact.list_ids == frozenset({3, 10})
    assert contact.email_blacklisted is True
    assert contact.list_unsubscribed is False
    assert contact.double_opt_in == "Yes"


def test_ensure_utc_normalizes_naive_and_aware_datetimes():
    assert reconcile._ensure_utc(datetime(2026, 8, 1, 12, 0)) == datetime(
        2026,
        8,
        1,
        12,
        0,
        tzinfo=timezone.utc,
    )


def test_reminder_attributes_preserve_provenance_without_rewriting_consent_date():
    assert reconcile._reminder_attributes(
        {
            "emailOptInSource": "studio_menu_authenticated",
            "emailOptInConsentText": "Send me product updates.",
            "emailOptInRequestedAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
    ) == {
        "SIGNUP_SOURCE": "studio_menu_authenticated",
        "GDPR_CONSENT_TEXT": "Send me product updates.",
    }


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.payload = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, _url, *, headers, json):
        assert headers["api-key"] == "test-key"
        self.payload = json
        return self.response


def _settings():
    return SimpleNamespace(
        brevo_waitlist_api_key_secret="BREVO_WAITLIST_API_KEY",
        brevo_waitlist_api_key_secret_version="latest",
        brevo_waitlist_timeout_seconds=10,
        brevo_waitlist_list_id=3,
        brevo_doi_template_id=1,
        brevo_doi_redirect_url="https://example.test/confirmed",
    )


def test_send_doi_reminder_records_only_unambiguous_provider_acceptance(monkeypatch):
    client = _FakeClient(_FakeResponse(204))
    monkeypatch.setattr(reconcile, "read_secret", lambda *_args: "test-key")
    monkeypatch.setattr(reconcile.httpx, "Client", lambda **_kwargs: client)

    reconcile.send_doi_reminder(
        _settings(),
        email="person@example.com",
        marketing={
            "emailOptInSource": "signup",
            "emailOptInConsentText": "I agree.",
            "emailOptInRequestedAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
        },
    )

    assert client.payload == {
        "email": "person@example.com",
        "includeListIds": [3],
        "templateId": 1,
        "redirectionUrl": "https://example.test/confirmed",
        "attributes": {"SIGNUP_SOURCE": "signup", "GDPR_CONSENT_TEXT": "I agree."},
    }


def test_send_doi_reminder_treats_existing_contact_response_as_ambiguous(monkeypatch):
    monkeypatch.setattr(reconcile, "read_secret", lambda *_args: "test-key")
    monkeypatch.setattr(
        reconcile.httpx,
        "Client",
        lambda **_kwargs: _FakeClient(_FakeResponse(400, "Contact already exists")),
    )

    with pytest.raises(reconcile.BrevoUnknownDeliveryError):
        reconcile.send_doi_reminder(_settings(), email="person@example.com", marketing={})


def test_send_doi_reminder_treats_transport_failure_as_ambiguous(monkeypatch):
    class _FailingClient(_FakeClient):
        def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(reconcile, "read_secret", lambda *_args: "test-key")
    monkeypatch.setattr(
        reconcile.httpx,
        "Client",
        lambda **_kwargs: _FailingClient(_FakeResponse(500)),
    )

    with pytest.raises(reconcile.BrevoUnknownDeliveryError):
        reconcile.send_doi_reminder(_settings(), email="person@example.com", marketing={})


class _FakeSnapshot:
    def __init__(self, data):
        self.reference = SimpleNamespace(id="user-1")
        self._data = data

    def to_dict(self):
        return self._data


def _scheduler_settings(*, mode="send"):
    return SimpleNamespace(
        marketing_doi_reconcile_mode=mode,
        marketing_doi_reminder_delay_days=3,
        marketing_doi_expiry_days=14,
        brevo_waitlist_list_id=3,
    )


def _candidate_marketing(*, status="doi_requested", next_action_at=None, expires_at=None):
    original = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return {
        "emailOptInBrevoStatus": status,
        "emailOptInBrevoCycleId": "cycle-1",
        "emailOptInBrevoOriginalRequestedAt": original,
        "emailOptInBrevoNextActionAt": next_action_at or original,
        "emailOptInBrevoExpiresAt": expires_at or datetime(2026, 8, 15, tzinfo=timezone.utc),
        "emailOptInEmail": "person@example.com",
    }


def _result():
    return {
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
    }


def test_process_candidate_reconciles_final_list_member_before_due_reminder(monkeypatch):
    marketing = _candidate_marketing()
    terminal_updates = []
    monkeypatch.setattr(reconcile, "_normalize_legacy_candidate", lambda *_args: marketing)
    monkeypatch.setattr(
        reconcile,
        "_set_terminal_state",
        lambda *args, **kwargs: terminal_updates.append(kwargs) or True,
    )
    monkeypatch.setattr(
        reconcile,
        "send_doi_reminder",
        lambda *_args, **_kwargs: pytest.fail("confirmed users must not be reminded"),
    )

    result = _result()
    reconcile._process_candidate(
        _FakeSnapshot({"marketing": marketing}),
        contacts={
            "person@example.com": reconcile.BrevoContact(
                email="person@example.com",
                list_ids=frozenset({3}),
                email_blacklisted=False,
                list_unsubscribed=False,
                double_opt_in=None,
            )
        },
        settings=_scheduler_settings(),
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
        result=result,
    )

    assert result["confirmed_reconciled"] == 1
    assert terminal_updates[0]["status"] == "doi_confirmed"


def test_process_candidate_sends_one_due_reminder_when_contact_is_absent(monkeypatch):
    marketing = _candidate_marketing()
    sent = []
    monkeypatch.setattr(reconcile, "_normalize_legacy_candidate", lambda *_args: marketing)
    monkeypatch.setattr(reconcile, "_claim_reminder", lambda *_args: True)
    monkeypatch.setattr(
        reconcile,
        "send_doi_reminder",
        lambda _settings, *, email, marketing: sent.append((email, marketing)),
    )
    monkeypatch.setattr(reconcile, "_mark_reminder_sent", lambda *_args: True)

    result = _result()
    reconcile._process_candidate(
        _FakeSnapshot({"marketing": marketing}),
        contacts={},
        settings=_scheduler_settings(),
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
        result=result,
    )

    assert sent == [("person@example.com", marketing)]
    assert result["reminders_sent"] == 1


def test_process_candidate_never_expires_in_observe_mode(monkeypatch):
    marketing = _candidate_marketing(expires_at=datetime(2026, 8, 2, tzinfo=timezone.utc))
    monkeypatch.setattr(reconcile, "_normalize_legacy_candidate", lambda *_args: marketing)
    monkeypatch.setattr(
        reconcile,
        "_set_terminal_state",
        lambda *_args, **_kwargs: pytest.fail("observe mode must not expire a cycle"),
    )

    result = _result()
    reconcile._process_candidate(
        _FakeSnapshot({"marketing": marketing}),
        contacts={},
        settings=_scheduler_settings(mode="observe"),
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
        result=result,
    )

    assert result["skipped_observe_mode"] == 1


def test_billing_internal_route_requires_expected_scheduler_identity(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    from src.backend import billing_api

    app = billing_api.create_billing_app()
    object.__setattr__(
        app.state.settings,
        "marketing_doi_scheduler_service_account",
        "scheduler@sightsinger-app.iam.gserviceaccount.com",
    )
    object.__setattr__(
        app.state.settings,
        "marketing_doi_scheduler_audience",
        "https://billing.example.test",
    )
    monkeypatch.setattr(
        billing_api,
        "_verify_google_oidc_token",
        lambda token, audience: {
            "email": "scheduler@sightsinger-app.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": audience,
        },
    )
    monkeypatch.setattr(
        reconcile,
        "run_marketing_doi_reconciliation",
        lambda _settings: {"status": "disabled", "run_id": "test-run"},
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/marketing/doi-reconcile",
            headers={"Authorization": "Bearer scheduler-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "disabled", "run_id": "test-run"}


def test_billing_internal_route_rejects_unexpected_scheduler_identity(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    from src.backend import billing_api

    app = billing_api.create_billing_app()
    object.__setattr__(
        app.state.settings,
        "marketing_doi_scheduler_service_account",
        "scheduler@sightsinger-app.iam.gserviceaccount.com",
    )
    object.__setattr__(
        app.state.settings,
        "marketing_doi_scheduler_audience",
        "https://billing.example.test",
    )
    monkeypatch.setattr(
        billing_api,
        "_verify_google_oidc_token",
        lambda *_args: {"email": "other@example.com", "email_verified": True},
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/marketing/doi-reconcile",
            headers={"Authorization": "Bearer scheduler-token"},
        )

    assert response.status_code == 403
