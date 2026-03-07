from __future__ import annotations

"""Backend credit management service using Firestore transactions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional
import logging
import math

from google.cloud import firestore
from src.backend.firebase_app import get_firestore_client
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

# Constants
CREDIT_DURATION_SECONDS = 30
TRIAL_CREDIT_AMOUNT = 10
TRIAL_EXPIRY_DAYS = 14
DEFAULT_RESERVATION_TTL_SECONDS = 60 * 60

@dataclass(frozen=True)
class UserCredits:
    balance: int
    reserved: int
    expires_at: datetime
    overdrafted: bool
    trial_granted_at: Optional[datetime] = None

    @property
    def available_balance(self) -> int:
        return self.balance - self.reserved

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at


@dataclass(frozen=True)
class ReserveCreditsResult:
    status: Literal[
        "reserved",
        "insufficient_balance",
        "overdrafted",
        "expired",
        "reservation_exists",
        "infra_error",
    ]
    estimated_credits: int


@dataclass(frozen=True)
class SettleCreditsResult:
    status: Literal[
        "settled",
        "reservation_missing",
        "already_settled",
        "already_released",
        "reconciliation_required",
        "infra_error",
    ]
    actual_credits: int
    overdrafted: bool


@dataclass(frozen=True)
class ReleaseCreditsResult:
    status: Literal[
        "released",
        "reservation_missing",
        "already_settled",
        "already_released",
        "reconciliation_required",
        "infra_error",
    ]


def mark_reservation_reconciliation_required(
    uid: str,
    job_id: str,
    *,
    last_error: str,
    last_error_message: str,
) -> bool:
    """Best-effort marker for reservations that need later billing repair."""
    db = get_firestore_client()
    res_ref = db.collection("credit_reservations").document(job_id)
    try:
        snapshot = res_ref.get()
        if not snapshot.exists:
            logger.error(
                "Cannot mark missing reservation as reconciliation_required: user=%s job=%s",
                uid,
                job_id,
            )
            return False
        now = datetime.now(timezone.utc)
        res_ref.set(
            {
                "status": "reconciliation_required",
                "lastError": last_error,
                "lastErrorMessage": last_error_message,
                "reconciliationAttemptedAt": now,
            },
            merge=True,
        )
        logger.warning(
            "Marked reservation as reconciliation_required: user=%s job=%s error=%s",
            uid,
            job_id,
            last_error,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to mark reservation as reconciliation_required: user=%s job=%s",
            uid,
            job_id,
        )
        return False

def get_or_create_credits(uid: str, email: str) -> UserCredits:
    """Fetch user credits, granting trial credits if first sign-in."""
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    
    @firestore.transactional
    def _transactional_get_or_create(transaction):
        snapshot = user_ref.get(transaction=transaction)
        
        if snapshot.exists:
            data = snapshot.to_dict() or {}
            credits_data = data.get("credits")
            if credits_data:
                return UserCredits(
                    balance=credits_data.get("balance", 0),
                    reserved=credits_data.get("reserved", 0),
                    expires_at=credits_data.get("expiresAt"),
                    overdrafted=credits_data.get("overdrafted", False),
                    trial_granted_at=credits_data.get("trialGrantedAt")
                )
        
        # Create trial credits
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=TRIAL_EXPIRY_DAYS)
        
        credits_data = {
            "balance": TRIAL_CREDIT_AMOUNT,
            "reserved": 0,
            "expiresAt": expires_at,
            "overdrafted": False,
            "trialGrantedAt": now
        }
        
        transaction.set(user_ref, {
            "email": email,
            "credits": credits_data,
            "createdAt": now
        }, merge=True)
        
        return UserCredits(
            balance=TRIAL_CREDIT_AMOUNT,
            reserved=0,
            expires_at=expires_at,
            overdrafted=False,
            trial_granted_at=now
        )

    transaction = db.transaction()
    return _transactional_get_or_create(transaction)

def estimate_credits(duration_seconds: float) -> int:
    """Calculate estimated credits for a given duration."""
    if duration_seconds <= 0:
        return 0
    return math.ceil(duration_seconds / CREDIT_DURATION_SECONDS)

def reserve_credits(
    uid: str,
    job_id: str,
    estimated_credits: int,
    reservation_ttl_seconds: Optional[int] = None,
) -> ReserveCreditsResult:
    """
    Atomically reserve credits for a job.
    Returns an explicit reservation outcome.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    
    @firestore.transactional
    def _transactional_reserve(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if res_snapshot.exists:
            res_data = res_snapshot.to_dict() or {}
            if (
                res_data.get("userId") == uid
                and int(res_data.get("estimatedCredits", 0)) == estimated_credits
            ):
                logger.info(
                    "Reservation already exists for user %s, job %s; treating as idempotent success",
                    uid,
                    job_id,
                )
                return ReserveCreditsResult(
                    status="reservation_exists",
                    estimated_credits=estimated_credits,
                )
            logger.error(
                "Reservation conflict for user %s, job %s; existing=%s requested=%s",
                uid,
                job_id,
                res_data,
                estimated_credits,
            )
            return ReserveCreditsResult(
                status="infra_error",
                estimated_credits=estimated_credits,
            )

        snapshot = user_ref.get(transaction=transaction)
        if not snapshot.exists:
            return ReserveCreditsResult(
                status="infra_error",
                estimated_credits=estimated_credits,
            )
            
        data = snapshot.to_dict() or {}
        credits = data.get("credits", {})
        
        if credits.get("overdrafted", False):
            logger.warning("Reservation rejected: user %s is overdrafted", uid)
            return ReserveCreditsResult(
                status="overdrafted",
                estimated_credits=estimated_credits,
            )
            
        expires_at = credits.get("expiresAt")
        if expires_at and datetime.now(timezone.utc) > expires_at:
            logger.warning("Reservation rejected: user %s credits expired", uid)
            return ReserveCreditsResult(
                status="expired",
                estimated_credits=estimated_credits,
            )
            
        balance = credits.get("balance", 0)
        reserved = credits.get("reserved", 0)
        
        if (balance - reserved) < estimated_credits:
            logger.warning("Reservation rejected: user %s insufficient balance (%d available, %d requested)", 
                           uid, balance - reserved, estimated_credits)
            return ReserveCreditsResult(
                status="insufficient_balance",
                estimated_credits=estimated_credits,
            )
            
        # Update user reserved amount
        transaction.update(user_ref, {
            "credits.reserved": reserved + estimated_credits
        })
        
        # Create reservation record
        now = datetime.now(timezone.utc)
        ttl_seconds = reservation_ttl_seconds or DEFAULT_RESERVATION_TTL_SECONDS
        transaction.set(res_ref, {
            "userId": uid,
            "estimatedCredits": estimated_credits,
            "createdAt": now,
            "expiresAt": now + timedelta(seconds=ttl_seconds),
            "status": "pending"
        })

        # Log to ledger for audit trail.
        ledger_ref = db.collection("credit_ledger").document()
        transaction.set(ledger_ref, {
            "userId": uid,
            "type": "reserve",
            "jobId": job_id,
            "amount": 0,
            "reservedDelta": estimated_credits,
            "reservedAfter": reserved + estimated_credits,
            "balanceAfter": balance,
            "createdAt": now
        })
        
        return ReserveCreditsResult(
            status="reserved",
            estimated_credits=estimated_credits,
        )

    transaction = db.transaction()
    try:
        return _transactional_reserve(transaction)
    except Exception:
        logger.exception("Error reserving credits for user %s, job %s", uid, job_id)
        return ReserveCreditsResult(
            status="infra_error",
            estimated_credits=estimated_credits,
        )

def settle_credits(uid: str, job_id: str, actual_duration_seconds: float) -> SettleCreditsResult:
    """
    Atomically settle credits for a job.
    Returns an explicit settlement outcome.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    
    actual_credits = estimate_credits(actual_duration_seconds)
    
    @firestore.transactional
    def _transactional_settle(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if not res_snapshot.exists:
            logger.error("Settlement failed: reservation %s not found", job_id)
            return SettleCreditsResult(
                status="reservation_missing",
                actual_credits=actual_credits,
                overdrafted=False,
            )
            
        res_data = res_snapshot.to_dict() or {}
        reservation_status = str(res_data.get("status") or "")
        if reservation_status == "settled":
            logger.info("Settlement skipped: reservation %s already settled", job_id)
            return SettleCreditsResult(
                status="already_settled",
                actual_credits=int(res_data.get("actualCredits", actual_credits) or actual_credits),
                overdrafted=False,
            )
        if reservation_status == "released":
            logger.warning("Settlement skipped: reservation %s already released", job_id)
            return SettleCreditsResult(
                status="already_released",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status == "reconciliation_required":
            logger.warning("Settlement blocked: reservation %s requires reconciliation", job_id)
            return SettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status != "pending":
            logger.warning("Settlement skipped: reservation %s is %s", job_id, reservation_status)
            return SettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )
            
        estimated_credits = res_data.get("estimatedCredits", 0)
        
        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            return SettleCreditsResult(
                status="infra_error",
                actual_credits=actual_credits,
                overdrafted=False,
            )
            
        user_data = user_snapshot.to_dict() or {}
        credits = user_data.get("credits", {})
        
        balance = credits.get("balance", 0)
        reserved = credits.get("reserved", 0)
        
        new_balance = balance - actual_credits
        new_reserved = max(0, reserved - estimated_credits)
        overdrafted = new_balance < 0
        
        # Update user
        transaction.update(user_ref, {
            "credits.balance": new_balance,
            "credits.reserved": new_reserved,
            "credits.overdrafted": overdrafted
        })
        
        # Update reservation
        transaction.update(res_ref, {
            "status": "settled",
            "actualCredits": actual_credits,
            "settledAt": datetime.now(timezone.utc)
        })
        
        # Log to ledger (optional but recommended in spec)
        ledger_ref = db.collection("credit_ledger").document()
        transaction.set(ledger_ref, {
            "userId": uid,
            "type": "settle",
            "jobId": job_id,
            "amount": -actual_credits,
            "reservedDelta": -estimated_credits,
            "reservedAfter": new_reserved,
            "balanceAfter": new_balance,
            "createdAt": datetime.now(timezone.utc)
        })
        
        return SettleCreditsResult(
            status="settled",
            actual_credits=actual_credits,
            overdrafted=overdrafted,
        )

    transaction = db.transaction()
    try:
        return _transactional_settle(transaction)
    except Exception as exc:
        logger.exception("Error settling credits for user %s, job %s", uid, job_id)
        marked = mark_reservation_reconciliation_required(
            uid,
            job_id,
            last_error="settle_failed",
            last_error_message=str(exc),
        )
        return SettleCreditsResult(
            status="reconciliation_required" if marked else "infra_error",
            actual_credits=actual_credits,
            overdrafted=False,
        )

def release_credits(uid: str, job_id: str) -> ReleaseCreditsResult:
    """
    Atomically release reserved credits for a failed or cancelled job.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    
    @firestore.transactional
    def _transactional_release(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if not res_snapshot.exists:
            return ReleaseCreditsResult(status="reservation_missing")
            
        res_data = res_snapshot.to_dict() or {}
        reservation_status = str(res_data.get("status") or "")
        if reservation_status == "released":
            return ReleaseCreditsResult(status="already_released")
        if reservation_status == "settled":
            return ReleaseCreditsResult(status="already_settled")
        if reservation_status == "reconciliation_required":
            return ReleaseCreditsResult(status="reconciliation_required")
        if reservation_status != "pending":
            return ReleaseCreditsResult(status="reconciliation_required")
            
        estimated_credits = res_data.get("estimatedCredits", 0)
        
        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            return ReleaseCreditsResult(status="infra_error")
            
        user_data = user_snapshot.to_dict() or {}
        credits = user_data.get("credits", {})
        reserved = credits.get("reserved", 0)
        
        # Update user
        transaction.update(user_ref, {
            "credits.reserved": max(0, reserved - estimated_credits)
        })
        
        # Update reservation
        transaction.update(res_ref, {
            "status": "released",
            "releasedAt": datetime.now(timezone.utc)
        })

        # Log to ledger for audit trail.
        ledger_ref = db.collection("credit_ledger").document()
        transaction.set(ledger_ref, {
            "userId": uid,
            "type": "release",
            "jobId": job_id,
            "amount": 0,
            "reservedDelta": -estimated_credits,
            "reservedAfter": max(0, reserved - estimated_credits),
            "balanceAfter": credits.get("balance", 0),
            "createdAt": datetime.now(timezone.utc)
        })
        
        return ReleaseCreditsResult(status="released")

    transaction = db.transaction()
    try:
        return _transactional_release(transaction)
    except Exception as exc:
        logger.exception("Error releasing credits for user %s, job %s", uid, job_id)
        marked = mark_reservation_reconciliation_required(
            uid,
            job_id,
            last_error="release_failed",
            last_error_message=str(exc),
        )
        return ReleaseCreditsResult(
            status="reconciliation_required" if marked else "infra_error"
        )
