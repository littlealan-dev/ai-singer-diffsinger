import {
  getDisplayPlans as getSharedDisplayPlans,
  type BillingInterval,
  type BillingPlanKey,
} from "../../../shared/billingPlans";

export type {
  BillingInterval,
  BillingPlanKey,
  DisplayPlan,
  PlanCardKey,
  PlanFamily,
} from "../../../shared/billingPlans";

export {
  formatPlanName,
  INCLUDED_IN_EVERY_PLAN_FEATURES,
  isBillingPlanKey,
  isCurrentPlanCard,
  isPaidPlanKey,
} from "../../../shared/billingPlans";

export const PENDING_CHECKOUT_PLAN_KEY = "sightsinger.pendingCheckoutPlan";

const DEFAULT_EARLY_SUPPORTER_ENABLED =
  import.meta.env.VITE_CHOIR_EARLY_SUPPORTER_ENABLED !== "false";

export function getDisplayPlans(interval: BillingInterval = "annual") {
  return getSharedDisplayPlans(interval, {
    choirEarlySupporterEnabled: DEFAULT_EARLY_SUPPORTER_ENABLED,
  });
}

export function getStoredPendingCheckoutPlan(): BillingPlanKey | null {
  if (typeof window === "undefined") return null;
  const value = window.localStorage.getItem(PENDING_CHECKOUT_PLAN_KEY);
  return value === "free" || isPaidCheckoutPlan(value) ? (value as BillingPlanKey) : null;
}

export function storePendingCheckoutPlan(planKey: BillingPlanKey): void {
  if (typeof window === "undefined") return;
  if (isPaidCheckoutPlan(planKey)) {
    window.localStorage.setItem(PENDING_CHECKOUT_PLAN_KEY, planKey);
  }
}

export function clearPendingCheckoutPlan(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(PENDING_CHECKOUT_PLAN_KEY);
}

function isPaidCheckoutPlan(planKey: string | null | undefined): boolean {
  return (
    planKey === "starter_monthly" ||
    planKey === "starter_annual" ||
    planKey === "solo_monthly" ||
    planKey === "solo_annual" ||
    planKey === "choir_early_monthly" ||
    planKey === "choir_early_annual" ||
    planKey === "choir_monthly" ||
    planKey === "choir_annual"
  );
}
