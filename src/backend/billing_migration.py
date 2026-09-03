from __future__ import annotations

"""Login-time bootstrap and migration helpers for recurring billing."""

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from src.backend.billing_plans import get_free_tier_monthly_allowance
from src.backend.billing_store import free_billing_payload
from src.backend.firebase_app import get_firestore_client


def ensure_billing_state_for_login(uid: str, email: str) -> dict[str, Any]:
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    now = datetime.now(timezone.utc)
    free_tier_monthly_allowance = get_free_tier_monthly_allowance()

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
                        "balance": free_tier_monthly_allowance,
                        "reserved": 0,
                        "overdrafted": False,
                        "expiresAt": None,
                        "monthlyAllowance": free_tier_monthly_allowance,
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
                    "amount": free_tier_monthly_allowance,
                    "balanceAfter": free_tier_monthly_allowance,
                    "createdAt": now,
                    "reason": "bootstrap_free_tier",
                },
            )
            return {
                "billing": free_billing_payload(now=now, anchor=now),
                "credits": {
                    "balance": free_tier_monthly_allowance,
                    "reserved": 0,
                    "overdrafted": False,
                    "expiresAt": None,
                    "monthlyAllowance": free_tier_monthly_allowance,
                    "lastGrantType": "grant_free_monthly",
                    "lastGrantAt": now,
                },
            }

        data = snapshot.to_dict() or {}
        billing = data.get("billing")
        credits = data.get("credits") or {}
        if billing and credits:
            existing_allowance = int(credits.get("monthlyAllowance", 0) or 0)
            is_free_plan = str(billing.get("activePlanKey") or "free") == "free"
            has_no_reservation = int(credits.get("reserved", 0) or 0) == 0
            if (
                is_free_plan
                and has_no_reservation
                and existing_allowance < free_tier_monthly_allowance
            ):
                upgraded_balance = max(
                    int(credits.get("balance", 0) or 0),
                    free_tier_monthly_allowance,
                )
                transaction.update(
                    user_ref,
                    {
                        "credits.balance": upgraded_balance,
                        "credits.monthlyAllowance": free_tier_monthly_allowance,
                        "credits.lastGrantType": "grant_free_tier_allowance_upgrade",
                        "credits.lastGrantAt": now,
                    },
                )
                transaction.set(
                    db.collection("credit_ledger").document(
                        f"grant_free_tier_allowance_{free_tier_monthly_allowance}_{uid}"
                    ),
                    {
                        "userId": uid,
                        "type": "grant_free_tier_allowance_upgrade",
                        "amount": upgraded_balance - int(credits.get("balance", 0) or 0),
                        "balanceAfter": upgraded_balance,
                        "createdAt": now,
                        "reason": "free_tier_allowance_increase",
                    },
                )
                credits.update(
                    {
                        "balance": upgraded_balance,
                        "monthlyAllowance": free_tier_monthly_allowance,
                        "lastGrantType": "grant_free_tier_allowance_upgrade",
                        "lastGrantAt": now,
                    }
                )
                data["credits"] = credits
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
                        "monthlyAllowance": free_tier_monthly_allowance,
                    },
                },
                merge=True,
            )
            data["billing"] = merged_billing
            credits["monthlyAllowance"] = free_tier_monthly_allowance
            data["credits"] = credits
            return data

        anchor = now
        converted_billing = free_billing_payload(now=now, anchor=anchor)
        converted_credits = {
            "balance": free_tier_monthly_allowance,
            "reserved": int(credits.get("reserved", 0) or 0),
            "overdrafted": False,
            "expiresAt": None,
            "monthlyAllowance": free_tier_monthly_allowance,
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
                "amount": free_tier_monthly_allowance,
                "balanceAfter": free_tier_monthly_allowance,
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
