from __future__ import annotations

"""Backend credit management service using Firestore transactions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Mapping, Optional
import hashlib
import json
import logging
import math
import uuid

from google.cloud import firestore
from src.backend.firebase_app import get_firestore_client
from src.backend.config import Settings
from src.backend.billing_migration import FREE_TIER_MONTHLY_ALLOWANCE, ensure_billing_state_for_login
from src.backend.billing_topup import (
    TopupPack,
    consume_reserved_topup_credits_in_transaction,
    consume_topup_credits_in_transaction,
    release_reserved_topup_credits_in_transaction,
    reserve_topup_credits_in_transaction,
    refresh_topup_pack_state_in_transaction,
    topup_aggregate_fields,
)
from src.backend.message_catalog import backend_message
from src.backend.synthesis_pricing import (
    SYNTHESIS_PRICING_VERSION,
    VOCAL_CREDIT_DURATION_SECONDS,
    SynthesisCreditBreakdown,
    calculate_synthesis_credit_breakdown,
    credits_for_duration,
)
from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

# Constants
CREDIT_DURATION_SECONDS = VOCAL_CREDIT_DURATION_SECONDS
EXPORT_MIX_CREDIT_DURATION_SECONDS = 60
_CREDIT_DURATION_PRECISION_SECONDS = 0.001
FREE_TIER_CREDIT_AMOUNT = FREE_TIER_MONTHLY_ALLOWANCE
TRIAL_CREDIT_AMOUNT = FREE_TIER_CREDIT_AMOUNT
TRIAL_EXPIRY_DAYS = 30
DEFAULT_RESERVATION_TTL_SECONDS = 60 * 60


def _days_since(now: datetime, previous: Any) -> int:
    """Return whole days since a stored datetime-like value, or a large number if absent."""
    if not isinstance(previous, datetime):
        return 999999
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return (now - previous).days


def _feedback_candidate_update(
    *,
    now: datetime,
    user_data: Dict[str, Any],
    cooldown_days: int,
    min_successful_generations: int,
) -> tuple[dict[str, Any], bool]:
    """Return user feedback counter updates and whether the completed job is a prompt candidate."""
    feedback = user_data.get("feedback") if isinstance(user_data.get("feedback"), dict) else {}
    successful_generations = int(feedback.get("successfulGenerationsSinceLastPrompt", 0) or 0) + 1
    last_prompt_at = feedback.get("lastPromptAt")
    last_submitted_at = feedback.get("lastSubmittedAt")
    has_prior_prompt_cycle = isinstance(last_prompt_at, datetime) or isinstance(
        last_submitted_at,
        datetime,
    )
    required_generations = min_successful_generations if has_prior_prompt_cycle else 1
    is_candidate = False
    if successful_generations >= required_generations:
        days_since_prompt = _days_since(now, last_prompt_at)
        days_since_submit = _days_since(now, last_submitted_at)
        is_candidate = days_since_prompt >= cooldown_days and days_since_submit >= cooldown_days
    return {
        "feedback.successfulGenerationsSinceLastPrompt": successful_generations,
    }, is_candidate

@dataclass(frozen=True)
class UserCredits:
    balance: int
    reserved: int
    expires_at: Optional[datetime]
    overdrafted: bool
    trial_granted_at: Optional[datetime] = None
    trial_reset_v1: bool = False
    monthly_allowance: Optional[int] = None
    topup_total_remaining: int = 0
    topup_total_reserved: int = 0
    topup_total_available: int = 0
    topup_active_pack_count: int = 0
    topup_earliest_expires_at: Optional[datetime] = None
    last_grant_type: Optional[str] = None
    last_grant_at: Optional[datetime] = None
    last_grant_invoice_id: Optional[str] = None

    @property
    def available_balance(self) -> int:
        topup_available = (
            self.topup_total_available
            if self.topup_total_available or self.topup_total_reserved
            else max(0, self.topup_total_remaining - self.topup_total_reserved)
        )
        return self.balance - self.reserved + topup_available

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
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


def instrumental_charge_scope(uid: str, session_id: str, score_id: str) -> str:
    """Return the stable once-per-upload instrumental billing scope."""
    return f"{uid}/{session_id}/{score_id}"


def _instrumental_charge_doc_id(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def get_instrumental_charge_state(scope: Optional[str]) -> str:
    if not scope:
        return "unpaid"
    snapshot = (
        get_firestore_client()
        .collection("instrumental_generation_charges")
        .document(_instrumental_charge_doc_id(scope))
        .get()
    )
    if not snapshot.exists:
        return "unpaid"
    return str((snapshot.to_dict() or {}).get("status") or "unpaid")


def canonical_render_choices_hash(choices: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(choices), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_synthesis_quote(
    *,
    user_id: str,
    session_id: str,
    score_id: str,
    score_version_no: int,
    render_choices: Mapping[str, Any],
    estimate: Mapping[str, Any],
    ui_voicebank_at_quote: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist an immutable server-priced quote for prompt-enforced confirmation."""
    quote_id = uuid.uuid4().hex
    render_hash = canonical_render_choices_hash(render_choices)
    now = datetime.now(timezone.utc)
    instrumentals = estimate.get("instrumentals")
    instrumentals = instrumentals if isinstance(instrumentals, Mapping) else {}
    vocal = estimate.get("vocal_part")
    vocal = vocal if isinstance(vocal, Mapping) else {}
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "scoreId": score_id,
        "scoreVersionNo": int(score_version_no),
        "pricingVersion": int(
            estimate.get("pricing_version", SYNTHESIS_PRICING_VERSION)
        ),
        "renderChoicesHash": render_hash,
        "renderChoices": dict(render_choices),
        "partId": estimate.get("vocal_part_id"),
        "expandRepeats": bool(estimate.get("expand_repeats", False)),
        "vocalDurationSeconds": float(estimate.get("vocal_duration_seconds", 0.0)),
        "vocalPartCredits": int(vocal.get("estimated_credits", 0) or 0),
        "instrumentalCredits": int(instrumentals.get("estimated_credits", 0) or 0),
        "totalEstimatedCredits": int(estimate.get("total_estimated_credits", 0) or 0),
        "instrumentalChargeScope": instrumentals.get("charge_scope"),
        "instrumentalChargeRequired": bool(instrumentals.get("charge_required", False)),
        "instrumentalPricingDurationSeconds": instrumentals.get("pricing_duration_seconds"),
        "instrumentalPricingExpandRepeats": instrumentals.get("pricing_expand_repeats"),
        "billingComponents": list(estimate.get("billing_components") or ["vocal"]),
        # The UI voice selected when this price was presented. Confirmation compares
        # the selection then against the selection now; the quote's own voicebank
        # may legitimately differ from both when the user accepted a suggestion.
        "uiVoicebankAtQuote": ui_voicebank_at_quote,
        "status": "quoted",
        "createdAt": now,
        "expiresAt": now + timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS),
    }
    get_firestore_client().collection("synthesis_quotes").document(quote_id).create(payload)
    return {"quote_id": quote_id, "render_choices_hash": render_hash, **payload}


def ui_voicebank_conflicts_with_quote(
    quote: Optional[Mapping[str, Any]], forced_voicebank_id: Optional[str]
) -> bool:
    """Say whether the UI voice moved away from what the quote priced.

    Two separate properties are at stake. The render-choices hash guarantees the
    take matches what was priced. This guards the other one: that the take matches
    what the user currently wants. A UI change is deliberate evidence of intent,
    but only when it disagrees with the quote -- accepting a suggested voice leaves
    a stale UI selection that must not block the render.
    """
    if not isinstance(quote, Mapping):
        return False
    ui_at_quote = quote.get("uiVoicebankAtQuote") or None
    ui_now = forced_voicebank_id or None
    if ui_at_quote == ui_now:
        # The dropdown has not moved since the price was presented.
        return False
    if not ui_now:
        # The selection was cleared back to "recommended"; the quoted voice stands.
        return False
    bound = str((quote.get("renderChoices") or {}).get("voicebank") or "") or None
    # Selecting exactly the quoted voice agrees with the quote, so it is no conflict.
    return bound is not None and ui_now != bound


def active_synthesis_quote_context(
    quote_id: Optional[str], *, user_id: str, session_id: str
) -> Optional[Dict[str, Any]]:
    """Summarize a still-confirmable quote for the LLM prompt.

    Looked up by id rather than queried: three equality filters plus an ordering
    would need a composite index, and this project declares none.
    """
    if not isinstance(quote_id, str) or not quote_id:
        return None
    quote = get_synthesis_quote(quote_id)
    if quote is None:
        return None
    if quote.get("userId") != user_id or quote.get("sessionId") != session_id:
        return None
    if str(quote.get("status") or "") != "quoted":
        return None
    expires_at = quote.get("expiresAt")
    if isinstance(expires_at, datetime) and expires_at <= datetime.now(timezone.utc):
        return None
    return {
        "quote_id": quote.get("quote_id"),
        "part_id": quote.get("partId"),
        "expand_repeats": quote.get("expandRepeats"),
        "vocal_part_credits": quote.get("vocalPartCredits"),
        "instrumental_credits": quote.get("instrumentalCredits"),
        "total_estimated_credits": quote.get("totalEstimatedCredits"),
        "score_version_no": quote.get("scoreVersionNo"),
        # The exact choices the quote's hash binds. Without them the model cannot
        # tell a plain confirmation from a confirmation made after the user changed
        # something, such as the voice in the UI, and has to learn it by being
        # rejected.
        "bound_render_choices": dict(quote.get("renderChoices") or {}),
        "ui_voicebank_at_quote": quote.get("uiVoicebankAtQuote"),
        "instruction": (
            "This quote was already presented to the user and covers only "
            "bound_render_choices. If their latest message confirms it and nothing in "
            "bound_render_choices has changed, call synthesize once with this exact "
            "quote_id and those same choices. If anything differs -- a different "
            "voicebank selected in the UI, a changed repeat setting, part, lyric "
            "selection or language -- call prepare_synthesis_quote for the new choices "
            "instead and present that quote; the old one cannot authorize them."
        ),
    }


def get_synthesis_quote(quote_id: str) -> Optional[Dict[str, Any]]:
    snapshot = get_firestore_client().collection("synthesis_quotes").document(quote_id).get()
    if not snapshot.exists:
        return None
    return {"quote_id": quote_id, **(snapshot.to_dict() or {})}


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
class CompleteJobAndSettleCreditsResult:
    status: Literal[
        "completed_and_settled",
        "already_completed_and_settled",
        "reservation_missing",
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

def _user_credits_from_data(data: Dict[str, Any]) -> UserCredits:
    """Build the public credit view from an existing user document."""
    credits_data = data.get("credits") or {}
    topup_data = data.get("topupCredits") or {}
    return UserCredits(
        balance=int(credits_data.get("balance", 0) or 0),
        reserved=int(credits_data.get("reserved", 0) or 0),
        expires_at=credits_data.get("expiresAt"),
        overdrafted=bool(credits_data.get("overdrafted", False)),
        trial_granted_at=credits_data.get("trialGrantedAt"),
        trial_reset_v1=bool(credits_data.get("trial_reset_v1", False)),
        monthly_allowance=credits_data.get("monthlyAllowance"),
        topup_total_remaining=int(topup_data.get("totalRemaining", 0) or 0),
        topup_total_reserved=int(topup_data.get("totalReserved", 0) or 0),
        topup_total_available=int(
            topup_data.get(
                "totalAvailable",
                max(
                    0,
                    int(topup_data.get("totalRemaining", 0) or 0)
                    - int(topup_data.get("totalReserved", 0) or 0),
                ),
            )
            or 0
        ),
        topup_active_pack_count=int(topup_data.get("activePackCount", 0) or 0),
        topup_earliest_expires_at=topup_data.get("earliestExpiresAt"),
        last_grant_type=credits_data.get("lastGrantType"),
        last_grant_at=credits_data.get("lastGrantAt"),
        last_grant_invoice_id=credits_data.get("lastGrantInvoiceId"),
    )


def get_credits_by_user_id(uid: str) -> Optional[UserCredits]:
    """Read an existing user's canonical credit state without mutating it."""
    snapshot = get_firestore_client().collection("users").document(uid).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    return _user_credits_from_data(data)


def get_or_create_credits(uid: str, email: str) -> UserCredits:
    """Fetch user credits after ensuring billing bootstrap or migration is applied."""
    data = ensure_billing_state_for_login(uid, email)
    return _user_credits_from_data(data)

def estimate_credits(duration_seconds: float) -> int:
    """Calculate estimated credits for a given duration."""
    if duration_seconds <= 0:
        return 0
    return credits_for_duration(duration_seconds, CREDIT_DURATION_SECONDS)


def _settled_synthesis_breakdown(
    duration_seconds: float, *, include_instrumentals: bool
) -> SynthesisCreditBreakdown:
    """Price a settled take while tolerating a missing generated duration.

    The shared pricing primitive deliberately rejects a non-positive duration.
    Settlement must still publish a job whose reported duration is zero or
    absent, charging nothing, exactly as the previous scalar estimator did.
    """
    try:
        return calculate_synthesis_credit_breakdown(
            duration_seconds=duration_seconds,
            include_instrumentals=include_instrumentals,
        )
    except ValueError:
        return SynthesisCreditBreakdown(
            duration_seconds=0.0,
            vocal_part_credits=0,
            instrumental_credits=0,
            total_credits=0,
        )


def estimate_export_mix_credits(duration_seconds: float) -> int:
    """Calculate export-mix credits: 1 credit per started minute, minimum 1."""
    if duration_seconds <= 0:
        raise ValueError("Export mix duration must be positive.")
    normalized_duration = round(
        float(duration_seconds) / _CREDIT_DURATION_PRECISION_SECONDS
    ) * _CREDIT_DURATION_PRECISION_SECONDS
    return max(1, math.ceil(normalized_duration / EXPORT_MIX_CREDIT_DURATION_SECONDS))


def _billing_metadata_fields(
    *,
    session_id: Optional[str] = None,
    job_kind: Optional[str] = None,
    pricing: Optional[str] = None,
    pricing_unit_seconds: Optional[int] = None,
    billable_duration_seconds: Optional[float] = None,
    billing_reference_job_id: Optional[str] = None,
    score_id: Optional[str] = None,
    score_version_no: Optional[int] = None,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if score_id:
        fields.update(scoreId=score_id, scoreVersionNo=score_version_no)
    if session_id:
        fields["sessionId"] = session_id
    if job_kind:
        fields["jobKind"] = job_kind
    if pricing:
        fields["pricing"] = pricing
    if pricing_unit_seconds is not None:
        fields["pricingUnitSeconds"] = int(pricing_unit_seconds)
    if billable_duration_seconds is not None:
        fields["billableDurationSeconds"] = float(billable_duration_seconds)
    if billing_reference_job_id:
        fields["billingReferenceJobId"] = billing_reference_job_id
    return fields


@dataclass(frozen=True)
class _CreditSettlementAccounting:
    new_balance: int
    new_reserved: int
    subscription_consumed: int
    topup_consumed: int
    active_topup_after: list[TopupPack]
    overdrafted: bool


def _reservation_topup_allocations(res_data: Dict[str, Any]) -> list[dict[str, Any]]:
    raw_allocations = res_data.get("reservedTopupPacks")
    if not isinstance(raw_allocations, list):
        return []
    allocations: list[dict[str, Any]] = []
    for raw in raw_allocations:
        if not isinstance(raw, dict):
            continue
        pack_id = raw.get("packId")
        credits = int(raw.get("credits", 0) or 0)
        if isinstance(pack_id, str) and pack_id and credits > 0:
            allocations.append({"packId": pack_id, "credits": credits})
    return allocations


def _reservation_has_split(res_data: Dict[str, Any]) -> bool:
    return "reservedMonthlyCredits" in res_data or "reservedTopupPacks" in res_data


def _settle_credit_accounting_in_transaction(
    *,
    transaction: Any,
    db: Any,
    uid: str,
    job_id: str,
    user_data: Dict[str, Any],
    res_data: Dict[str, Any],
    actual_credits: int,
    now: datetime,
) -> _CreditSettlementAccounting:
    credits = user_data.get("credits", {})
    balance = int(credits.get("balance", 0) or 0)
    reserved = int(credits.get("reserved", 0) or 0)
    estimated_credits = int(res_data.get("estimatedCredits", 0) or 0)
    topup_state = refresh_topup_pack_state_in_transaction(
        transaction,
        db,
        uid,
        now,
        expire_stale=False,
    )

    if not _reservation_has_split(res_data):
        new_reserved = max(0, reserved - estimated_credits)
        subscription_available = max(0, balance - new_reserved)
        from_subscription = min(actual_credits, subscription_available)
        topup_needed = actual_credits - from_subscription
        new_balance = balance - from_subscription
        topup_consumed, active_topup_after = consume_topup_credits_in_transaction(
            transaction,
            db,
            uid,
            job_id,
            topup_needed,
            topup_state.active_packs,
            now,
            subscription_balance_after=new_balance,
        )
        unmet_credits = max(0, topup_needed - topup_consumed)
        if unmet_credits:
            new_balance -= unmet_credits
        return _CreditSettlementAccounting(
            new_balance=new_balance,
            new_reserved=new_reserved,
            subscription_consumed=from_subscription,
            topup_consumed=topup_consumed,
            active_topup_after=active_topup_after,
            overdrafted=new_balance < 0,
        )

    reserved_monthly = max(0, int(res_data.get("reservedMonthlyCredits", 0) or 0))
    reserved_topup_allocations = _reservation_topup_allocations(res_data)
    reserved_topup = sum(allocation["credits"] for allocation in reserved_topup_allocations)
    new_reserved = max(0, reserved - reserved_monthly)

    remaining_actual = actual_credits
    from_reserved_monthly = min(remaining_actual, reserved_monthly)
    remaining_actual -= from_reserved_monthly
    new_balance = balance - from_reserved_monthly

    reserved_topup_to_consume = min(remaining_actual, reserved_topup)
    remaining_actual -= reserved_topup_to_consume
    topup_consumed, active_topup_after = consume_reserved_topup_credits_in_transaction(
        transaction,
        db,
        uid,
        job_id,
        reserved_topup_allocations,
        reserved_topup_to_consume,
        topup_state.active_packs,
        now,
        subscription_balance_after=new_balance,
    )

    extra_from_subscription = min(remaining_actual, max(0, new_balance - new_reserved))
    remaining_actual -= extra_from_subscription
    new_balance -= extra_from_subscription

    extra_topup_consumed, active_topup_after = consume_topup_credits_in_transaction(
        transaction,
        db,
        uid,
        job_id,
        remaining_actual,
        active_topup_after,
        now,
        subscription_balance_after=new_balance,
    )
    remaining_actual -= extra_topup_consumed
    topup_consumed += extra_topup_consumed

    if remaining_actual:
        new_balance -= remaining_actual

    return _CreditSettlementAccounting(
        new_balance=new_balance,
        new_reserved=new_reserved,
        subscription_consumed=from_reserved_monthly + extra_from_subscription,
        topup_consumed=topup_consumed,
        active_topup_after=active_topup_after,
        overdrafted=new_balance < 0,
    )


def reserve_credits(
    uid: str,
    job_id: str,
    estimated_credits: int,
    reservation_ttl_seconds: Optional[int] = None,
    *,
    session_id: Optional[str] = None,
    job_kind: Optional[str] = None,
    pricing: Optional[str] = None,
    pricing_unit_seconds: Optional[int] = None,
    billable_duration_seconds: Optional[float] = None,
    billing_reference_job_id: Optional[str] = None,
    score_id: Optional[str] = None,
    score_version_no: Optional[int] = None,
    quote_id: Optional[str] = None,
    render_choices_hash: Optional[str] = None,
    billing_components: Optional[list[str]] = None,
    vocal_estimated_credits: Optional[int] = None,
    instrumental_estimated_credits: Optional[int] = None,
    instrumental_charge_scope_value: Optional[str] = None,
) -> ReserveCreditsResult:
    """
    Atomically reserve credits for a job.
    Returns an explicit reservation outcome.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    quote_ref = (
        db.collection("synthesis_quotes").document(quote_id) if quote_id else None
    )
    requested_components = tuple(billing_components or ["vocal"])
    includes_instrumental = "instrumental" in requested_components
    charge_ref = (
        db.collection("instrumental_generation_charges").document(
            _instrumental_charge_doc_id(instrumental_charge_scope_value)
        )
        if includes_instrumental and instrumental_charge_scope_value
        else None
    )
    
    @firestore.transactional
    def _transactional_reserve(transaction):
        quote_data: Dict[str, Any] = {}
        if quote_ref is not None:
            quote_snapshot = quote_ref.get(transaction=transaction)
            if not quote_snapshot.exists:
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)
            quote_data = quote_snapshot.to_dict() or {}
            if (
                quote_data.get("userId") != uid
                or quote_data.get("sessionId") != session_id
                or quote_data.get("scoreId") != score_id
                or int(quote_data.get("scoreVersionNo", -1)) != int(score_version_no or -1)
                or int(quote_data.get("totalEstimatedCredits", -1)) != estimated_credits
                or (render_choices_hash and quote_data.get("renderChoicesHash") != render_choices_hash)
                or tuple(quote_data.get("billingComponents") or ["vocal"]) != requested_components
            ):
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)
            quote_status = str(quote_data.get("status") or "")
            quote_expires_at = quote_data.get("expiresAt")
            if isinstance(quote_expires_at, datetime) and quote_expires_at <= datetime.now(timezone.utc):
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)
            if quote_status == "reserved" and quote_data.get("reservedByJobId") != job_id:
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)
            if quote_status not in {"quoted", "reserved"}:
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)

        charge_data: Dict[str, Any] = {}
        if charge_ref is not None:
            charge_snapshot = charge_ref.get(transaction=transaction)
            charge_data = charge_snapshot.to_dict() if charge_snapshot.exists else {}
            charge_status = str((charge_data or {}).get("status") or "unpaid")
            if charge_status == "paid":
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)
            if charge_status == "pending" and charge_data.get("jobId") != job_id:
                return ReserveCreditsResult(status="infra_error", estimated_credits=estimated_credits)

        res_snapshot = res_ref.get(transaction=transaction)
        if res_snapshot.exists:
            res_data = res_snapshot.to_dict() or {}
            if (
                res_data.get("userId") == uid
                and int(res_data.get("estimatedCredits", 0)) == estimated_credits
                and res_data.get("quoteId") == quote_id
                and tuple(res_data.get("billingComponents") or ["vocal"]) == requested_components
                and (
                    charge_ref is None
                    or (
                        str((charge_data or {}).get("status") or "") == "pending"
                        and charge_data.get("jobId") == job_id
                    )
                )
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
            
        balance = int(credits.get("balance", 0) or 0)
        reserved = int(credits.get("reserved", 0) or 0)
        now = datetime.now(timezone.utc)
        topup_state = refresh_topup_pack_state_in_transaction(
            transaction,
            db,
            uid,
            now,
            expire_stale=True,
        )
        available = balance - reserved + topup_state.total_available
        
        if available < estimated_credits:
            logger.warning("Reservation rejected: user %s insufficient balance (%d available, %d requested)",
                           uid, available, estimated_credits)
            return ReserveCreditsResult(
                status="insufficient_balance",
                estimated_credits=estimated_credits,
            )

        monthly_available = max(0, balance - reserved)
        reserved_monthly_credits = min(estimated_credits, monthly_available)
        topup_to_reserve = estimated_credits - reserved_monthly_credits
        reserved_topup_credits, reserved_topup_packs, active_topup_after = reserve_topup_credits_in_transaction(
            transaction,
            topup_to_reserve,
            topup_state.active_packs,
        )
        if reserved_monthly_credits + reserved_topup_credits < estimated_credits:
            logger.error(
                "Reservation split failed: user %s only split %d/%d credits",
                uid,
                reserved_monthly_credits + reserved_topup_credits,
                estimated_credits,
            )
            return ReserveCreditsResult(
                status="infra_error",
                estimated_credits=estimated_credits,
            )
            
        transaction.update(
            user_ref,
            {
                "credits.reserved": reserved + reserved_monthly_credits,
                **topup_aggregate_fields(active_topup_after),
            },
        )
        
        # Create reservation record
        ttl_seconds = reservation_ttl_seconds or DEFAULT_RESERVATION_TTL_SECONDS
        metadata_fields = _billing_metadata_fields(
            session_id=session_id,
            job_kind=job_kind,
            score_id=score_id,
            score_version_no=score_version_no,
            pricing=pricing,
            pricing_unit_seconds=pricing_unit_seconds,
            billable_duration_seconds=billable_duration_seconds,
            billing_reference_job_id=billing_reference_job_id,
        )
        transaction.set(res_ref, {
            "jobId": job_id,
            "userId": uid,
            "estimatedCredits": estimated_credits,
            "reservedMonthlyCredits": reserved_monthly_credits,
            "reservedTopupCredits": reserved_topup_credits,
            "reservedTopupPacks": reserved_topup_packs,
            "createdAt": now,
            "expiresAt": now + timedelta(seconds=ttl_seconds),
            "status": "pending",
            "quoteId": quote_id,
            "renderChoicesHash": render_choices_hash,
            "billingComponents": list(requested_components),
            "components": {
                "vocal": {
                    "status": "pending",
                    "estimatedCredits": int(
                        vocal_estimated_credits
                        if vocal_estimated_credits is not None
                        else estimated_credits
                    ),
                },
                **(
                    {
                        "instrumental": {
                            "status": "pending",
                            "estimatedCredits": int(instrumental_estimated_credits or 0),
                            "chargeScope": instrumental_charge_scope_value,
                        }
                    }
                    if includes_instrumental
                    else {}
                ),
            },
            **metadata_fields,
        })

        logger.info(
            "synthesis_billing_reserved job=%s user=%s quote=%s components=%s "
            "estimated_total=%s estimated_vocal=%s estimated_instrumental=%s scope=%s",
            job_id,
            uid,
            quote_id,
            ",".join(requested_components),
            estimated_credits,
            vocal_estimated_credits
            if vocal_estimated_credits is not None
            else estimated_credits,
            int(instrumental_estimated_credits or 0),
            instrumental_charge_scope_value if includes_instrumental else None,
        )
        if quote_ref is not None:
            transaction.update(
                quote_ref,
                {"status": "reserved", "reservedByJobId": job_id, "reservedAt": now},
            )
        if charge_ref is not None:
            transaction.set(
                charge_ref,
                {
                    "status": "pending",
                    "scope": instrumental_charge_scope_value,
                    "userId": uid,
                    "sessionId": session_id,
                    "scoreId": score_id,
                    "jobId": job_id,
                    "quoteId": quote_id,
                    "estimatedCredits": int(instrumental_estimated_credits or 0),
                    "estimatedExpandRepeats": quote_data.get("instrumentalPricingExpandRepeats"),
                    "estimatedDurationSeconds": quote_data.get("instrumentalPricingDurationSeconds"),
                    "updatedAt": now,
                },
            )

        # Log to ledger for audit trail.
        ledger_ref = db.collection("credit_ledger").document(f"reserve_{job_id}")
        transaction.set(ledger_ref, {
            "userId": uid,
            "type": "reserve",
            "jobId": job_id,
            "amount": 0,
            "reservedDelta": reserved_monthly_credits,
            "reservedAfter": reserved + reserved_monthly_credits,
            "monthlyReservedDelta": reserved_monthly_credits,
            "monthlyReservedAfter": reserved + reserved_monthly_credits,
            "topupReservedDelta": reserved_topup_credits,
            "topupReservedAfter": sum(pack.credits_reserved for pack in active_topup_after),
            "totalReservedDelta": estimated_credits,
            "reservedTopupPacks": reserved_topup_packs,
            "balanceAfter": balance,
            "createdAt": now,
            **metadata_fields,
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
        now = datetime.now(timezone.utc)
        accounting = _settle_credit_accounting_in_transaction(
            transaction=transaction,
            db=db,
            uid=uid,
            job_id=job_id,
            user_data=user_data,
            res_data=res_data,
            actual_credits=actual_credits,
            now=now,
        )
        
        # Update user
        transaction.update(
            user_ref,
            {
                "credits.balance": accounting.new_balance,
                "credits.reserved": accounting.new_reserved,
                "credits.overdrafted": accounting.overdrafted,
                **topup_aggregate_fields(accounting.active_topup_after),
            },
        )
        
        # Update reservation
        transaction.update(
            res_ref,
            {
                "status": "settled",
                "actualCredits": actual_credits,
                "settledAt": now,
            },
        )
        
        # Log to ledger (optional but recommended in spec)
        ledger_ref = db.collection("credit_ledger").document(f"settle_{job_id}")
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "type": "settle",
                "jobId": job_id,
                **_billing_metadata_fields(
                    job_kind=res_data.get("jobKind"), score_id=res_data.get("scoreId"),
                    score_version_no=res_data.get("scoreVersionNo"),
                ),
                "amount": -actual_credits,
                "reservedDelta": -estimated_credits,
                "reservedAfter": accounting.new_reserved,
                "monthlyReservedDelta": -int(res_data.get("reservedMonthlyCredits", estimated_credits) or 0),
                "monthlyReservedAfter": accounting.new_reserved,
                "topupReservedDelta": -int(res_data.get("reservedTopupCredits", 0) or 0),
                "topupReservedAfter": sum(pack.credits_reserved for pack in accounting.active_topup_after),
                "balanceAfter": accounting.new_balance,
                "subscriptionCreditsConsumed": accounting.subscription_consumed,
                "topupCreditsConsumed": accounting.topup_consumed,
                "createdAt": now,
            },
        )
        
        return SettleCreditsResult(
            status="settled",
            actual_credits=actual_credits,
            overdrafted=accounting.overdrafted,
        )

    transaction = db.transaction()
    try:
        return _transactional_settle(transaction)
    except Exception:
        logger.exception("Error settling credits for user %s, job %s", uid, job_id)
        return SettleCreditsResult(
            status="infra_error",
            actual_credits=actual_credits,
            overdrafted=False,
        )


def settle_credits_and_complete_job(
    uid: str,
    job_id: str,
    session_id: str,
    actual_duration_seconds: float,
    *,
    output_path: Optional[str],
    audio_url: Optional[str],
    lossless_output_path: Optional[str] = None,
    performance_midi: Optional[Mapping[str, Any]] = None,
    performance_midi_paths: Optional[Mapping[str, Any]] = None,
    message: str = backend_message("job.take_ready"),
) -> CompleteJobAndSettleCreditsResult:
    """
    Atomically settle credits and publish the completed job.

    This is the user-visible publish boundary for synthesized audio:
    either the credit settlement and completed job document commit together,
    or neither does.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    job_ref = db.collection("jobs").document(job_id)
    default_breakdown = _settled_synthesis_breakdown(
        actual_duration_seconds, include_instrumentals=False
    )
    actual_credits = default_breakdown.total_credits
    settings = Settings.from_env()

    @firestore.transactional
    def _transactional_complete_and_settle(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if not res_snapshot.exists:
            logger.error("Complete-and-settle failed: reservation %s not found", job_id)
            return CompleteJobAndSettleCreditsResult(
                status="reservation_missing",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        res_data = res_snapshot.to_dict() or {}
        reservation_status = str(res_data.get("status") or "")
        if reservation_status == "settled":
            job_snapshot = job_ref.get(transaction=transaction)
            job_data = job_snapshot.to_dict() if job_snapshot.exists else {}
            if (
                isinstance(job_data, dict)
                and job_data.get("status") == "completed"
                and job_data.get("audioUrl") == audio_url
            ):
                logger.info(
                    "Complete-and-settle already committed for user %s, job %s",
                    uid,
                    job_id,
                )
                return CompleteJobAndSettleCreditsResult(
                    status="already_completed_and_settled",
                    actual_credits=int(res_data.get("actualCredits", actual_credits) or actual_credits),
                    overdrafted=False,
                )
            logger.warning(
                "Complete-and-settle found settled reservation without completed job: user=%s job=%s",
                uid,
                job_id,
            )
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=int(res_data.get("actualCredits", actual_credits) or actual_credits),
                overdrafted=False,
            )
        if reservation_status == "released":
            return CompleteJobAndSettleCreditsResult(
                status="already_released",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status == "reconciliation_required":
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status != "pending":
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        components = res_data.get("components")
        components = dict(components) if isinstance(components, dict) else {}
        includes_instrumental = "instrumental" in components
        actual_breakdown = _settled_synthesis_breakdown(
            actual_duration_seconds, include_instrumentals=includes_instrumental
        )
        actual_credits_for_job = actual_breakdown.total_credits
        instrumental_component = components.get("instrumental")
        instrumental_component = (
            dict(instrumental_component)
            if isinstance(instrumental_component, dict)
            else None
        )
        charge_ref = None
        charge_data: Dict[str, Any] = {}
        if instrumental_component is not None:
            charge_scope = instrumental_component.get("chargeScope")
            if not isinstance(charge_scope, str) or not charge_scope:
                return CompleteJobAndSettleCreditsResult(
                    status="reconciliation_required",
                    actual_credits=actual_credits_for_job,
                    overdrafted=False,
                )
            charge_ref = db.collection("instrumental_generation_charges").document(
                _instrumental_charge_doc_id(charge_scope)
            )
            charge_snapshot = charge_ref.get(transaction=transaction)
            charge_data = charge_snapshot.to_dict() if charge_snapshot.exists else {}
            if (
                str((charge_data or {}).get("status") or "") != "pending"
                or charge_data.get("jobId") != job_id
            ):
                return CompleteJobAndSettleCreditsResult(
                    status="reconciliation_required",
                    actual_credits=actual_credits_for_job,
                    overdrafted=False,
                )

        estimated_credits = int(res_data.get("estimatedCredits", 0) or 0)

        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            return CompleteJobAndSettleCreditsResult(
                status="infra_error",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        user_data = user_snapshot.to_dict() or {}
        now = datetime.now(timezone.utc)
        accounting = _settle_credit_accounting_in_transaction(
            transaction=transaction,
            db=db,
            uid=uid,
            job_id=job_id,
            user_data=user_data,
            res_data=res_data,
            actual_credits=actual_credits_for_job,
            now=now,
        )
        feedback_user_update, feedback_prompt_candidate = _feedback_candidate_update(
            now=now,
            user_data=user_data,
            cooldown_days=settings.feedback_prompt_cooldown_days,
            min_successful_generations=settings.feedback_prompt_min_successful_generations,
        )

        transaction.update(
            user_ref,
            {
                "credits.balance": accounting.new_balance,
                "credits.reserved": accounting.new_reserved,
                "credits.overdrafted": accounting.overdrafted,
                **topup_aggregate_fields(accounting.active_topup_after),
                **feedback_user_update,
            },
        )
        vocal_component = components.get("vocal")
        if isinstance(vocal_component, dict):
            components["vocal"] = {
                **vocal_component,
                "status": "settled",
                "actualCredits": actual_breakdown.vocal_part_credits,
            }
        if instrumental_component is not None:
            components["instrumental"] = {
                **instrumental_component,
                "status": "settled",
                "actualCredits": actual_breakdown.instrumental_credits,
            }
        transaction.update(
            res_ref,
            {
                "status": "settled",
                "actualCredits": actual_credits_for_job,
                "components": components,
                "settledAt": now,
            },
        )
        if charge_ref is not None:
            transaction.set(
                charge_ref,
                {
                    **charge_data,
                    "status": "paid",
                    "actualCredits": actual_breakdown.instrumental_credits,
                    "actualDurationSeconds": float(actual_duration_seconds),
                    "pricingExpandRepeats": charge_data.get("estimatedExpandRepeats"),
                    "paidAt": now,
                    "updatedAt": now,
                },
            )
        quote_id = res_data.get("quoteId")
        if isinstance(quote_id, str) and quote_id:
            transaction.update(
                db.collection("synthesis_quotes").document(quote_id),
                {"status": "consumed", "consumedByJobId": job_id, "consumedAt": now},
            )
        logger.info(
            "synthesis_billing_settled job=%s user=%s quote=%s components=%s "
            "estimated_total=%s actual_total=%s actual_vocal=%s actual_instrumental=%s "
            "actual_duration=%s instrumental_scope_paid=%s pricing_version=%s",
            job_id,
            uid,
            quote_id,
            ",".join(sorted(components)),
            estimated_credits,
            actual_credits_for_job,
            actual_breakdown.vocal_part_credits,
            actual_breakdown.instrumental_credits,
            float(actual_duration_seconds),
            charge_ref is not None,
            SYNTHESIS_PRICING_VERSION,
        )
        ledger_ref = db.collection("credit_ledger").document(f"settle_{job_id}")
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "sessionId": session_id,
                "type": "settle",
                "jobId": job_id,
                **_billing_metadata_fields(
                    job_kind=res_data.get("jobKind"), score_id=res_data.get("scoreId"),
                    score_version_no=res_data.get("scoreVersionNo"),
                ),
                "amount": -actual_credits_for_job,
                "reservedDelta": -estimated_credits,
                "reservedAfter": accounting.new_reserved,
                "monthlyReservedDelta": -int(res_data.get("reservedMonthlyCredits", estimated_credits) or 0),
                "monthlyReservedAfter": accounting.new_reserved,
                "topupReservedDelta": -int(res_data.get("reservedTopupCredits", 0) or 0),
                "topupReservedAfter": sum(pack.credits_reserved for pack in accounting.active_topup_after),
                "balanceAfter": accounting.new_balance,
                "subscriptionCreditsConsumed": accounting.subscription_consumed,
                "topupCreditsConsumed": accounting.topup_consumed,
                "createdAt": now,
            },
        )
        job_payload: Dict[str, Any] = {
            "status": "completed",
            "completedAt": now,
            "step": "done",
            "message": message,
            "progress": 1.0,
            "actualDurationSeconds": float(actual_duration_seconds),
            "consumedCredits": actual_credits_for_job,
            "creditBreakdown": {
                "estimated": {
                    "vocal_part_credits": int(
                        (components.get("vocal") or {}).get("estimatedCredits", 0) or 0
                    ),
                    "instrumental_credits": int(
                        (components.get("instrumental") or {}).get("estimatedCredits", 0) or 0
                    ),
                    "total_credits": estimated_credits,
                },
                "actual": {
                    "duration_seconds": float(actual_duration_seconds),
                    "vocal_part_credits": actual_breakdown.vocal_part_credits,
                    "instrumental_credits": actual_breakdown.instrumental_credits,
                    "total_credits": actual_credits_for_job,
                },
                "quote_id": res_data.get("quoteId"),
                "pricing_version": SYNTHESIS_PRICING_VERSION,
            },
            "updatedAt": now,
        }
        if performance_midi is not None:
            job_payload["performanceMidi"] = dict(performance_midi)
        if performance_midi_paths is not None:
            job_payload["performanceMidiPaths"] = dict(performance_midi_paths)
        if output_path:
            job_payload["outputPath"] = output_path
        if lossless_output_path:
            job_payload["losslessOutputPath"] = lossless_output_path
            job_payload["losslessAudioFormat"] = "wav"
        if audio_url:
            job_payload["audioUrl"] = audio_url
        if feedback_prompt_candidate:
            job_payload["feedback"] = {
                "promptCandidate": True,
                "prompted": False,
                "submitted": False,
            }
        transaction.set(job_ref, job_payload, merge=True)

        return CompleteJobAndSettleCreditsResult(
            status="completed_and_settled",
            actual_credits=actual_credits_for_job,
            overdrafted=accounting.overdrafted,
        )

    transaction = db.transaction()
    try:
        return _transactional_complete_and_settle(transaction)
    except Exception:
        logger.exception(
            "Error settling credits and completing job for user %s, job %s",
            uid,
            job_id,
        )
        return CompleteJobAndSettleCreditsResult(
            status="infra_error",
            actual_credits=actual_credits,
            overdrafted=False,
        )


def settle_export_mix_credits_and_complete_job(
    uid: str,
    job_id: str,
    session_id: str,
    billable_duration_seconds: float,
    *,
    actual_duration_seconds: Optional[float] = None,
    output_path: Optional[str],
    audio_url: Optional[str],
    mix_metadata: Optional[Dict[str, Any]] = None,
) -> CompleteJobAndSettleCreditsResult:
    """Atomically settle export-mix credits and publish the completed mix job."""
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    job_ref = db.collection("jobs").document(job_id)
    actual_credits = estimate_export_mix_credits(billable_duration_seconds)

    @firestore.transactional
    def _transactional_complete_and_settle_export_mix(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if not res_snapshot.exists:
            logger.error("Export-mix complete-and-settle failed: reservation %s not found", job_id)
            return CompleteJobAndSettleCreditsResult(
                status="reservation_missing",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        res_data = res_snapshot.to_dict() or {}
        reservation_status = str(res_data.get("status") or "")
        if reservation_status == "settled":
            job_snapshot = job_ref.get(transaction=transaction)
            job_data = job_snapshot.to_dict() if job_snapshot.exists else {}
            if (
                isinstance(job_data, dict)
                and job_data.get("status") == "completed"
                and job_data.get("audioUrl") == audio_url
            ):
                return CompleteJobAndSettleCreditsResult(
                    status="already_completed_and_settled",
                    actual_credits=int(res_data.get("actualCredits", actual_credits) or actual_credits),
                    overdrafted=False,
                )
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=int(res_data.get("actualCredits", actual_credits) or actual_credits),
                overdrafted=False,
            )
        if reservation_status == "released":
            return CompleteJobAndSettleCreditsResult(
                status="already_released",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status == "reconciliation_required":
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )
        if reservation_status != "pending":
            return CompleteJobAndSettleCreditsResult(
                status="reconciliation_required",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        estimated_credits = int(res_data.get("estimatedCredits", 0) or 0)
        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            return CompleteJobAndSettleCreditsResult(
                status="infra_error",
                actual_credits=actual_credits,
                overdrafted=False,
            )

        user_data = user_snapshot.to_dict() or {}
        now = datetime.now(timezone.utc)
        accounting = _settle_credit_accounting_in_transaction(
            transaction=transaction,
            db=db,
            uid=uid,
            job_id=job_id,
            user_data=user_data,
            res_data=res_data,
            actual_credits=actual_credits,
            now=now,
        )
        pricing = str(res_data.get("pricing") or "export_mix_v1")
        pricing_unit_seconds = int(
            res_data.get("pricingUnitSeconds") or EXPORT_MIX_CREDIT_DURATION_SECONDS
        )
        billing_reference_job_id = res_data.get("billingReferenceJobId")
        billable_duration = float(
            res_data.get("billableDurationSeconds") or billable_duration_seconds
        )
        metadata_fields = _billing_metadata_fields(
            session_id=session_id,
            job_kind="export_mix",
            score_id=res_data.get("scoreId"),
            score_version_no=res_data.get("scoreVersionNo"),
            pricing=pricing,
            pricing_unit_seconds=pricing_unit_seconds,
            billable_duration_seconds=billable_duration,
            billing_reference_job_id=billing_reference_job_id
            if isinstance(billing_reference_job_id, str)
            else None,
        )

        transaction.update(
            user_ref,
            {
                "credits.balance": accounting.new_balance,
                "credits.reserved": accounting.new_reserved,
                "credits.overdrafted": accounting.overdrafted,
                **topup_aggregate_fields(accounting.active_topup_after),
            },
        )
        transaction.update(
            res_ref,
            {
                "status": "settled",
                "actualCredits": actual_credits,
                "settledAt": now,
            },
        )
        ledger_ref = db.collection("credit_ledger").document(f"settle_{job_id}")
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "type": "settle",
                "jobId": job_id,
                "amount": -actual_credits,
                "reservedDelta": -estimated_credits,
                "reservedAfter": accounting.new_reserved,
                "monthlyReservedDelta": -int(res_data.get("reservedMonthlyCredits", estimated_credits) or 0),
                "monthlyReservedAfter": accounting.new_reserved,
                "topupReservedDelta": -int(res_data.get("reservedTopupCredits", 0) or 0),
                "topupReservedAfter": sum(pack.credits_reserved for pack in accounting.active_topup_after),
                "balanceAfter": accounting.new_balance,
                "subscriptionCreditsConsumed": accounting.subscription_consumed,
                "topupCreditsConsumed": accounting.topup_consumed,
                "createdAt": now,
                **metadata_fields,
            },
        )
        billing_payload = {
            "pricing": pricing,
            "pricingUnitSeconds": pricing_unit_seconds,
            "billingReferenceJobId": billing_reference_job_id,
            "billableDurationSeconds": billable_duration,
            "requiredCredits": estimated_credits,
            "consumedCredits": actual_credits,
            "reservationStatus": "settled",
        }
        job_payload: Dict[str, Any] = {
            "status": "completed",
            "completedAt": now,
            "step": "done",
            "progress": 1.0,
            "jobKind": "export_mix",
            "actualDurationSeconds": float(
                actual_duration_seconds
                if actual_duration_seconds is not None
                else billable_duration_seconds
            ),
            "consumedCredits": actual_credits,
            "billing": billing_payload,
            "updatedAt": now,
        }
        if output_path:
            job_payload["outputPath"] = output_path
        if audio_url:
            job_payload["audioUrl"] = audio_url
        if mix_metadata:
            job_payload["mix"] = dict(mix_metadata)
        transaction.set(job_ref, job_payload, merge=True)

        return CompleteJobAndSettleCreditsResult(
            status="completed_and_settled",
            actual_credits=actual_credits,
            overdrafted=accounting.overdrafted,
        )

    transaction = db.transaction()
    try:
        return _transactional_complete_and_settle_export_mix(transaction)
    except Exception:
        logger.exception(
            "Error settling export-mix credits and completing job for user %s, job %s",
            uid,
            job_id,
        )
        return CompleteJobAndSettleCreditsResult(
            status="infra_error",
            actual_credits=actual_credits,
            overdrafted=False,
        )


def release_credits(
    uid: str,
    job_id: str,
    *,
    terminal_job_fields: Optional[Mapping[str, Any]] = None,
) -> ReleaseCreditsResult:
    """
    Atomically release reserved credits for a failed or cancelled job.
    """
    db = get_firestore_client()
    user_ref = db.collection("users").document(uid)
    res_ref = db.collection("credit_reservations").document(job_id)
    job_ref = db.collection("jobs").document(job_id)

    def update_terminal_job(transaction) -> None:
        if terminal_job_fields is None:
            return
        payload = dict(terminal_job_fields)
        payload["updatedAt"] = firestore.SERVER_TIMESTAMP
        transaction.update(job_ref, payload)

    @firestore.transactional
    def _transactional_release(transaction):
        res_snapshot = res_ref.get(transaction=transaction)
        if not res_snapshot.exists:
            update_terminal_job(transaction)
            return ReleaseCreditsResult(status="reservation_missing")

        res_data = res_snapshot.to_dict() or {}
        reservation_status = str(res_data.get("status") or "")
        if reservation_status == "released":
            update_terminal_job(transaction)
            return ReleaseCreditsResult(status="already_released")
        if reservation_status == "settled":
            return ReleaseCreditsResult(status="already_settled")
        if reservation_status == "reconciliation_required":
            return ReleaseCreditsResult(status="reconciliation_required")
        if reservation_status != "pending":
            return ReleaseCreditsResult(status="reconciliation_required")

        components = res_data.get("components")
        components = dict(components) if isinstance(components, dict) else {}
        instrumental_component = components.get("instrumental")
        instrumental_component = (
            dict(instrumental_component)
            if isinstance(instrumental_component, dict)
            else None
        )
        charge_ref = None
        charge_data: Dict[str, Any] = {}
        if instrumental_component is not None:
            # A combined job releases its own pending instrumental claim in the
            # same transaction. Vocal-only jobs never enter this branch, so no
            # already-paid scope owned by an earlier job is ever touched here.
            charge_scope = instrumental_component.get("chargeScope")
            if isinstance(charge_scope, str) and charge_scope:
                charge_ref = db.collection("instrumental_generation_charges").document(
                    _instrumental_charge_doc_id(charge_scope)
                )
                charge_snapshot = charge_ref.get(transaction=transaction)
                charge_data = charge_snapshot.to_dict() if charge_snapshot.exists else {}
            if (
                charge_ref is not None
                and str((charge_data or {}).get("status") or "") == "pending"
                and charge_data.get("jobId") != job_id
            ):
                # Another job owns the claim. Releasing it here would hand this
                # job's rollback authority over another job's payment state.
                logger.error(
                    "Release blocked: instrumental claim for job %s is owned by job %s",
                    job_id,
                    charge_data.get("jobId"),
                )
                return ReleaseCreditsResult(status="reconciliation_required")
            if charge_ref is None or str((charge_data or {}).get("status") or "") != "pending":
                # This job's claim is absent or already resolved. The reserved
                # credits are still this job's to release, so release them
                # rather than stranding the balance, and leave the durable
                # scope exactly as it is.
                logger.warning(
                    "Releasing job %s without an owned pending instrumental claim (status=%s)",
                    job_id,
                    (charge_data or {}).get("status"),
                )
                charge_ref = None

        estimated_credits = int(res_data.get("estimatedCredits", 0) or 0)
        reserved_monthly_credits = int(
            res_data.get(
                "reservedMonthlyCredits",
                estimated_credits if not _reservation_has_split(res_data) else 0,
            )
            or 0
        )
        reserved_topup_packs = _reservation_topup_allocations(res_data)
        
        user_snapshot = user_ref.get(transaction=transaction)
        if not user_snapshot.exists:
            return ReleaseCreditsResult(status="infra_error")
            
        user_data = user_snapshot.to_dict() or {}
        credits = user_data.get("credits", {})
        reserved = int(credits.get("reserved", 0) or 0)
        now = datetime.now(timezone.utc)
        topup_state = refresh_topup_pack_state_in_transaction(
            transaction,
            db,
            uid,
            now,
            expire_stale=False,
        )
        active_topup_after = release_reserved_topup_credits_in_transaction(
            transaction,
            reserved_topup_packs,
            topup_state.active_packs,
        )
        
        transaction.update(
            user_ref,
            {
                "credits.reserved": max(0, reserved - reserved_monthly_credits),
                **topup_aggregate_fields(active_topup_after),
            },
        )

        vocal_component = components.get("vocal")
        if isinstance(vocal_component, dict):
            components["vocal"] = {**vocal_component, "status": "released"}
        if instrumental_component is not None:
            components["instrumental"] = {
                **instrumental_component,
                "status": "released",
            }
        transaction.update(
            res_ref,
            {
                "status": "released",
                "components": components,
                "releasedAt": now,
            },
        )
        if charge_ref is not None:
            transaction.set(
                charge_ref,
                {
                    "status": "unpaid",
                    "scope": instrumental_component.get("chargeScope"),
                    "userId": uid,
                    "sessionId": res_data.get("sessionId"),
                    "scoreId": res_data.get("scoreId"),
                    "releasedJobId": job_id,
                    "releasedAt": now,
                },
            )
        quote_id = res_data.get("quoteId")
        if isinstance(quote_id, str) and quote_id:
            transaction.update(
                db.collection("synthesis_quotes").document(quote_id),
                {"status": "expired", "releasedJobId": job_id, "releasedAt": now},
            )
        logger.info(
            "synthesis_billing_released job=%s user=%s quote=%s components=%s "
            "released_total=%s instrumental_claim_removed=%s",
            job_id,
            uid,
            quote_id,
            ",".join(sorted(components)),
            estimated_credits,
            charge_ref is not None,
        )

        # Log to ledger for audit trail.
        ledger_ref = db.collection("credit_ledger").document(f"release_{job_id}")
        metadata_fields = _billing_metadata_fields(
            session_id=res_data.get("sessionId"),
            job_kind=res_data.get("jobKind"),
            score_id=res_data.get("scoreId"),
            score_version_no=res_data.get("scoreVersionNo"),
            pricing=res_data.get("pricing"),
            pricing_unit_seconds=res_data.get("pricingUnitSeconds"),
            billable_duration_seconds=res_data.get("billableDurationSeconds"),
            billing_reference_job_id=res_data.get("billingReferenceJobId"),
        )
        transaction.set(
            ledger_ref,
            {
                "userId": uid,
                "type": "release",
                "jobId": job_id,
                "amount": 0,
                "reservedDelta": -reserved_monthly_credits,
                "reservedAfter": max(0, reserved - reserved_monthly_credits),
                "monthlyReservedDelta": -reserved_monthly_credits,
                "monthlyReservedAfter": max(0, reserved - reserved_monthly_credits),
                "topupReservedDelta": -int(
                    res_data.get("reservedTopupCredits", 0) or 0
                ),
                "topupReservedAfter": sum(
                    pack.credits_reserved for pack in active_topup_after
                ),
                "reservedTopupPacks": reserved_topup_packs,
                "balanceAfter": credits.get("balance", 0),
                "createdAt": now,
                **metadata_fields,
            },
        )
        update_terminal_job(transaction)

        return ReleaseCreditsResult(status="released")

    transaction = db.transaction()
    try:
        return _transactional_release(transaction)
    except Exception:
        logger.exception("Error releasing credits for user %s, job %s", uid, job_id)
        return ReleaseCreditsResult(status="infra_error")
