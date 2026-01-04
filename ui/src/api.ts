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
  | { type: "chat_text"; message: string; current_score?: unknown }
  | { type: "chat_audio"; message: string; audio_url: string; current_score?: unknown }
  | {
      type: "chat_progress";
      message: string;
      progress_url: string;
      job_id?: string;
      current_score?: unknown;
    };

export type ProgressResponse = {
  status: "idle" | "queued" | "running" | "done" | "error";
  message?: string;
  step?: string;
  progress?: number;
  audio_url?: string;
  job_id?: string;
  error?: string;
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

async function request<T>(path: string, options: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createSession(): Promise<{ session_id: string }> {
  return request("/sessions", { method: "POST" });
}

export async function uploadScore(sessionId: string, file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/upload`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "Upload failed");
  }
  return response.json();
}

export async function fetchScoreXml(sessionId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/score`);
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
      audio_url: withStream(withApiBase(response.audio_url)),
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
  const response = await fetch(withApiBase(progressUrl));
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  const payload = (await response.json()) as ProgressResponse;
  if (payload.audio_url) {
    payload.audio_url = withStream(withApiBase(payload.audio_url));
  }
  return payload;
}
