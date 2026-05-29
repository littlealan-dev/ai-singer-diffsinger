import { getAppCheckToken, getIdToken } from "./firebase";

export type ScoreSummaryPart = {
  part_id?: string;
  part_index?: number;
  part_name?: string;
  has_lyrics?: boolean;
};

export type ScoreSummary = {
  title?: string | null;
  composer?: string | null;
  lyricist?: string | null;
  duration_seconds?: number;
  parts?: ScoreSummaryPart[];
  available_verses?: Array<string | number>;
};

export type ChatSelection = {
  verse_number?: string | number;
  part_index?: number;
  part_id?: string;
};

export type VoicebankOption = {
  id: string;
  name: string;
  gender?: string | null;
  voice_type?: string | null;
  default_voice_color?: string | null;
  profile_image?: string | null;
  selector_image?: string | null;
};

export type UploadResponse = {
  session_id: string;
  parsed: boolean;
  current_score?: unknown;
  score_summary?: ScoreSummary | null;
};

export type ChatResponse =
  | {
      type: "chat_text";
      message: string;
      current_score?: unknown;
      suppress_selector?: boolean;
      details?: unknown;
      warning?: string;
    }
  | {
      type: "chat_audio";
      message: string;
      audio_url: string;
      current_score?: unknown;
      details?: unknown;
      warning?: string;
    }
  | {
      type: "chat_progress";
      message: string;
      progress_url: string;
      job_id?: string;
      current_score?: unknown;
      details?: unknown;
      warning?: string;
    }
  | { type: "chat_error"; message: string; details?: unknown };

export type ProgressResponse = {
  status: "idle" | "queued" | "running" | "done" | "error";
  message?: string;
  step?: string;
  progress?: number;
  audio_url?: string;
  job_id?: string;
  job_kind?: string;
  review_required?: boolean;
  error?: string;
  details?: unknown;
  warning?: string;
  feedback?: FeedbackPromptState;
};

export type FeedbackPromptState = {
  promptCandidate?: boolean;
  prompted?: boolean;
  submitted?: boolean;
  feedbackId?: string;
};

export type FeedbackRatingsRequest = {
  voiceQuality: number;
  pronunciation: number;
  timingRhythm: number;
  lyricsAlignment: number;
  partSplittingAccuracy: number;
};

export type FeedbackSubmitResponse = {
  status: string;
  feedbackId: string;
};

export type WaitlistSubscribeRequest = {
  email: string;
  first_name?: string;
  feedback?: string;
  gdpr_consent: boolean;
  consent_text: string;
  source: string;
};

export type WaitlistSubscribeResponse = {
  success: boolean;
  message: string;
  requires_confirmation: boolean;
};

export type MarketingOptInRequest = {
  source: string;
  consent_text: string;
  pending_intent_created_at?: string;
};

export type MarketingOptInResponse = {
  success: boolean;
  status: string;
  message: string;
  requires_confirmation: boolean;
};

export type BillingCheckoutResponse = {
  url: string;
};

export type BillingPortalResponse = {
  url: string;
};

export type BillingCheckoutSyncResponse = {
  synced: boolean;
  status: string;
  activePlanKey?: string | null;
};

export type BillingSubscriptionSyncResponse = {
  synced: boolean;
  status: string;
  activePlanKey?: string | null;
};

export type VoicebankListResponse = {
  voicebanks: VoicebankOption[];
};

export type MaintenanceStatusResponse = {
  enabled: boolean;
  allowed: boolean;
  message?: string | null;
};

export type BackendReadyzResponse = {
  status: string;
  ready: boolean;
  build?: string;
  mcp?: {
    status?: string;
    ready?: boolean;
    starting?: boolean;
    error?: string;
  };
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const APP_ENV = import.meta.env.VITE_APP_ENV ?? "";
const IS_LOCAL_DEV = ["dev", "development", "local"].includes(APP_ENV.toLowerCase());
export const BACKEND_STARTING_MESSAGE =
  "SightSinger is still starting up. Please try again in a moment.";
const BACKEND_READY_TIMEOUT_SECONDS = parsePositiveNumber(
  import.meta.env.VITE_BACKEND_READY_TIMEOUT_SECONDS,
  240
);

class BackendStartingError extends Error {
  constructor(message = BACKEND_STARTING_MESSAGE) {
    super(message);
    this.name = "BackendStartingError";
  }
}

function parsePositiveNumber(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function withApiBase(url: string): string {
  if (!url) return url;
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  if (!API_BASE) return url;
  return `${API_BASE}${url.startsWith("/") ? "" : "/"}${url}`;
}

async function withAppCheckHeaders(
  headers?: HeadersInit
): Promise<HeadersInit | undefined> {
  const token = await getAppCheckToken();
  if (!token) {
    return headers;
  }
  return {
    ...(headers ?? {}),
    "X-Firebase-AppCheck": token,
  };
}

async function withAuthHeaders(
  headers?: HeadersInit
): Promise<HeadersInit | undefined> {
  const token = await getIdToken();
  if (!token) {
    return headers;
  }
  return {
    ...(headers ?? {}),
    Authorization: `Bearer ${token}`,
  };
}

async function fetchWithBackendTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {}
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(
    () => controller.abort(),
    BACKEND_READY_TIMEOUT_SECONDS * 1000
  );
  const signal = init.signal;
  if (signal) {
    if (signal.aborted) {
      controller.abort();
    } else {
      signal.addEventListener("abort", () => controller.abort(), { once: true });
    }
  }
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new BackendStartingError();
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function backendStartingMessageFromBody(text: string): string | null {
  if (!text) return null;
  try {
    const payload = JSON.parse(text);
    const detail = payload?.detail;
    if (detail?.code === "backend_starting") {
      return detail.message || BACKEND_STARTING_MESSAGE;
    }
  } catch {
    // Fall back to text matching below.
  }
  if (
    text.includes("backend_starting") ||
    text.includes("MCP startup is still in progress") ||
    text.includes("SightSinger is still starting up")
  ) {
    return BACKEND_STARTING_MESSAGE;
  }
  return null;
}

async function errorFromResponse(response: Response, fallback: string): Promise<Error> {
  const text = await response.text();
  const startupMessage = backendStartingMessageFromBody(text);
  const localProxyBackendDown = IS_LOCAL_DEV && response.status === 500 && !text.trim();
  if (startupMessage || response.status === 503 || localProxyBackendDown) {
    return new BackendStartingError(startupMessage || BACKEND_STARTING_MESSAGE);
  }
  return new Error(text || fallback);
}

async function request<T>(path: string, options: RequestInit): Promise<T> {
  let headers = await withAppCheckHeaders(options.headers);
  headers = await withAuthHeaders(headers);
  const response = await fetchWithBackendTimeout(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    throw await errorFromResponse(response, `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createSession(): Promise<{ session_id: string }> {
  return request("/sessions", { method: "POST" });
}

export async function ensureCredits(): Promise<unknown> {
  return request("/credits", { method: "GET" });
}

export async function fetchMaintenanceStatus(): Promise<MaintenanceStatusResponse> {
  return request("/maintenance/status", { method: "GET" });
}

export async function fetchBackendReadiness(): Promise<BackendReadyzResponse> {
  return request("/readyz", { method: "GET" });
}

export async function createCheckoutSession(planKey: string): Promise<BillingCheckoutResponse> {
  return request("/billing/checkout-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ planKey }),
  });
}

export async function syncCheckoutSession(sessionId: string): Promise<BillingCheckoutSyncResponse> {
  return request("/billing/checkout-session/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
}

export async function syncBillingSubscription(): Promise<BillingSubscriptionSyncResponse> {
  return request("/billing/subscription/sync", { method: "POST" });
}

export async function createPortalSession(): Promise<BillingPortalResponse> {
  return request("/billing/portal-session", { method: "POST" });
}

export async function subscribeToWaitlist(
  payload: WaitlistSubscribeRequest
): Promise<WaitlistSubscribeResponse> {
  return request("/waitlist/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function requestMarketingOptIn(
  payload: MarketingOptInRequest
): Promise<MarketingOptInResponse> {
  return request("/marketing/opt-in", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function uploadScore(sessionId: string, file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithBackendTimeout(`${API_BASE}/sessions/${sessionId}/upload`, {
    method: "POST",
    body: form,
    headers,
  });
  if (!response.ok) {
    throw await errorFromResponse(response, "Upload failed");
  }
  return response.json();
}

export async function fetchScoreXml(sessionId: string): Promise<string> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithBackendTimeout(`${API_BASE}/sessions/${sessionId}/score`, {
    headers,
  });
  if (!response.ok) {
    throw await errorFromResponse(response, "Failed to load score.");
  }
  return response.text();
}

export async function fetchVoicebanks(): Promise<VoicebankOption[]> {
  const response = await request<VoicebankListResponse>("/api/voicebanks", { method: "GET" });
  return Array.isArray(response.voicebanks) ? response.voicebanks : [];
}

export async function chat(
  sessionId: string,
  message: string,
  selection?: ChatSelection,
  selectedVoicebankId?: string | null
): Promise<ChatResponse> {
  const body: {
    message: string;
    selection?: ChatSelection;
    selected_voicebank_id?: string;
  } = { message };
  if (selection) {
    body.selection = selection;
  }
  if (selectedVoicebankId) {
    body.selected_voicebank_id = selectedVoicebankId;
  }
  const response = await request<ChatResponse>(`/sessions/${sessionId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.type === "chat_audio") {
    return {
      ...response,
      audio_url: withApiBase(response.audio_url),
    };
  }
  if (response.type === "chat_progress") {
    return {
      ...response,
      progress_url: withApiBase(response.progress_url),
    };
  }
  return response;
}

export async function fetchProgress(progressUrl: string): Promise<ProgressResponse> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithBackendTimeout(withApiBase(progressUrl), { headers });
  if (!response.ok) {
    throw await errorFromResponse(response, `Request failed: ${response.status}`);
  }
  const payload = (await response.json()) as ProgressResponse;
  if (payload.audio_url) {
    payload.audio_url = withApiBase(payload.audio_url);
  }
  return payload;
}

export async function markFeedbackPrompted(
  jobId: string,
  trigger: "audio_played" | "audio_downloaded"
): Promise<{ status: string }> {
  return request("/feedback/prompted", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jobId, trigger }),
  });
}

export async function submitAudioFeedback(payload: {
  jobId: string;
  ratings: FeedbackRatingsRequest;
  comment: string;
}): Promise<FeedbackSubmitResponse> {
  return request("/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      client: {
        userAgent: typeof navigator !== "undefined" ? navigator.userAgent : undefined,
      },
    }),
  });
}
