import { useEffect, useState } from "react";
import { doc, onSnapshot } from "firebase/firestore";
import { db } from "../firebase";
import { useAuth } from "./useAuth";
import type { BillingPlanKey, PlanFamily } from "../billing/plans";
import { isBillingPlanKey } from "../billing/plans";

type FirestoreTimestampLike = {
  toDate?: () => Date;
};

export type BillingState = {
  activePlanKey: BillingPlanKey;
  family: PlanFamily;
  billingInterval: "none" | "month" | "year";
  stripeSubscriptionStatus: string | null;
  stripeCustomerId: string | null;
  cancelAtPeriodEnd: boolean;
  currentPeriodEnd: Date | null;
  nextCreditRefreshAt: Date | null;
  monthlyAllowance: number;
  availableCredits: number;
  reservedCredits: number;
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
  stripeCustomerId: null,
  cancelAtPeriodEnd: false,
  currentPeriodEnd: null,
  nextCreditRefreshAt: null,
  monthlyAllowance: 8,
  availableCredits: 0,
  reservedCredits: 0,
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

    setState((current) => ({ ...current, loading: true, error: null }));

    const unsubscribe = onSnapshot(
      doc(db, "users", user.uid),
      (snapshot) => {
        if (!snapshot.exists()) {
          setState({ ...DEFAULT_STATE, loading: false, error: null });
          return;
        }

        const data = snapshot.data();
        const billing = data.billing || {};
        const credits = data.credits || {};
        const balance = Number(credits.balance || 0);
        const reserved = Number(credits.reserved || 0);
        const expiresAt = toDate(credits.expiresAt);
        const rawPlanKey = String(billing.activePlanKey || "free");
        const activePlanKey = isBillingPlanKey(rawPlanKey) ? rawPlanKey : "free";

        setState({
          activePlanKey,
          family: normalizeFamily(billing.family),
          billingInterval: normalizeInterval(billing.billingInterval),
          stripeSubscriptionStatus: billing.stripeSubscriptionStatus || null,
          stripeCustomerId: billing.stripeCustomerId || null,
          cancelAtPeriodEnd: Boolean(billing.cancelAtPeriodEnd),
          currentPeriodEnd: toDate(billing.currentPeriodEnd),
          nextCreditRefreshAt: toDate(billing.nextCreditRefreshAt),
          monthlyAllowance: Number(credits.monthlyAllowance || 0),
          availableCredits: balance - reserved,
          reservedCredits: reserved,
          overdrafted: Boolean(credits.overdrafted),
          isExpired: expiresAt ? Date.now() > expiresAt.getTime() : false,
          loading: false,
          error: null,
        });
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

    return () => unsubscribe();
  }, [user]);

  return state;
}

function normalizeFamily(value: unknown): PlanFamily {
  return value === "solo" || value === "choir" ? value : "free";
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
