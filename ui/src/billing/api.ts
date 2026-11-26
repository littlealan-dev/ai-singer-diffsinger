import {
  cancelScheduledPlanChange,
  cancelTopupCheckoutSession as cancelTopupCheckoutSessionRequest,
  createCheckoutSession as createCheckoutSessionRequest,
  createEmbeddedCheckoutSession,
  createPlanChangeSession,
  createPortalSession,
  createTopupCheckoutSession,
  syncBillingSubscription as syncBillingSubscriptionRequest,
  syncCheckoutSession as syncCheckoutSessionRequest,
  syncTopupCheckoutSession as syncTopupCheckoutSessionRequest,
  type EmbeddedCheckoutResponse,
} from "../api";
import { isBillingPlanKey, type BillingPlanKey } from "./plans";

const PENDING_BILLING_PORTAL_SYNC_KEY = "sightsinger.pendingBillingPortalSync";
const PENDING_PLAN_CHANGE_KEY = "sightsinger.pendingPlanChange";

/** A plan change the user confirmed in the Stripe portal, until the app shows its result. */
export type PendingPlanChange = {
  planKey: BillingPlanKey;
  startedAt: number;
};

export async function startCheckout(planKey: BillingPlanKey): Promise<string> {
  const { url } = await createCheckoutSessionRequest(planKey);
  return url;
}

export async function startTopupCheckout(): Promise<string> {
  const { url } = await createTopupCheckoutSession("topup_15");
  return url;
}

export async function startEmbeddedPlanCheckout(planKey: BillingPlanKey): Promise<EmbeddedCheckoutResponse> {
  return createEmbeddedCheckoutSession({ checkoutType: "subscription", planKey });
}

export async function startEmbeddedTopupCheckout(): Promise<EmbeddedCheckoutResponse> {
  return createEmbeddedCheckoutSession({ checkoutType: "topup", packKey: "topup_15" });
}

export async function startBillingPortal(): Promise<string> {
  const { url } = await createPortalSession();
  storePendingBillingPortalSync();
  return url;
}

/** Open the Stripe portal's confirm page for one plan change; the portal returns to the app. */
export async function startPlanChange(planKey: BillingPlanKey): Promise<string> {
  const { url } = await createPlanChangeSession(planKey);
  storePendingBillingPortalSync();
  if (typeof window !== "undefined") {
    const pending: PendingPlanChange = { planKey, startedAt: Date.now() };
    window.localStorage.setItem(PENDING_PLAN_CHANGE_KEY, JSON.stringify(pending));
  }
  return url;
}

/** Undo a plan change booked for the renewal date. */
export async function undoScheduledPlanChange(): Promise<void> {
  await cancelScheduledPlanChange();
}

export function getPendingPlanChange(): PendingPlanChange | null {
  if (typeof window === "undefined") return null;
  try {
    const value = JSON.parse(window.localStorage.getItem(PENDING_PLAN_CHANGE_KEY) || "null");
    if (value && isBillingPlanKey(value.planKey) && typeof value.startedAt === "number") {
      return { planKey: value.planKey, startedAt: value.startedAt };
    }
  } catch {
    // A damaged entry is the same as none.
  }
  return null;
}

export function clearPendingPlanChange(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(PENDING_PLAN_CHANGE_KEY);
}

export async function syncCheckoutSession(sessionId: string): Promise<void> {
  await syncCheckoutSessionRequest(sessionId);
}

export async function syncTopupCheckoutSession(sessionId: string): Promise<void> {
  await syncTopupCheckoutSessionRequest(sessionId);
}

export async function cancelTopupCheckoutSession(sessionId: string): Promise<void> {
  await cancelTopupCheckoutSessionRequest(sessionId);
}

export async function syncBillingSubscription(): Promise<void> {
  await syncBillingSubscriptionRequest();
}

export function storePendingBillingPortalSync(): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(PENDING_BILLING_PORTAL_SYNC_KEY, "1");
}

export function hasPendingBillingPortalSync(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(PENDING_BILLING_PORTAL_SYNC_KEY) === "1";
}

export function clearPendingBillingPortalSync(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(PENDING_BILLING_PORTAL_SYNC_KEY);
}
