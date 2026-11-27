import { useEffect, useState } from "react";
import { collection, doc, onSnapshot, query, where } from "firebase/firestore";
import { db } from "../firebase";
import { useAuth } from "./useAuth";
import type { BillingPlanKey, PlanFamily } from "../billing/plans";
import { isBillingPlanKey } from "../billing/plans";

type FirestoreTimestampLike = {
  toDate?: () => Date;
};

export type BillingTopupPack = {
  id: string;
  creditsAvailable: number;
  creditsRemaining: number;
  creditsReserved: number;
  expiresAt: Date | null;
};

/** A plan change the Stripe portal booked for the renewal date. */
export type ScheduledPlanChange = {
  planKey: BillingPlanKey;
  at: Date;
};

export type BillingState = {
  activePlanKey: BillingPlanKey;
  family: PlanFamily;
  billingInterval: "none" | "month" | "year";
  stripeSubscriptionStatus: string | null;
  latestInvoiceId: string | null;
  latestInvoiceStatus: string | null;
  latestPaymentIntentStatus: string | null;
  latestPaymentFailureCode: string | null;
  latestPaymentFailureMessage: string | null;
  stripeCustomerId: string | null;
  cancelAtPeriodEnd: boolean;
  currentPeriodEnd: Date | null;
  nextCreditRefreshAt: Date | null;
  scheduledChange: ScheduledPlanChange | null;
  monthlyAllowance: number;
  lastGrantAt: Date | null;
  availableCredits: number;
  reservedCredits: number;
  subscriptionCredits: number;
  topupCredits: number;
  topupActivePackCount: number;
  topupEarliestExpiresAt: Date | null;
  topupPacks: BillingTopupPack[];
  overdrafted: boolean;
  isExpired: boolean;
  loading: boolean;
  error: string | null;
};

const DEFAULT_STATE: Omit<BillingState, "loading" | "error"> = {
  activePlanKey: "free",
  family: "free",
  billingInterval: "none",
  stripeSubscriptionStatus: null,
  latestInvoiceId: null,
  latestInvoiceStatus: null,
  latestPaymentIntentStatus: null,
  latestPaymentFailureCode: null,
  latestPaymentFailureMessage: null,
  stripeCustomerId: null,
  cancelAtPeriodEnd: false,
  currentPeriodEnd: null,
  nextCreditRefreshAt: null,
  scheduledChange: null,
  monthlyAllowance: 8,
  lastGrantAt: null,
  availableCredits: 0,
  reservedCredits: 0,
  subscriptionCredits: 0,
  topupCredits: 0,
  topupActivePackCount: 0,
  topupEarliestExpiresAt: null,
  topupPacks: [],
  overdrafted: false,
  isExpired: false,
};

export function useBillingState(): BillingState {
  const { user } = useAuth();
  const [state, setState] = useState<BillingState>({
    ...DEFAULT_STATE,
    loading: true,
    error: null,
  });

  useEffect(() => {
    if (!user) {
      setState({ ...DEFAULT_STATE, loading: false, error: null });
      return;
    }

    setState({ ...DEFAULT_STATE, topupPacks: [], loading: true, error: null });

    const unsubscribe = onSnapshot(
      doc(db, "users", user.uid),
      (snapshot) => {
        if (!snapshot.exists()) {
          return;
        }

        const data = snapshot.data();
        const billing = objectValue(data.billing);
        const credits = objectValue(data.credits);
        if (
          !billing ||
          !credits ||
          typeof billing.activePlanKey !== "string" ||
          finiteNumber(credits.balance) === null
        ) {
          return;
        }
        const topupCredits = objectValue(data.topupCredits) ?? {};
        const balance = finiteNumber(credits.balance) ?? 0;
        const reserved = finiteNumber(credits.reserved) ?? 0;
        const topupTotalRemaining = Number(topupCredits.totalRemaining || 0);
        const topupReserved = Number(topupCredits.totalReserved || 0);
        const topupAvailable = Number(
          topupCredits.totalAvailable ?? Math.max(0, topupTotalRemaining - topupReserved)
        );
        const expiresAt = toDate(credits.expiresAt);
        const rawPlanKey = String(billing.activePlanKey || "free");
        const activePlanKey = isBillingPlanKey(rawPlanKey) ? rawPlanKey : "free";

        setState((current) => ({
          ...current,
          activePlanKey,
          family: normalizeFamily(billing.family),
          billingInterval: normalizeInterval(billing.billingInterval),
          stripeSubscriptionStatus: textOrNull(billing.stripeSubscriptionStatus),
          latestInvoiceId: textOrNull(billing.latestInvoiceId),
          latestInvoiceStatus: textOrNull(billing.latestInvoiceStatus),
          latestPaymentIntentStatus: textOrNull(billing.latestPaymentIntentStatus),
          latestPaymentFailureCode: textOrNull(billing.latestPaymentFailureCode),
          latestPaymentFailureMessage: textOrNull(billing.latestPaymentFailureMessage),
          stripeCustomerId: textOrNull(billing.stripeCustomerId),
          cancelAtPeriodEnd: Boolean(billing.cancelAtPeriodEnd),
          currentPeriodEnd: toDate(billing.currentPeriodEnd),
          nextCreditRefreshAt: toDate(billing.nextCreditRefreshAt),
          scheduledChange: toScheduledChange(billing.scheduledChange),
          monthlyAllowance: finiteNumber(credits.monthlyAllowance) ?? 0,
          lastGrantAt: toDate(credits.lastGrantAt),
          availableCredits: balance - reserved + topupAvailable,
          reservedCredits: reserved,
          subscriptionCredits: balance - reserved,
          topupCredits: topupAvailable,
          topupActivePackCount: Number(topupCredits.activePackCount || 0),
          topupEarliestExpiresAt: toDate(topupCredits.earliestExpiresAt),
          overdrafted: Boolean(credits.overdrafted),
          isExpired: expiresAt ? Date.now() > expiresAt.getTime() : false,
          loading: false,
          error: null,
        }));
      },
      (error) => {
        console.error("Error listening to billing state:", error);
        setState((current) => ({
          ...current,
          loading: false,
          error: "Could not load your billing status.",
        }));
      }
    );

    const packsQuery = query(collection(db, "topup_packs"), where("userId", "==", user.uid));
    const unsubscribePacks = onSnapshot(
      packsQuery,
      (snapshot) => {
        const topupPacks = snapshot.docs
          .map((packSnapshot) => {
            const data = packSnapshot.data();
            const creditsRemaining = Number(data.creditsRemaining || 0);
            const creditsReserved = Number(data.creditsReserved || 0);
            return {
              id: packSnapshot.id,
              creditsAvailable: Math.max(0, creditsRemaining - creditsReserved),
              creditsRemaining,
              creditsReserved,
              expiresAt: toDate(data.expiresAt),
              status: String(data.status || ""),
            };
          })
          .filter((pack) => pack.status === "active" && pack.creditsAvailable > 0)
          .sort((left, right) => {
            const leftTime = left.expiresAt?.getTime() ?? Number.MAX_SAFE_INTEGER;
            const rightTime = right.expiresAt?.getTime() ?? Number.MAX_SAFE_INTEGER;
            return leftTime - rightTime;
          })
          .map(({ status: _status, ...pack }) => pack);
        setState((current) => ({ ...current, topupPacks }));
      },
      (error) => {
        console.error("Error listening to top-up packs:", error);
        setState((current) => ({ ...current, topupPacks: [] }));
      }
    );

    return () => {
      unsubscribe();
      unsubscribePacks();
    };
  }, [user]);

  return state;
}

function toScheduledChange(value: unknown): ScheduledPlanChange | null {
  const change = objectValue(value);
  const at = toDate(change?.at);
  if (!change || !at || !isBillingPlanKey(String(change.planKey))) return null;
  // Once its date has passed, the change has happened: the webhooks clear it shortly after.
  if (at.getTime() <= Date.now()) return null;
  return { planKey: change.planKey as BillingPlanKey, at };
}

function normalizeFamily(value: unknown): PlanFamily {
  return value === "solo" || value === "choir" || value === "max" ? value : "free";
}

function normalizeInterval(value: unknown): BillingState["billingInterval"] {
  return value === "month" || value === "year" ? value : "none";
}

function toDate(value: unknown): Date | null {
  if (!value) return null;
  if (value instanceof Date) return value;
  if (typeof value === "string") {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const timestamp = value as FirestoreTimestampLike;
  return typeof timestamp.toDate === "function" ? timestamp.toDate() : null;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function textOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function finiteNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const numberValue = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}
