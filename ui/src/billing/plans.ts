import {
  annualPromoFromEnv,
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
  formatPlanBadge,
  formatPlanName,
  getPlanChangeKind,
  planChangeAppliesNow,
  INCLUDED_IN_EVERY_PLAN_FEATURES,
  isBillingPlanKey,
  isCurrentPlanCard,
  isPaidPlanKey,
} from "../../../shared/billingPlans";

export const PENDING_CHECKOUT_PLAN_KEY = "sightsinger.pendingCheckoutPlan";

const DEFAULT_EARLY_SUPPORTER_ENABLED =
  import.meta.env.VITE_CHOIR_EARLY_SUPPORTER_ENABLED !== "false";
// Max stays hidden until its Stripe prices exist: the build turns it on with "true".
const MAX_EARLY_SUPPORTER_ENABLED = import.meta.env.VITE_MAX_EARLY_SUPPORTER_ENABLED === "true";

// A sale on the Pro and Max annual plans; the API applies the matching Stripe coupon.
const ANNUAL_PROMO = annualPromoFromEnv(
  import.meta.env.VITE_ANNUAL_PROMO_PERCENT_OFF,
  import.meta.env.VITE_ANNUAL_PROMO_ENDS_AT,
  import.meta.env.VITE_ANNUAL_PROMO_LABEL
);

export function getDisplayPlans(interval: BillingInterval = "annual") {
  return getSharedDisplayPlans(interval, {
    choirEarlySupporterEnabled: DEFAULT_EARLY_SUPPORTER_ENABLED,
    maxEarlySupporterEnabled: MAX_EARLY_SUPPORTER_ENABLED,
    annualPromo: ANNUAL_PROMO,
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
    planKey === "solo_monthly" ||
    planKey === "solo_annual" ||
    planKey === "choir_early_monthly" ||
    planKey === "choir_early_annual" ||
    planKey === "choir_monthly" ||
    planKey === "choir_annual" ||
    planKey === "max_early_monthly" ||
    planKey === "max_early_annual"
  );
}
