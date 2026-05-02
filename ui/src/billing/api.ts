import {
  createCheckoutSession as createCheckoutSessionRequest,
  createPortalSession,
  syncBillingSubscription as syncBillingSubscriptionRequest,
  syncCheckoutSession as syncCheckoutSessionRequest,
} from "../api";
import type { BillingPlanKey } from "./plans";

const PENDING_BILLING_PORTAL_SYNC_KEY = "sightsinger.pendingBillingPortalSync";

export async function startCheckout(planKey: BillingPlanKey): Promise<string> {
  const { url } = await createCheckoutSessionRequest(planKey);
  return url;
}

export async function startBillingPortal(): Promise<string> {
  const { url } = await createPortalSession();
  storePendingBillingPortalSync();
  return url;
}

export async function syncCheckoutSession(sessionId: string): Promise<void> {
  await syncCheckoutSessionRequest(sessionId);
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
