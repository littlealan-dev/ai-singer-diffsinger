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
    }
  | { type: "chat_audio"; message: string; audio_url: string; current_score?: unknown }
  | {
      type: "chat_progress";
      message: string;
      progress_url: string;
      job_id?: string;
      current_score?: unknown;
    }
  | { type: "chat_error"; message: string };

export type ProgressResponse = {
  status: "idle" | "queued" | "running" | "done" | "error";
  message?: string;
  step?: string;
  progress?: number;
  audio_url?: string;
  job_id?: string;
  error?: string;
};

export type WaitlistSubscribeRequest = {
  email: string;
  first_name?: string;
  gdpr_consent: boolean;
  consent_text: string;
  source: string;
};

export type WaitlistSubscribeResponse = {
  success: boolean;
  message: string;
  requires_confirmation: boolean;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

function withApiBase(url: string): string {
  if (!url) return url;
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  if (!API_BASE) return url;
  return `${API_BASE}${url.startsWith("/") ? "" : "/"}${url}`;
}

function withStream(url: string): string {
  if (!url) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}stream=1`;
}

async function withAppCheckParam(url: string): Promise<string> {
  if (!url) return url;
  const token = await getAppCheckToken();
  if (!token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}app_check=${encodeURIComponent(token)}`;
}

async function withAuthParam(url: string): Promise<string> {
  if (!url) return url;
  const token = await getIdToken();
  if (!token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}id_token=${encodeURIComponent(token)}`;
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

async function request<T>(path: string, options: RequestInit): Promise<T> {
  let headers = await withAppCheckHeaders(options.headers);
  headers = await withAuthHeaders(headers);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createSession(): Promise<{ session_id: string }> {
  return request("/sessions", { method: "POST" });
}

export async function ensureCredits(): Promise<unknown> {
  return request("/credits", { method: "GET" });
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

export async function uploadScore(sessionId: string, file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/upload`, {
    method: "POST",
    body: form,
    headers,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "Upload failed");
  }
  return response.json();
}

export async function fetchScoreXml(sessionId: string): Promise<string> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/score`, { headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "Failed to load score.");
  }
  return response.text();
}

export async function chat(sessionId: string, message: string): Promise<ChatResponse> {
  const response = await request<ChatResponse>(`/sessions/${sessionId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (response.type === "chat_audio") {
    return {
      ...response,
      // TODO: Replace query-param auth with short-lived signed URLs from the backend.
      audio_url: await withAuthParam(
        await withAppCheckParam(withStream(withApiBase(response.audio_url)))
      ),
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
  const response = await fetch(withApiBase(progressUrl), { headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  const payload = (await response.json()) as ProgressResponse;
  if (payload.audio_url) {
    // TODO: Replace query-param auth with short-lived signed URLs from the backend.
    payload.audio_url = await withAuthParam(
      await withAppCheckParam(withStream(withApiBase(payload.audio_url)))
    );
  }
  return payload;
}
