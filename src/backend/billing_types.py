from __future__ import annotations

"""Types shared by the Stripe billing integration."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

PlanKey = Literal[
    "free",
    "starter_monthly",
    "starter_annual",
    "solo_monthly",
    "solo_annual",
    "choir_early_monthly",
    "choir_early_annual",
    "choir_monthly",
    "choir_annual",
]

StripeSubscriptionStatus = Literal[
    "active",
    "past_due",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "unpaid",
]

PlanFamily = Literal["free", "starter", "solo", "choir"]
BillingInterval = Literal["none", "month", "year"]


class BillingState(TypedDict, total=False):
    stripeCustomerId: str | None
    stripeSubscriptionId: str | None
    stripeCheckoutSessionId: str | None
    activePlanKey: PlanKey
    stripeSubscriptionStatus: StripeSubscriptionStatus | None
    family: PlanFamily
    billingInterval: BillingInterval
    currentPeriodStart: datetime | None
    currentPeriodEnd: datetime | None
    cancelAtPeriodEnd: bool
    canceledAt: datetime | None
    latestInvoiceId: str | None
    latestInvoicePaidAt: datetime | None
    latestInvoicePaymentFailedAt: datetime | None
    isEarlySupporter: bool
    lastCreditRefreshAt: datetime | None
    nextCreditRefreshAt: datetime | None
    creditRefreshAnchor: datetime | None
    freeTierActivatedAt: datetime | None


class CreditsState(TypedDict, total=False):
    balance: int
    reserved: int
    overdrafted: bool
    expiresAt: datetime | None
    monthlyAllowance: int | None
    lastGrantType: str | None
    lastGrantAt: datetime | None
    lastGrantInvoiceId: str | None
    trialGrantedAt: datetime | None
    trial_reset_v1: bool


class AuthContext(TypedDict):
    uid: str
    email: str
    claims: dict[str, Any]


@dataclass(frozen=True)
class BillingHttpError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return self.detail
