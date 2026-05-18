import type { User } from "firebase/auth";
import { requestMarketingOptIn } from "./api";

export const MARKETING_AUTH_CONSENT_TEXT =
  "Send me product updates and SightSinger news. I can unsubscribe at any time.";

const PENDING_MARKETING_OPT_IN_KEY = "sightsinger.pendingMarketingOptIn";
const PENDING_MARKETING_OPT_IN_TTL_MS = 24 * 60 * 60 * 1000;

export type PendingMarketingOptInSource =
  | "auth_modal_google"
  | "auth_modal_magic_link";

export type PendingMarketingOptIn = {
  marketingOptIn: boolean;
  source: PendingMarketingOptInSource;
  createdAt: number;
  email?: string;
};

export type MarketingOptInState = {
  emailOptInRequested: boolean | null;
  emailOptInBrevoStatus: string | null;
  emailOptInRequestedAt: Date | null;
  loading: boolean;
};

export type MarketingOptInProcessResult =
  | {
      status: "processed";
      backendStatus: string;
      message: string;
    }
  | {
      status: "skipped" | "retry_later";
    };

export function storePendingMarketingOptIn(intent: PendingMarketingOptIn): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PENDING_MARKETING_OPT_IN_KEY, JSON.stringify(intent));
  } catch {
    // Ignore storage failures; auth should still proceed.
  }
}

export function readPendingMarketingOptIn(): PendingMarketingOptIn | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(PENDING_MARKETING_OPT_IN_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingMarketingOptIn>;
    if (!parsed.marketingOptIn || !parsed.source || typeof parsed.createdAt !== "number") {
      return null;
    }
    return {
      marketingOptIn: true,
      source: parsed.source,
      createdAt: parsed.createdAt,
      email: typeof parsed.email === "string" ? parsed.email : undefined,
    };
  } catch {
    return null;
  }
}

export function clearPendingMarketingOptIn(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(PENDING_MARKETING_OPT_IN_KEY);
  } catch {
    // Ignore storage failures.
  }
}

export function isPendingMarketingOptInExpired(intent: PendingMarketingOptIn): boolean {
  return Date.now() - intent.createdAt > PENDING_MARKETING_OPT_IN_TTL_MS;
}

export async function processPendingMarketingOptIn(
  user: User,
  marketingState: MarketingOptInState
): Promise<MarketingOptInProcessResult> {
  const intent = readPendingMarketingOptIn();
  if (!intent) return { status: "skipped" };
  if (isPendingMarketingOptInExpired(intent)) {
    clearPendingMarketingOptIn();
    return { status: "skipped" };
  }
  if (intent.source === "auth_modal_magic_link") {
    const signedInEmail = user.email?.trim().toLowerCase();
    const pendingEmail = intent.email?.trim().toLowerCase();
    if (!signedInEmail || !pendingEmail || signedInEmail !== pendingEmail) {
      clearPendingMarketingOptIn();
      return { status: "skipped" };
    }
  }
  if (marketingState.emailOptInRequested === true) {
    clearPendingMarketingOptIn();
    return { status: "skipped" };
  }
  try {
    const response = await requestMarketingOptIn({
      source: intent.source,
      consent_text: MARKETING_AUTH_CONSENT_TEXT,
      pending_intent_created_at: new Date(intent.createdAt).toISOString(),
    });
    clearPendingMarketingOptIn();
    return {
      status: "processed",
      backendStatus: response.status,
      message: response.message,
    };
  } catch (error) {
    console.error("Failed to request marketing opt-in:", error);
    return { status: "retry_later" };
  }
}
