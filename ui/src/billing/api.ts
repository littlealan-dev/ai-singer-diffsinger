import {
  cancelTopupCheckoutSession as cancelTopupCheckoutSessionRequest,
  createCheckoutSession as createCheckoutSessionRequest,
  createEmbeddedCheckoutSession,
  createPortalSession,
  createTopupCheckoutSession,
  syncBillingSubscription as syncBillingSubscriptionRequest,
  syncCheckoutSession as syncCheckoutSessionRequest,
  syncTopupCheckoutSession as syncTopupCheckoutSessionRequest,
  type EmbeddedCheckoutResponse,
} from "../api";
import type { BillingPlanKey } from "./plans";

const PENDING_BILLING_PORTAL_SYNC_KEY = "sightsinger.pendingBillingPortalSync";

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
