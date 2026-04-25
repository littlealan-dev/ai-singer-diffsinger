from __future__ import annotations

"""Login-time bootstrap and migration helpers for recurring billing."""

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_store import free_billing_payload
from src.backend.firebase_app import get_firestore_client

FREE_TIER_MONTHLY_ALLOWANCE = 8


def ensure_billing_state_for_login(uid: str, email: str) -> dict[str, Any]:
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def _ensure(transaction):
        snapshot = user_ref.get(transaction=transaction)
        if not snapshot.exists:
            transaction.set(
                user_ref,
                {
                    "email": email,
                    "createdAt": now,
                    "billing": free_billing_payload(now=now, anchor=now),
                    "credits": {
                        "balance": FREE_TIER_MONTHLY_ALLOWANCE,
                        "reserved": 0,
                        "overdrafted": False,
                        "expiresAt": None,
                        "monthlyAllowance": FREE_TIER_MONTHLY_ALLOWANCE,
                        "lastGrantType": "grant_free_monthly",
                        "lastGrantAt": now,
                        "lastGrantInvoiceId": None,
                    },
                },
                merge=True,
            )
            transaction.set(
                db.collection("credit_ledger").document(f"grant_bootstrap_{uid}"),
                {
                    "userId": uid,
                    "type": "grant_free_monthly",
                    "amount": FREE_TIER_MONTHLY_ALLOWANCE,
                    "balanceAfter": FREE_TIER_MONTHLY_ALLOWANCE,
                    "createdAt": now,
                    "reason": "bootstrap_free_tier",
                },
            )
            return {
                "billing": free_billing_payload(now=now, anchor=now),
                "credits": {
                    "balance": FREE_TIER_MONTHLY_ALLOWANCE,
                    "reserved": 0,
                    "overdrafted": False,
                    "expiresAt": None,
                    "monthlyAllowance": FREE_TIER_MONTHLY_ALLOWANCE,
                    "lastGrantType": "grant_free_monthly",
                    "lastGrantAt": now,
                },
            }

        data = snapshot.to_dict() or {}
        billing = data.get("billing")
        credits = data.get("credits") or {}
        if billing and credits:
            return data

        created_at = data.get("createdAt") or now
        expires_at = credits.get("expiresAt")
        trial_granted_at = credits.get("trialGrantedAt") or created_at
        if expires_at and _to_utc(expires_at) > now:
            anchor = _to_utc(trial_granted_at)
            merged_billing = free_billing_payload(now=now, anchor=anchor)
            merged_billing["lastCreditRefreshAt"] = anchor
            transaction.set(
                user_ref,
                {
                    "email": email,
                    "billing": merged_billing,
                    "credits": {
                        "monthlyAllowance": FREE_TIER_MONTHLY_ALLOWANCE,
                    },
                },
                merge=True,
            )
            data["billing"] = merged_billing
            credits["monthlyAllowance"] = FREE_TIER_MONTHLY_ALLOWANCE
            data["credits"] = credits
            return data

        anchor = now
        converted_billing = free_billing_payload(now=now, anchor=anchor)
        converted_credits = {
            "balance": FREE_TIER_MONTHLY_ALLOWANCE,
            "reserved": int(credits.get("reserved", 0) or 0),
            "overdrafted": False,
            "expiresAt": None,
            "monthlyAllowance": FREE_TIER_MONTHLY_ALLOWANCE,
            "lastGrantType": "grant_free_monthly",
            "lastGrantAt": now,
            "lastGrantInvoiceId": None,
        }
        transaction.set(
            user_ref,
            {
                "email": email,
                "billing": converted_billing,
                "credits": converted_credits,
                "metadata": {
                    "legacyTrialConvertedAt": now,
                },
            },
            merge=True,
        )
        transaction.set(
            db.collection("credit_ledger").document(f"grant_conversion_{uid}"),
            {
                "userId": uid,
                "type": "grant_free_monthly",
                "amount": FREE_TIER_MONTHLY_ALLOWANCE,
                "balanceAfter": FREE_TIER_MONTHLY_ALLOWANCE,
                "createdAt": now,
                "reason": "expired_legacy_trial_conversion",
            },
        )
        return {
            **data,
            "billing": converted_billing,
            "credits": converted_credits,
        }

    return _ensure(db.transaction())


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
