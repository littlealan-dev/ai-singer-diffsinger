import { createCheckoutSession as createCheckoutSessionRequest, createPortalSession } from "../api";
import type { BillingPlanKey } from "./plans";

export async function startCheckout(planKey: BillingPlanKey): Promise<string> {
  const { url } = await createCheckoutSessionRequest(planKey);
  return url;
}

export async function startBillingPortal(): Promise<string> {
  const { url } = await createPortalSession();
  return url;
}

