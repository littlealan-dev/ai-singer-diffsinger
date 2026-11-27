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
export const ANNUAL_PROMO = annualPromoFromEnv(
  import.meta.env.VITE_ANNUAL_PROMO_PERCENT_OFF,
  import.meta.env.VITE_ANNUAL_PROMO_ENDS_AT,
  import.meta.env.VITE_ANNUAL_PROMO_LABEL
);

/** The Credit Pack: $10 for 30 credits, or the sale's credits until the sale ends (the API grants the same). */
const TOPUP_PROMO_CREDITS = Number(import.meta.env.VITE_TOPUP_PROMO_CREDITS || 0);
const TOPUP_PROMO_ENDS_AT = import.meta.env.VITE_TOPUP_PROMO_ENDS_AT
  ? new Date(import.meta.env.VITE_TOPUP_PROMO_ENDS_AT)
  : null;

export type TopupPackOffer = {
  priceLabel: string;
  credits: number;
  usualCredits: number;
  saleLabel?: string;
  /** The saving per credit: $10 for 40 instead of 30 is 25% off. */
  savingsLabel?: string;
};

export function getTopupPackOffer(now: Date = new Date()): TopupPackOffer {
  const usual = { priceLabel: "$10", credits: 30, usualCredits: 30 };
  const onSale =
    TOPUP_PROMO_CREDITS > 0 && TOPUP_PROMO_ENDS_AT !== null && now < TOPUP_PROMO_ENDS_AT;
  if (!onSale) return usual;
  const percentOff = Math.round((1 - usual.usualCredits / TOPUP_PROMO_CREDITS) * 100);
  return {
    ...usual,
    credits: TOPUP_PROMO_CREDITS,
    saleLabel: ANNUAL_PROMO?.label ?? "Sale",
    savingsLabel: percentOff > 0 ? `${percentOff}% off` : undefined,
  };
}

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
