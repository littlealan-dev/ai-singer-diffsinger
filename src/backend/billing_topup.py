from __future__ import annotations

"""One-time top-up credit pack helpers."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from google.cloud import firestore

from src.backend.billing_config import BillingConfig, get_billing_config, get_stripe_v1_client
from src.backend.billing_store import get_billing_state, upsert_stripe_customer_id
from src.backend.billing_types import BillingHttpError
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

TOPUP_PACK_KEY = "topup_15"


@dataclass
class TopupPack:
    ref: Any
    pack_id: str
    credits_remaining: int
    credits_reserved: int
    expires_at: datetime | None
    stripe_checkout_session_id: str | None = None


@dataclass
class TopupPackState:
    active_packs: list[TopupPack]
    total_remaining: int
    total_reserved: int
    total_available: int
    active_pack_count: int
    earliest_expires_at: datetime | None


def create_topup_checkout_session(
    uid: str,
    email: str,
    pack_key: str = TOPUP_PACK_KEY,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, Any]:
    return _create_topup_checkout_session(
        uid,
        email,
        pack_key,
        embedded=False,
        config=config,
        stripe_client=stripe_client,
    )


def create_embedded_topup_checkout_session(
    uid: str,
    email: str,
    pack_key: str = TOPUP_PACK_KEY,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, Any]:
    return _create_topup_checkout_session(
        uid,
        email,
        pack_key,
        embedded=True,
        config=config,
        stripe_client=stripe_client,
    )


def _create_topup_checkout_session(
    uid: str,
    email: str,
    pack_key: str,
    *,
    embedded: bool,
    config: BillingConfig | None,
    stripe_client: Any | None,
) -> dict[str, Any]:
    billing_config = config or get_billing_config()
    if pack_key != TOPUP_PACK_KEY:
        raise BillingHttpError(400, "Invalid credit pack.")
    if not billing_config.stripe_price_topup_15:
        raise BillingHttpError(503, "Credit pack checkout is not configured.")

    db = get_firestore_client()
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client

    stripe_customer_id = get_billing_state(uid).get("stripeCustomerId")
    if not stripe_customer_id:
        customer = client.customers.create(
            params={
                "email": email or None,
                "metadata": {
                    "firebaseUserId": uid,
                    "environment": "app",
                },
            },
        )
        stripe_customer_id = customer.id
        upsert_stripe_customer_id(uid, stripe_customer_id)

    hold_id, remaining_slots, hold_expires_at = _create_checkout_hold(db, uid, billing_config)
    params: dict[str, Any] = {
        "mode": "payment",
        "customer": stripe_customer_id,
        "expires_at": int(hold_expires_at.timestamp()),
        "line_items": [
            {
                "price": billing_config.stripe_price_topup_15,
                "quantity": 1,
                "adjustable_quantity": {
                    "enabled": True,
                    "minimum": 1,
                    "maximum": remaining_slots,
                },
            }
        ],
        "client_reference_id": uid,
        "metadata": {
            "firebaseUserId": uid,
            "purchaseType": "topup",
            "packKey": TOPUP_PACK_KEY,
            "creditAmount": str(billing_config.topup_pack_credit_amount),
            "checkoutHoldId": hold_id,
            "maxQuantity": str(remaining_slots),
            **({"checkoutUiMode": "embedded"} if embedded else {}),
        },
    }
    if embedded:
        params.update(
            {
                "ui_mode": "embedded_page",
                "redirect_on_completion": "never",
            }
        )
    else:
        params.update(
            {
                "success_url": billing_config.topup_success_url,
                "cancel_url": billing_config.topup_cancel_url,
            }
        )
    try:
        session = client.checkout.sessions.create(params=params)
    except Exception:
        _cancel_checkout_hold(db, hold_id)
        raise

    db.collection("topup_checkout_holds").document(hold_id).set(
        {"stripeCheckoutSessionId": session.id},
        merge=True,
    )
    logger.info(
        "topup_checkout_created uid=%s hold_id=%s max_quantity=%d ui_mode=%s",
        uid,
        hold_id,
        remaining_slots,
        "embedded" if embedded else "hosted",
    )
    if embedded:
        client_secret = _object_get(session, "client_secret")
        if not client_secret:
            _cancel_checkout_hold(db, hold_id)
            raise BillingHttpError(503, "Embedded credit pack checkout is missing its client secret.")
        return {
            "checkoutSessionId": str(session.id),
            "clientSecret": str(client_secret),
            "checkoutType": "topup",
            "maxQuantity": remaining_slots,
        }
    return {"url": str(session.url), "maxQuantity": remaining_slots}


def cancel_topup_checkout_session(
    uid: str,
    checkout_session_id: str,
    *,
    stripe_client: Any | None = None,
) -> dict[str, Any]:
    session_id = checkout_session_id.strip()
    if not session_id:
        raise BillingHttpError(400, "Missing Checkout session id.")

    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    session = _to_plain_dict(client.checkout.sessions.retrieve(session_id))
    _assert_topup_session_belongs_to_user(uid, session)
    if session.get("mode") != "payment" or (session.get("metadata") or {}).get("purchaseType") != "topup":
        raise BillingHttpError(409, "Checkout session is not a credit pack checkout.")
    if session.get("status") == "complete" or session.get("payment_status") == "paid":
        return {"cancelled": False, "status": str(session.get("status") or "complete")}

    metadata = session.get("metadata") or {}
    hold_id = str(metadata.get("checkoutHoldId") or "").strip()
    if not hold_id:
        raise BillingHttpError(409, "Top-up checkout session is missing its hold id.")

    db = get_firestore_client()
    hold_ref = db.collection("topup_checkout_holds").document(hold_id)
    now = datetime.now(timezone.utc)

    @firestore.transactional
    def _transactional_cancel(transaction):
        hold_snapshot = hold_ref.get(transaction=transaction)
        if not hold_snapshot.exists:
            raise BillingHttpError(409, "Top-up checkout hold is missing.")
        hold = hold_snapshot.to_dict() or {}
        if hold.get("userId") != uid:
            raise BillingHttpError(409, "Top-up checkout hold user mismatch.")
        status = str(hold.get("status") or "")
        if status == "completed":
            return False
        if status in {"cancelled", "expired"}:
            return True
        if status != "pending":
            raise BillingHttpError(409, "Top-up checkout hold is no longer pending.")
        hold_session_id = str(hold.get("stripeCheckoutSessionId") or "")
        if hold_session_id and hold_session_id != session_id:
            raise BillingHttpError(409, "Top-up checkout hold session mismatch.")
        transaction.update(
            hold_ref,
            {
                "status": "cancelled",
                "cancelledAt": now,
                "cancelReason": "client_abandoned_checkout",
            },
        )
        return True

    cancelled = bool(_transactional_cancel(db.transaction()))
    stripe_expired = _expire_checkout_session_if_open(client, session_id)
    logger.info(
        "topup_checkout_cancelled uid=%s session=%s cancelled=%s stripe_expired=%s",
        uid,
        session_id,
        cancelled,
        stripe_expired,
    )
    return {
        "cancelled": cancelled,
        "status": "cancelled" if cancelled else str(session.get("status") or ""),
        "stripeExpired": stripe_expired,
    }


def apply_topup_checkout_completed(
    payload: dict[str, Any],
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> None:
    billing_config = config or get_billing_config()
    uid = _resolve_checkout_uid(payload)
    if not uid:
        raise BillingHttpError(409, "Unable to resolve Firebase user for top-up checkout session.")
    if payload.get("payment_status") != "paid":
        logger.info(
            "topup_checkout_completed_ignored uid=%s session=%s payment_status=%s",
            uid,
            payload.get("id"),
            payload.get("payment_status"),
        )
        return

    metadata = payload.get("metadata") or {}
    hold_id = str(metadata.get("checkoutHoldId") or "").strip()
    if not hold_id:
        raise BillingHttpError(409, "Top-up checkout session is missing its hold id.")

    quantity = _resolve_topup_quantity(payload, billing_config, stripe_client=stripe_client)
    if quantity <= 0:
        raise BillingHttpError(409, "Top-up checkout session has no purchased credit packs.")

    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    hold_ref = db.collection("topup_checkout_holds").document(hold_id)
    now = datetime.now(timezone.utc)
    session_id = str(payload.get("id") or "")
    payment_intent_id = _stripe_id_from_expandable(payload.get("payment_intent"))

    @firestore.transactional
    def _transactional_apply(transaction):
        hold_snapshot = hold_ref.get(transaction=transaction)
        if not hold_snapshot.exists:
            raise BillingHttpError(409, "Top-up checkout hold is missing.")
        hold = hold_snapshot.to_dict() or {}
        hold_status = str(hold.get("status") or "")
        if hold_status == "completed":
            logger.info("topup_grant_idempotent uid=%s hold_id=%s", uid, hold_id)
            return
        if hold_status != "pending":
            raise BillingHttpError(409, "Top-up checkout hold is no longer pending.")
        if hold.get("userId") != uid:
            raise BillingHttpError(409, "Top-up checkout hold user mismatch.")
        hold_expires_at = _to_utc_datetime(hold.get("expiresAt"))
        if hold_expires_at and hold_expires_at <= now:
            transaction.update(hold_ref, {"status": "expired", "expiredAt": now})
            raise BillingHttpError(409, "Top-up checkout hold expired.")
        remaining_slots = int(hold.get("remainingSlots", 0) or 0)
        if quantity > remaining_slots:
            raise BillingHttpError(409, "Top-up checkout quantity exceeds the reserved slot limit.")

        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            raise BillingHttpError(409, "Top-up user document is missing.")
        user_data = user_snapshot.to_dict() or {}
        pack_state = refresh_topup_pack_state_in_transaction(
            transaction,
            db,
            uid,
            now,
            expire_stale=True,
        )
        if pack_state.active_pack_count + quantity > billing_config.topup_max_active_packs:
            raise BillingHttpError(409, "Maximum 3 active credit packs reached.")

        credits = user_data.get("credits") if isinstance(user_data.get("credits"), dict) else {}
        subscription_balance = int(credits.get("balance", 0) or 0)
        total_granted = quantity * billing_config.topup_pack_credit_amount
        overdraft_recovery = min(max(0, -subscription_balance), total_granted)
        topup_grant_remaining = total_granted - overdraft_recovery
        new_subscription_balance = subscription_balance + overdraft_recovery

        created_packs: list[TopupPack] = []
        remaining_for_packs = topup_grant_remaining
        expires_at = now + timedelta(days=billing_config.topup_pack_expiry_days)
        for index in range(quantity):
            pack_id = f"topup_{_safe_doc_id(session_id)}_{index + 1}"
            pack_ref = db.collection("topup_packs").document(pack_id)
            pack_credits = min(billing_config.topup_pack_credit_amount, max(0, remaining_for_packs))
            remaining_for_packs -= pack_credits
            status = "active" if pack_credits > 0 else "exhausted"
            transaction.set(
                pack_ref,
                {
                    "userId": uid,
                    "packId": pack_id,
                    "packKey": TOPUP_PACK_KEY,
                    "creditsGranted": billing_config.topup_pack_credit_amount,
                    "creditsRemaining": pack_credits,
                    "creditsReserved": 0,
                    "priceCents": 500,
                    "currency": "usd",
                    "stripePaymentIntentId": payment_intent_id,
                    "stripeCheckoutSessionId": session_id,
                    "purchasedAt": now,
                    "expiresAt": expires_at,
                    "status": status,
                    "createdAt": now,
                },
            )
            if pack_credits > 0:
                created_packs.append(
                    TopupPack(
                        ref=pack_ref,
                        pack_id=pack_id,
                        credits_remaining=pack_credits,
                        credits_reserved=0,
                        expires_at=expires_at,
                        stripe_checkout_session_id=session_id,
                    )
                )
            transaction.set(
                db.collection("credit_ledger").document(f"topup_grant_{_safe_doc_id(session_id)}_{index + 1}"),
                {
                    "userId": uid,
                    "type": "topup_grant",
                    "amount": billing_config.topup_pack_credit_amount,
                    "packId": pack_id,
                    "stripePaymentIntentId": payment_intent_id,
                    "stripeCheckoutSessionId": session_id,
                    "topupRemainingAfter": pack_state.total_remaining + sum(
                        pack.credits_remaining for pack in created_packs
                    ),
                    "subscriptionBalanceAfter": new_subscription_balance,
                    "overdraftRecoveryCredits": overdraft_recovery,
                    "createdAt": now,
                },
            )

        active_after = _sort_active_packs(pack_state.active_packs + created_packs)
        transaction.update(
            user_ref,
            {
                "credits.balance": new_subscription_balance,
                "credits.overdrafted": new_subscription_balance < 0,
                **topup_aggregate_fields(active_after),
            },
        )
        transaction.update(
            hold_ref,
            {
                "status": "completed",
                "completedAt": now,
                "quantity": quantity,
                "stripeCheckoutSessionId": session_id,
            },
        )
        logger.info(
            "topup_grant_applied uid=%s session=%s quantity=%d credits=%d",
            uid,
            session_id,
            quantity,
            total_granted,
        )

    _transactional_apply(db.transaction())


def sync_topup_checkout_session(
    uid: str,
    checkout_session_id: str,
    *,
    config: BillingConfig | None = None,
    stripe_client: Any | None = None,
) -> dict[str, Any]:
    session_id = checkout_session_id.strip()
    if not session_id:
        raise BillingHttpError(400, "Missing Checkout session id.")

    billing_config = config or get_billing_config()
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    session = _to_plain_dict(
        client.checkout.sessions.retrieve(
            session_id,
            params={"expand": ["line_items"]},
        )
    )
    _assert_topup_session_belongs_to_user(uid, session)
    if session.get("mode") != "payment" or (session.get("metadata") or {}).get("purchaseType") != "topup":
        raise BillingHttpError(409, "Checkout session is not a credit pack checkout.")
    if session.get("status") != "complete" or session.get("payment_status") != "paid":
        raise BillingHttpError(409, "Credit pack checkout is not complete yet.")

    apply_topup_checkout_completed(session, config=billing_config, stripe_client=stripe_client)
    user = get_firestore_client().collection("users").document(uid).get().to_dict() or {}
    topup_credits = user.get("topupCredits") if isinstance(user.get("topupCredits"), dict) else {}
    return {
        "synced": True,
        "status": str(session.get("status") or "complete"),
        "topupCredits": {
            "totalRemaining": int(topup_credits.get("totalRemaining", 0) or 0),
            "totalReserved": int(topup_credits.get("totalReserved", 0) or 0),
            "totalAvailable": int(topup_credits.get("totalAvailable", 0) or 0),
            "activePackCount": int(topup_credits.get("activePackCount", 0) or 0),
        },
    }


def refresh_topup_pack_state_in_transaction(
    transaction: Any,
    db: Any,
    uid: str,
    now: datetime,
    *,
    expire_stale: bool,
) -> TopupPackState:
    snapshots = list(
        db.collection("topup_packs")
        .where(filter=firestore.FieldFilter("userId", "==", uid))
        .stream(transaction=transaction)
    )
    active_packs: list[TopupPack] = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        if data.get("status") != "active":
            continue
        remaining = int(data.get("creditsRemaining", 0) or 0)
        reserved = min(remaining, max(0, int(data.get("creditsReserved", 0) or 0)))
        pack_id = str(data.get("packId") or snapshot.id)
        expires_at = _to_utc_datetime(data.get("expiresAt"))
        if remaining <= 0:
            transaction.update(
                snapshot.reference,
                {"status": "exhausted", "exhaustedAt": now, "creditsReserved": 0},
            )
            continue
        if expire_stale and expires_at and expires_at <= now:
            transaction.update(
                snapshot.reference,
                {
                    "status": "expired",
                    "expiredAt": now,
                    "creditsRemaining": 0,
                    "creditsReserved": 0,
                },
            )
            transaction.set(
                db.collection("credit_ledger").document(f"topup_expire_{pack_id}"),
                {
                    "userId": uid,
                    "type": "topup_expire",
                    "amount": -remaining,
                    "packId": pack_id,
                    "topupRemainingAfter": 0,
                    "reason": "pack_expired_180d",
                    "createdAt": now,
                },
            )
            logger.info("topup_pack_expired uid=%s pack_id=%s forfeited=%d", uid, pack_id, remaining)
            continue
        active_packs.append(
            TopupPack(
                ref=snapshot.reference,
                pack_id=pack_id,
                credits_remaining=remaining,
                credits_reserved=reserved,
                expires_at=expires_at,
                stripe_checkout_session_id=_string_or_none(data.get("stripeCheckoutSessionId")),
            )
        )
    active_packs = _sort_active_packs(active_packs)
    total_reserved = sum(pack.credits_reserved for pack in active_packs)
    total_remaining = sum(pack.credits_remaining for pack in active_packs)
    return TopupPackState(
        active_packs=active_packs,
        total_remaining=total_remaining,
        total_reserved=total_reserved,
        total_available=max(0, total_remaining - total_reserved),
        active_pack_count=len(active_packs),
        earliest_expires_at=active_packs[0].expires_at if active_packs else None,
    )


def reserve_topup_credits_in_transaction(
    transaction: Any,
    amount: int,
    active_packs: list[TopupPack],
) -> tuple[int, list[dict[str, Any]], list[TopupPack]]:
    remaining_to_reserve = max(0, int(amount))
    reserved = 0
    allocations: list[dict[str, Any]] = []
    active_after: list[TopupPack] = []
    for pack in active_packs:
        available = max(0, pack.credits_remaining - pack.credits_reserved)
        allocate = min(remaining_to_reserve, available)
        new_reserved = pack.credits_reserved + allocate
        if allocate > 0:
            remaining_to_reserve -= allocate
            reserved += allocate
            allocations.append({"packId": pack.pack_id, "credits": allocate})
            transaction.update(pack.ref, {"creditsReserved": new_reserved})
        active_after.append(
            TopupPack(
                ref=pack.ref,
                pack_id=pack.pack_id,
                credits_remaining=pack.credits_remaining,
                credits_reserved=new_reserved,
                expires_at=pack.expires_at,
                stripe_checkout_session_id=pack.stripe_checkout_session_id,
            )
        )
    return reserved, allocations, _sort_active_packs(active_after)


def release_reserved_topup_credits_in_transaction(
    transaction: Any,
    reserved_allocations: list[dict[str, Any]],
    active_packs: list[TopupPack],
) -> list[TopupPack]:
    allocations_by_pack = _allocation_amounts_by_pack(reserved_allocations)
    active_after: list[TopupPack] = []
    for pack in active_packs:
        release = min(pack.credits_reserved, allocations_by_pack.get(pack.pack_id, 0))
        new_reserved = pack.credits_reserved - release
        if release > 0:
            transaction.update(pack.ref, {"creditsReserved": new_reserved})
        active_after.append(
            TopupPack(
                ref=pack.ref,
                pack_id=pack.pack_id,
                credits_remaining=pack.credits_remaining,
                credits_reserved=new_reserved,
                expires_at=pack.expires_at,
                stripe_checkout_session_id=pack.stripe_checkout_session_id,
            )
        )
    return _sort_active_packs(active_after)


def consume_reserved_topup_credits_in_transaction(
    transaction: Any,
    db: Any,
    uid: str,
    job_id: str,
    reserved_allocations: list[dict[str, Any]],
    consume_amount: int,
    active_packs: list[TopupPack],
    now: datetime,
    *,
    subscription_balance_after: int,
) -> tuple[int, list[TopupPack]]:
    remaining_to_consume = max(0, int(consume_amount))
    allocations_by_pack = _allocation_amounts_by_pack(reserved_allocations)
    consumed = 0
    total_remaining_before = sum(pack.credits_remaining for pack in active_packs)
    total_remaining_after = total_remaining_before
    active_after: list[TopupPack] = []
    for pack in active_packs:
        reserved_for_job = min(pack.credits_reserved, allocations_by_pack.get(pack.pack_id, 0))
        consume = min(remaining_to_consume, reserved_for_job)
        release = reserved_for_job - consume
        new_reserved = pack.credits_reserved - consume - release
        new_remaining = pack.credits_remaining - consume
        if consume or release:
            transaction.update(
                pack.ref,
                {
                    "creditsRemaining": new_remaining,
                    "creditsReserved": new_reserved,
                    "status": "exhausted" if new_remaining == 0 else "active",
                    **({"exhaustedAt": now} if new_remaining == 0 else {}),
                },
            )
        if consume:
            remaining_to_consume -= consume
            consumed += consume
            total_remaining_after -= consume
            transaction.set(
                db.collection("credit_ledger").document(f"topup_consume_{job_id}_{pack.pack_id}"),
                {
                    "userId": uid,
                    "type": "topup_consume",
                    "jobId": job_id,
                    "amount": -consume,
                    "packId": pack.pack_id,
                    "topupRemainingAfter": max(0, total_remaining_after),
                    "subscriptionBalanceAfter": subscription_balance_after,
                    "reservedTopupConsumed": True,
                    "createdAt": now,
                },
            )
            logger.info(
                "reserved_topup_credits_consumed uid=%s job_id=%s pack_id=%s amount=%d remaining=%d reserved=%d",
                uid,
                job_id,
                pack.pack_id,
                consume,
                new_remaining,
                new_reserved,
            )
        if new_remaining > 0:
            active_after.append(
                TopupPack(
                    ref=pack.ref,
                    pack_id=pack.pack_id,
                    credits_remaining=new_remaining,
                    credits_reserved=new_reserved,
                    expires_at=pack.expires_at,
                    stripe_checkout_session_id=pack.stripe_checkout_session_id,
                )
            )
    return consumed, _sort_active_packs(active_after)


def consume_topup_credits_in_transaction(
    transaction: Any,
    db: Any,
    uid: str,
    job_id: str,
    amount: int,
    active_packs: list[TopupPack],
    now: datetime,
    *,
    subscription_balance_after: int,
) -> tuple[int, list[TopupPack]]:
    remaining_to_consume = max(0, int(amount))
    consumed = 0
    active_after: list[TopupPack] = []
    total_remaining_before = sum(pack.credits_remaining for pack in active_packs)
    total_remaining_after = total_remaining_before
    for pack in active_packs:
        if remaining_to_consume <= 0:
            active_after.append(pack)
            continue
        available = max(0, pack.credits_remaining - pack.credits_reserved)
        deduct = min(remaining_to_consume, available)
        if deduct <= 0:
            active_after.append(pack)
            continue
        new_remaining = pack.credits_remaining - deduct
        remaining_to_consume -= deduct
        consumed += deduct
        total_remaining_after -= deduct
        transaction.update(
            pack.ref,
            {
                "creditsRemaining": new_remaining,
                "status": "exhausted" if new_remaining == 0 else "active",
                **({"exhaustedAt": now} if new_remaining == 0 else {}),
            },
        )
        transaction.set(
            db.collection("credit_ledger").document(f"topup_consume_{job_id}_{pack.pack_id}"),
            {
                "userId": uid,
                "type": "topup_consume",
                "jobId": job_id,
                "amount": -deduct,
                "packId": pack.pack_id,
                "topupRemainingAfter": max(0, total_remaining_after),
                "subscriptionBalanceAfter": subscription_balance_after,
                "createdAt": now,
            },
        )
        logger.info(
            "topup_credits_consumed uid=%s job_id=%s pack_id=%s amount=%d remaining=%d",
            uid,
            job_id,
            pack.pack_id,
            deduct,
            new_remaining,
        )
        if new_remaining > 0:
            active_after.append(
                TopupPack(
                ref=pack.ref,
                pack_id=pack.pack_id,
                credits_remaining=new_remaining,
                credits_reserved=pack.credits_reserved,
                expires_at=pack.expires_at,
                stripe_checkout_session_id=pack.stripe_checkout_session_id,
            )
            )
    return consumed, _sort_active_packs(active_after)


def topup_aggregate_fields(active_packs: list[TopupPack]) -> dict[str, Any]:
    sorted_packs = _sort_active_packs(active_packs)
    total_remaining = sum(pack.credits_remaining for pack in sorted_packs)
    total_reserved = sum(pack.credits_reserved for pack in sorted_packs)
    return {
        "topupCredits.totalRemaining": total_remaining,
        "topupCredits.totalReserved": total_reserved,
        "topupCredits.totalAvailable": max(0, total_remaining - total_reserved),
        "topupCredits.activePackCount": len(sorted_packs),
        "topupCredits.earliestExpiresAt": sorted_packs[0].expires_at if sorted_packs else None,
    }


def _allocation_amounts_by_pack(allocations: list[dict[str, Any]]) -> dict[str, int]:
    amounts: dict[str, int] = {}
    for allocation in allocations:
        pack_id = allocation.get("packId")
        if not isinstance(pack_id, str) or not pack_id:
            continue
        amounts[pack_id] = amounts.get(pack_id, 0) + max(0, int(allocation.get("credits", 0) or 0))
    return amounts


def _create_checkout_hold(db: Any, uid: str, config: BillingConfig) -> tuple[str, int, datetime]:
    hold_id = f"topup_hold_{uuid.uuid4().hex}"
    hold_ref = db.collection("topup_checkout_holds").document(hold_id)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=config.topup_checkout_hold_ttl_minutes)

    @firestore.transactional
    def _transactional_create(transaction):
        pending_slots, expired_hold_refs = _pending_hold_slots(transaction, db, uid, now)
        pack_state = refresh_topup_pack_state_in_transaction(
            transaction,
            db,
            uid,
            now,
            expire_stale=True,
        )
        remaining_slots = config.topup_max_active_packs - pack_state.active_pack_count - pending_slots
        if remaining_slots <= 0:
            raise BillingHttpError(409, "Maximum 3 active credit packs reached.")
        for expired_hold_ref in expired_hold_refs:
            transaction.update(
                expired_hold_ref,
                {
                    "status": "expired",
                    "expiredAt": now,
                },
            )
        transaction.set(
            hold_ref,
            {
                "userId": uid,
                "holdId": hold_id,
                "packKey": TOPUP_PACK_KEY,
                "remainingSlots": remaining_slots,
                "status": "pending",
                "createdAt": now,
                "expiresAt": expires_at,
            },
        )
        user_ref = db.collection("users").document(uid)
        transaction.update(user_ref, topup_aggregate_fields(pack_state.active_packs))
        return remaining_slots

    return hold_id, int(_transactional_create(db.transaction())), expires_at


def _pending_hold_slots(transaction: Any, db: Any, uid: str, now: datetime) -> tuple[int, list[Any]]:
    snapshots = list(
        db.collection("topup_checkout_holds")
        .where(filter=firestore.FieldFilter("userId", "==", uid))
        .stream(transaction=transaction)
    )
    pending_slots = 0
    expired_hold_refs: list[Any] = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        if data.get("status") != "pending":
            continue
        expires_at = _to_utc_datetime(data.get("expiresAt"))
        if expires_at and expires_at <= now:
            expired_hold_refs.append(snapshot.reference)
            continue
        pending_slots += int(data.get("remainingSlots", 0) or 0)
    return pending_slots, expired_hold_refs


def _cancel_checkout_hold(db: Any, hold_id: str) -> None:
    db.collection("topup_checkout_holds").document(hold_id).set(
        {
            "status": "cancelled",
            "cancelledAt": datetime.now(timezone.utc),
        },
        merge=True,
    )


def _expire_checkout_session_if_open(client: Any, session_id: str) -> bool:
    sessions_api = getattr(getattr(client, "checkout", None), "sessions", None)
    if sessions_api is None or not hasattr(sessions_api, "expire"):
        return False
    try:
        session = sessions_api.expire(session_id)
    except Exception:
        logger.warning("topup_checkout_stripe_expire_failed session=%s", session_id, exc_info=True)
        return False
    return str(_object_get(session, "status") or "") == "expired"


def _resolve_topup_quantity(
    payload: dict[str, Any],
    config: BillingConfig,
    *,
    stripe_client: Any | None,
) -> int:
    line_items = _checkout_line_items_from_payload(payload)
    if not line_items:
        session_id = str(payload.get("id") or "")
        if not session_id:
            return 0
        line_items = _fetch_checkout_line_items(session_id, stripe_client=stripe_client)
    quantity = 0
    for item in line_items:
        price = _object_get(item, "price", {}) or {}
        if _object_get(price, "id") != config.stripe_price_topup_15:
            continue
        quantity += int(_object_get(item, "quantity", 0) or 0)
    return quantity


def _fetch_checkout_line_items(session_id: str, *, stripe_client: Any | None) -> list[Any]:
    raw_client = stripe_client or get_stripe_v1_client()
    client = getattr(raw_client, "v1", None) or raw_client
    sessions_api = getattr(getattr(client, "checkout", None), "sessions", None)
    if sessions_api is not None and hasattr(sessions_api, "list_line_items"):
        response = sessions_api.list_line_items(session_id, params={"limit": 100})
        line_items = _checkout_line_items_from_response(response)
        if line_items:
            return line_items

    try:
        import stripe
    except Exception:
        logger.warning("topup_checkout_line_items_fetch_unavailable session=%s", session_id)
        return []

    try:
        response = stripe.checkout.Session.list_line_items(session_id, limit=100)
    except Exception:
        logger.exception("topup_checkout_line_items_fetch_failed session=%s", session_id)
        return []
    return _checkout_line_items_from_response(response)


def _checkout_line_items_from_payload(payload: dict[str, Any]) -> list[Any]:
    return _checkout_line_items_from_response(payload.get("line_items"))


def _checkout_line_items_from_response(response: Any) -> list[Any]:
    data = _object_get(response, "data")
    return list(data or [])


def _object_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _assert_topup_session_belongs_to_user(uid: str, session: dict[str, Any]) -> None:
    metadata = session.get("metadata") or {}
    session_user_id = session.get("client_reference_id") or metadata.get("firebaseUserId")
    if session_user_id != uid:
        raise BillingHttpError(403, "Checkout session does not belong to the current user.")
    billing = get_billing_state(uid)
    stored_customer_id = billing.get("stripeCustomerId")
    session_customer_id = session.get("customer")
    if stored_customer_id and session_customer_id and stored_customer_id != session_customer_id:
        raise BillingHttpError(403, "Checkout session does not belong to the current user.")


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    to_dict_recursive = getattr(value, "to_dict_recursive", None)
    if callable(to_dict_recursive):
        result = to_dict_recursive()
        if isinstance(result, dict):
            return result
    private_recursive = getattr(value, "_to_dict_recursive", None)
    if callable(private_recursive):
        result = private_recursive()
        if isinstance(result, dict):
            return result
    return dict(value)


def _resolve_checkout_uid(payload: dict[str, Any]) -> str | None:
    client_reference_id = payload.get("client_reference_id")
    if isinstance(client_reference_id, str) and client_reference_id.strip():
        return client_reference_id.strip()
    metadata = payload.get("metadata") or {}
    firebase_user_id = metadata.get("firebaseUserId")
    if isinstance(firebase_user_id, str) and firebase_user_id.strip():
        return firebase_user_id.strip()
    return None


def _to_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sort_active_packs(packs: list[TopupPack]) -> list[TopupPack]:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(packs, key=lambda pack: pack.expires_at or far_future)


def _safe_doc_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)
    return cleaned or uuid.uuid4().hex


def _stripe_id_from_expandable(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value_id = value.get("id")
        return value_id if isinstance(value_id, str) else None
    return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
