import { getAppCheckToken, getIdToken } from "./firebase";

export type ScoreSummaryPart = {
  part_id?: string;
  part_index?: number;
  part_name?: string;
  has_lyrics?: boolean;
  /** Changes when the part's music or the score's timing is edited. */
  take_signature?: string;
};

export type InstrumentalPart = {
  part_index: number;
  part_id: string;
  raw_part_id: string;
  label: string;
  eligible: boolean;
  has_lyrics: boolean;
  midi_program: number | null;
  soundfont_bank?: number | null;
  playback_preset?: {
    soundfont_id: string;
    bank: number;
    program: number;
    kind: "melodic" | "percussion_kit";
  } | null;
  score_instrument_id?: string | null;
  program_source?: string;
  midi_channel?: number | null;
  percussion: boolean;
  diagnostic?: string | null;
};

export type PerformanceMidi = {
  version: number;
  instrumental_parts: InstrumentalPart[];
  has_instrumental_parts: boolean;
  original_midi_available: boolean;
  expanded_midi_available: boolean;
  diagnostic?: string | null;
};

export type PerformanceMeasureMapEntry = {
  played_measure_index: number;
  source_measure_index: number;
  source_measure_number: string;
  pass_index: number;
  start_seconds: number;
  end_seconds: number;
};

export type PerformanceMeasureMap = {
  written: PerformanceMeasureMapEntry[];
  expanded: PerformanceMeasureMapEntry[];
};

export type ScoreSummary = {
  title?: string | null;
  composer?: string | null;
  lyricist?: string | null;
  duration_seconds?: number;
  expanded_duration_seconds?: number;
  // False when the score's repeat marks give no played order; it then sings
  // only in written order.
  repeat_expansion_available?: boolean;
  performance_measure_map?: PerformanceMeasureMap;
  performance_midi?: PerformanceMidi;
  parts?: ScoreSummaryPart[];
  available_verses?: Array<string | number>;
  selected_verse_number?: string | number | null;
};

export type VoicebankOption = {
  id: string;
  name: string;
  profile_gender?: string | null;
  default_voice_color?: string | null;
  profile_image?: string | null;
  selector_image?: string | null;
};

export type UploadResponse = {
  session_id: string;
  score_id?: string;
  parsed: boolean;
  current_score?: unknown;
  score_summary?: ScoreSummary | null;
  performance_midi?: PerformanceMidi | null;
  solfege_settings?: SolfegeSettings;
  /** The Song this upload made or updated. Absent on a backend without Songs. */
  song?: SongInfo;
};

export type SongInfo = {
  id: string;
  title: string;
  title_edited: boolean;
  created_at: string;
  last_active_at?: string;
  /** Present on a workspace's Song: whether this user can share, and the link while it is on. */
  share?: SongShare;
  /** Present on a workspace's Song: its credits, license and description. */
  info?: SongInfoFields;
};

export type SongLicense = "all_rights_reserved" | "cc_by_4" | "public_domain";

/** A Song's info (docs/specs/song-info-spec.md). Voices are read-only: the sung tracks' voicebanks. */
export type SongInfoFields = {
  title: string;
  subtitle: string;
  composer: string;
  lyricist: string;
  arranger: string;
  /** The score's printed credit boxes, as printed. Absent from a backend before it. */
  credits?: string;
  score_copyright: string;
  producer: string;
  description: string;
  license: SongLicense;
  voices: string[];
};

/** The fields the owner can edit, besides the title. */
export type SongInfoEdit = Partial<Omit<SongInfoFields, "title" | "voices">>;

export const SONG_LICENSES: Record<SongLicense, { label: string; url: string | null }> = {
  all_rights_reserved: { label: "All rights reserved", url: null },
  cc_by_4: { label: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/" },
  public_domain: { label: "Public domain (CC0)", url: "https://creativecommons.org/publicdomain/zero/1.0/" },
};

export type SongShare = {
  available: boolean;
  enabled: boolean;
  url: string | null;
};

/** A row of the My songs list. */
export type SongRow = {
  id: string;
  title: string;
  created_at: string;
  last_active_at: string;
  status: "idle" | "working";
  track_count: number;
  /** True while the Song's share link is on. */
  shared?: boolean;
  /** On the answer to a Song update. */
  info?: SongInfoFields;
};

export type WorkspaceMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  /** On a Sing All job's entries (started, paused, ended): the chat shows the job's card once,
   * at the first, in place of their text, which is context for the chat model. */
  singAllJobId?: string;
};

/** A job's progress payload, plus what places it in the rebuilt chat. */
export type WorkspaceJob = ProgressResponse & {
  originating_turn_id: string | null;
  created_at: string;
  progress_url: string;
};

export type WorkspaceVocal = {
  key: string;
  part_id?: string | null;
  part_index?: number | null;
  label: string;
  verse_number?: string | number | null;
  take_signature?: string | null;
  source_job_id: string;
  expand_repeats?: boolean | null;
  duration_seconds?: number | null;
  voice_name?: string | null;
  /** Absent on a track saved before Sing All: it counts as lyrics. */
  mode?: SingAllTakeMode;
  muted: boolean;
  solo: boolean;
  volume: number;
  audio_url: string;
  progress_url: string;
};

export type WorkspaceInstrument = {
  raw_part_id: string;
  muted: boolean;
  solo: boolean;
  volume: number;
  gm_program: number | null;
  soundfont_bank: number | null;
  preset_kind: "melodic" | "percussion_kit" | null;
  unassigned: boolean;
};

export type WorkspaceBus = { muted: boolean; solo: boolean; level_db: number };

export type WorkspacePlayer = {
  score_id: string | null;
  revision: number;
  repeats: boolean;
  vocals: WorkspaceVocal[];
  instruments: WorkspaceInstrument[];
  bus: WorkspaceBus;
};

export type SongWorkspace = {
  session_id: string;
  song: SongInfo;
  score: {
    score_id: string | null;
    current_score?: unknown;
    score_summary?: ScoreSummary | null;
    solfege_settings?: SolfegeSettings;
  };
  messages: WorkspaceMessage[];
  jobs: WorkspaceJob[];
  player: WorkspacePlayer;
};

/** A shared Song as the public page gets it: no ids, paths or storage links. */
export type ShareVocal = {
  key: string;
  mode: SingAllTakeMode;
  label: string;
  verse_number?: string | number | null;
  duration_seconds?: number | null;
  voice_name?: string | null;
  muted: boolean;
  solo: boolean;
  volume: number;
  audio_url: string;
};

export type ShareSummary = {
  duration_seconds?: number | null;
  expanded_duration_seconds?: number | null;
  repeat_expansion_available?: boolean | null;
  performance_measure_map?: PerformanceMeasureMap | null;
  performance_midi: Partial<PerformanceMidi> | null;
};

export type SharePlayer = {
  repeats: boolean;
  vocals: ShareVocal[];
  instruments: WorkspaceInstrument[];
  bus: WorkspaceBus;
  midi_url: string | null;
};

export type SharePageData = {
  title: string;
  is_owner: boolean;
  song_id: string | null;
  score: { version: number | null; url: string; summary: ShareSummary };
  player: SharePlayer;
  credits: { voices: string[] };
  /** Absent from a backend before Song Info. */
  info?: SongInfoFields;
};

export type PlayerMixRequest = {
  score_id: string | null;
  vocals: Array<{ key: string; muted: boolean; solo: boolean; volume: number }>;
  instruments: WorkspaceInstrument[];
  bus: WorkspaceBus;
};

export type SolfegeSystem = "movable_do" | "fixed_do";
export type SolfegeMode = "major" | "minor_la_based" | "minor_do_based";

export type SolfegeSettings = {
  system: SolfegeSystem;
  mode: SolfegeMode;
  revision: number;
};

export type SolfegeSettingsResponse = {
  status?: "ready";
  settings: SolfegeSettings;
  score_version: number;
  updated_generated_verses?: Array<{
    part_id: string;
    part_index?: number;
    verse_number: string;
    notes_updated: number;
  }>;
  current_score?: unknown;
  score_summary?: ScoreSummary | null;
};

export type ChatResponse =
  | {
      type: "chat_text";
      message: string;
      /** The chat model opened Sing All for this language: the bubble shows the card. */
      singAll?: SingAllOpen;
      stopped?: boolean;
      current_score?: unknown;
      score_summary?: ScoreSummary | null;
      /** The repeat order this turn's quote used, when chat changed it; the With Repeats switch adopts it. */
      expand_repeats?: boolean;
      details?: unknown;
      warning?: string;
      solfege_settings?: SolfegeSettings;
    }
  | {
      type: "chat_audio";
      message: string;
      audio_url: string;
      /** Required to cache or refresh the take; the backend no longer sends this type. */
      job_id?: string;
      audio_track?: AudioTrackMetadata;
      expand_repeats?: boolean;
      current_score?: unknown;
      score_summary?: ScoreSummary | null;
      details?: unknown;
      warning?: string;
      solfege_settings?: SolfegeSettings;
    }
  | {
      type: "chat_progress";
      message: string;
      progress_url: string;
      job_id?: string;
      expand_repeats?: boolean;
      current_score?: unknown;
      score_summary?: ScoreSummary | null;
      details?: unknown;
      warning?: string;
      solfege_settings?: SolfegeSettings;
    }
  | { type: "chat_error"; message: string; details?: unknown };

export type ProgressResponse = {
  score_id?: string;
  score_version_no?: number;
  result_score_ref?: { scoreId: string; scoreVersionNo: number };
  status: "idle" | "queued" | "running" | "done" | "stopped" | "error" | "action_required";
  message?: string;
  step?: string;
  progress?: number;
  audio_url?: string;
  audio_track?: AudioTrackMetadata;
  /** Whether the take was rendered with repeats; absent for jobs from before it was recorded. */
  expand_repeats?: boolean;
  job_id?: string;
  job_kind?: string;
  review_required?: boolean;
  action_required?: unknown;
  error?: string;
  details?: unknown;
  warning?: string;
  feedback?: FeedbackPromptState;
  actual_duration_seconds?: number;
  consumed_credits?: number;
  required_credits?: number;
  billable_duration_seconds?: number;
  billing?: Record<string, unknown>;
  credit_breakdown?: Record<string, unknown>;
  performance_midi?: PerformanceMidi;
  /** True when this job actually wrote MIDI files, not merely billed for them. */
  performance_midi_published?: boolean;
};

export type SynthesisCreditEstimate = {
  pricing_version: number;
  vocal_part_id?: string | null;
  expand_repeats: boolean;
  vocal_duration_seconds: number;
  vocal_part: { pricing_unit_seconds: number; estimated_credits: number };
  instrumentals: {
    has_instrumental_parts: boolean;
    charge_required: boolean;
    charge_scope?: string | null;
    pricing_expand_repeats?: boolean | null;
    pricing_duration_seconds?: number | null;
    pricing_unit_seconds: number;
    estimated_credits: number;
    charged_once_for_all_tracks: boolean;
  };
  billing_components: string[];
  total_estimated_credits: number;
};

/** A take in the score player, so a quote can say which takes a render removes. */
export type ScorePlayerTake = {
  part_id: string | null;
  label: string;
  expand_repeats: boolean;
  take_signature?: string | null;
};

export type ChatStreamEvent = {
  event:
    | "progress"
    | "completed"
    | "failed"
    | "action-required"
    | "heartbeat"
    | "stream-error";
  data: ProgressResponse;
};

export type ChatStream = AsyncIterable<ChatStreamEvent> & {
  cancel: () => Promise<void>;
};

export type ChatRequestResult = {
  response: ChatResponse;
  stream?: ChatStream;
};

export type ExportMixTrackRequest = {
  job_id: string;
  part_id: string;
  key?: string;
  label?: string;
  verse_number?: string | number | null;
  muted: boolean;
  solo: boolean;
  volume: number;
};

export type ExportMixResponse = {
  status: "queued";
  progress_url: string;
  job_id: string;
  required_credits: number;
  billable_duration_seconds: number;
};

export type AudioTrackMetadata = {
  key?: string;
  label?: string;
  part_id?: string | null;
  part_index?: number | null;
  verse_number?: string | number | null;
  /** The part's take_signature when the take was rendered. */
  take_signature?: string | null;
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

export type TopupCheckoutResponse = {
  url: string;
  maxQuantity: number;
};

export type EmbeddedCheckoutRequest =
  | {
      checkoutType: "topup";
      packKey: "topup_15";
    }
  | {
      checkoutType: "subscription";
      planKey: string;
    };

export type EmbeddedCheckoutResponse = {
  checkoutSessionId: string;
  clientSecret: string;
  checkoutType: "topup" | "subscription";
  maxQuantity?: number;
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

export type TopupCheckoutSyncResponse = {
  synced: boolean;
  status: string;
  topupCredits?: {
    totalRemaining: number;
    totalReserved: number;
    totalAvailable: number;
    activePackCount: number;
  };
};

export type TopupCheckoutCancelResponse = {
  cancelled: boolean;
  status: string;
  stripeExpired?: boolean;
};

export type VoicebankListResponse = {
  voicebanks: VoicebankOption[];
};

export type MaintenanceStatusResponse = {
  enabled: boolean;
  allowed: boolean;
  message?: string | null;
};

export type TurnstileVerifyResponse = {
  success: boolean;
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
export const SYNTHESIS_JOB_DETAILS_TIMEOUT_TITLE =
  "We couldn’t receive your job details";
export const SYNTHESIS_JOB_DETAILS_TIMEOUT_BODY =
  "Your synthesis may still complete and use credits, but we can’t retrieve the result automatically. If synthesis fails, the normal rollback releases its reserved credits.\n\nPlease contact support to check the outcome, help retrieve any completed audio, and resolve any credit issue.";
export const SYNTHESIS_JOB_DETAILS_TIMEOUT_MESSAGE =
  `${SYNTHESIS_JOB_DETAILS_TIMEOUT_TITLE}\n\n${SYNTHESIS_JOB_DETAILS_TIMEOUT_BODY}`;
const BACKEND_READY_TIMEOUT_SECONDS = parsePositiveNumber(
  import.meta.env.VITE_BACKEND_READY_TIMEOUT_SECONDS,
  240
);
const API_REQUEST_TIMEOUT_SECONDS = parsePositiveNumber(
  import.meta.env.VITE_API_REQUEST_TIMEOUT_SECONDS,
  300
);

/**
 * A failed request whose body named a code, e.g. `song_not_found` or
 * `stop_too_late`. Callers branch on `code`, never on the message text.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  /** The error body's other fields, e.g. `activeSongId` beside `sing_all_already_running`. */
  readonly detail: Record<string, unknown>;

  constructor(message: string, status: number, code?: string, detail: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

class BackendStartingError extends Error {
  constructor(message = BACKEND_STARTING_MESSAGE) {
    super(message);
    this.name = "BackendStartingError";
  }
}

class ApiRequestTimeoutError extends Error {
  constructor(message = "The request took too long. Please try again.") {
    super(message);
    this.name = "ApiRequestTimeoutError";
  }
}

class SynthesisJobDetailsTimeoutError extends Error {
  constructor() {
    super(SYNTHESIS_JOB_DETAILS_TIMEOUT_MESSAGE);
    this.name = "SynthesisJobDetailsTimeoutError";
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

function normalizeProgressResponse(payload: ProgressResponse): ProgressResponse {
  if (!payload.audio_url) return payload;
  return { ...payload, audio_url: withApiBase(payload.audio_url) };
}

async function nextBeforeDeadline<T>(
  iterator: AsyncIterator<T>,
  deadlineMs: number,
  createTimeoutError: () => Error
): Promise<IteratorResult<T>> {
  const remainingMs = deadlineMs - Date.now();
  if (remainingMs <= 0) throw createTimeoutError();
  return new Promise<IteratorResult<T>>((resolve, reject) => {
    const timeoutId = window.setTimeout(
      () => reject(createTimeoutError()),
      remainingMs
    );
    iterator.next().then(
      (result) => {
        window.clearTimeout(timeoutId);
        resolve(result);
      },
      (error) => {
        window.clearTimeout(timeoutId);
        reject(error);
      }
    );
  });
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

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutSeconds = API_REQUEST_TIMEOUT_SECONDS,
  createAbortError: () => Error = () => new ApiRequestTimeoutError()
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(
    () => controller.abort(),
    timeoutSeconds * 1000
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
      throw createAbortError();
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function timeoutForPath(path: string): number {
  return path === "/readyz" ? BACKEND_READY_TIMEOUT_SECONDS : API_REQUEST_TIMEOUT_SECONDS;
}

function abortErrorForPath(path: string): Error {
  return path === "/readyz" ? new BackendStartingError() : new ApiRequestTimeoutError();
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
  // A 503 the Sing All quote answers on purpose: the card offers "Try again".
  const quoteUnavailable = errorCodeFromBody(text) === "quote_unavailable";
  if (!quoteUnavailable && (startupMessage || response.status === 503 || localProxyBackendDown)) {
    return new BackendStartingError(startupMessage || BACKEND_STARTING_MESSAGE);
  }
  return new ApiError(
    errorMessageFromBody(text) || fallback,
    response.status,
    errorCodeFromBody(text) ?? undefined,
    errorDetailFromBody(text)
  );
}

function errorDetailFromBody(text: string): Record<string, unknown> {
  try {
    const detail = JSON.parse(text)?.detail;
    return detail && typeof detail === "object" && !Array.isArray(detail) ? detail : {};
  } catch {
    return {};
  }
}

function errorCodeFromBody(text: string): string | null {
  try {
    const payload = JSON.parse(text);
    const code = payload?.code ?? payload?.detail?.code;
    return typeof code === "string" && code ? code : null;
  } catch {
    return null;
  }
}

function errorMessageFromBody(text: string): string | null {
  if (!text.trim()) return null;
  try {
    const payload = JSON.parse(text);
    const detail = payload?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (detail && typeof detail.message === "string" && detail.message.trim()) {
      return detail.message;
    }
    if (typeof payload?.message === "string" && payload.message.trim()) {
      return payload.message;
    }
  } catch {
    // Non-JSON error bodies are already user-readable text in local/dev cases.
  }
  return text;
}

async function request<T>(path: string, options: RequestInit): Promise<T> {
  let headers = await withAppCheckHeaders(options.headers);
  headers = await withAuthHeaders(headers);
  const response = await fetchWithTimeout(
    `${API_BASE}${path}`,
    { ...options, headers },
    timeoutForPath(path),
    () => abortErrorForPath(path)
  );
  if (!response.ok) {
    throw await errorFromResponse(response, `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function createSession(): Promise<{ session_id: string }> {
  return request("/sessions", { method: "POST" });
}

export type StopWorkResponse = {
  result: "stopped" | "too_late" | "not_stoppable" | "nothing_running";
  jobs: Array<{
    jobId: string;
    jobKind: string;
    result: "stopped" | "too_late" | "not_stoppable";
    refundedCredits: number;
  }>;
  /** The Song's running Sing All, when there was one. */
  singAll?: { jobId: string; result: "stopping" | "too_late" } | null;
};

export async function stopSessionWork(sessionId: string): Promise<StopWorkResponse> {
  return request(`/sessions/${sessionId}/stop`, { method: "POST" });
}

export type CreditsBootstrapResponse = {
  balance: number;
  reserved: number;
  available: number;
  expires_at: string | null;
  overdrafted: boolean;
  is_expired: boolean;
  monthly_allowance: number | null;
  last_grant_type: string | null;
  last_grant_at: string | null;
};

export async function ensureCredits(): Promise<CreditsBootstrapResponse> {
  return request("/credits", { method: "GET" });
}

export async function fetchMaintenanceStatus(): Promise<MaintenanceStatusResponse> {
  return request("/maintenance/status", { method: "GET" });
}

export async function fetchBackendReadiness(): Promise<BackendReadyzResponse> {
  return request("/readyz", { method: "GET" });
}

export async function verifyTurnstileToken(token: string): Promise<TurnstileVerifyResponse> {
  return request("/auth/turnstile/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
}

export async function createCheckoutSession(planKey: string): Promise<BillingCheckoutResponse> {
  return request("/billing/checkout-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ planKey }),
  });
}

export async function createTopupCheckoutSession(packKey = "topup_15"): Promise<TopupCheckoutResponse> {
  return request("/billing/topup-checkout-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ packKey }),
  });
}

export async function createEmbeddedCheckoutSession(
  body: EmbeddedCheckoutRequest
): Promise<EmbeddedCheckoutResponse> {
  return request("/billing/embedded-checkout-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function syncCheckoutSession(sessionId: string): Promise<BillingCheckoutSyncResponse> {
  return request("/billing/checkout-session/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
}

export async function syncTopupCheckoutSession(sessionId: string): Promise<TopupCheckoutSyncResponse> {
  return request("/billing/topup-checkout-session/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
}

export async function cancelTopupCheckoutSession(sessionId: string): Promise<TopupCheckoutCancelResponse> {
  return request("/billing/topup-checkout-session/cancel", {
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

export type UploadScoreOptions = {
  /** The user chose Replace: stop the Song's running work, then upload. */
  replace?: boolean;
};

export async function uploadScore(
  sessionId: string,
  file: File,
  options: UploadScoreOptions = {}
): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  if (options.replace) form.append("replace", "true");
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithTimeout(
    `${API_BASE}/sessions/${sessionId}/upload`,
    {
      method: "POST",
      body: form,
      headers,
    },
    API_REQUEST_TIMEOUT_SECONDS,
    () => new ApiRequestTimeoutError("Upload took too long. Please try again.")
  );
  if (!response.ok) {
    throw await errorFromResponse(response, "Upload failed");
  }
  return response.json();
}

export async function fetchScoreXml(sessionId: string): Promise<string> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithTimeout(`${API_BASE}/sessions/${sessionId}/score`, {
    headers,
    // This URL remains stable while score transforms replace its contents.  Do
    // not let a prior upload response hide a newly generated lyric verse.
    cache: "no-store",
  });
  if (!response.ok) {
    throw await errorFromResponse(response, "Failed to load score.");
  }
  return response.text();
}

export async function fetchWorkspace(songId: string): Promise<SongWorkspace> {
  const workspace = await request<SongWorkspace>(`/songs/${songId}/workspace`, { method: "GET" });
  return {
    ...workspace,
    jobs: workspace.jobs.map((job) => ({ ...job, ...normalizeProgressResponse(job) })),
    player: {
      ...workspace.player,
      vocals: workspace.player.vocals.map((vocal) => ({
        ...vocal,
        audio_url: withApiBase(vocal.audio_url),
      })),
    },
  };
}

export async function listSongs(limit = 50): Promise<SongRow[]> {
  const response = await request<{ songs: SongRow[] }>(`/songs?limit=${limit}`, { method: "GET" });
  return response.songs;
}

export async function renameSong(songId: string, title: string): Promise<SongRow> {
  return request<SongRow>(`/songs/${songId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

/** Rename the Song and edit its info in one request. */
export async function updateSongInfo(
  songId: string,
  changes: { title?: string; info?: SongInfoEdit }
): Promise<SongRow> {
  return request<SongRow>(`/songs/${songId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export async function deleteSong(songId: string): Promise<void> {
  await request<{ deleted: boolean }>(`/songs/${songId}`, { method: "DELETE" });
}

export async function shareSong(
  songId: string,
  noticeAccepted = false
): Promise<{ enabled: boolean; url: string | null }> {
  return request(`/songs/${songId}/share`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(noticeAccepted ? { notice_accepted: true } : {}),
  });
}

export async function unshareSong(songId: string): Promise<{ enabled: boolean; url: string | null }> {
  return request(`/songs/${songId}/share`, { method: "DELETE" });
}

/** The public page's data. A signed-in viewer's token is sent, so the owner is recognised. */
export async function fetchSharePage(token: string): Promise<SharePageData> {
  return request<SharePageData>(`/public/s/${encodeURIComponent(token)}`, {
    method: "GET",
    cache: "no-store",
  });
}

/** A public share file (score, MIDI or take), with the App Check header and no sign-in. */
async function fetchShareFile(url: string, fallback: string): Promise<Response> {
  const response = await fetchWithTimeout(withApiBase(url), {
    headers: await withAppCheckHeaders(),
    cache: "no-store",
  });
  if (!response.ok) {
    throw await errorFromResponse(response, fallback);
  }
  return response;
}

export async function fetchShareScore(url: string): Promise<string> {
  return (await fetchShareFile(url, "Failed to load score.")).text();
}

export async function fetchShareMidi(url: string): Promise<Blob> {
  return (await fetchShareFile(url, "Failed to load instrumental MIDI.")).blob();
}

export async function fetchShareAudio(url: string): Promise<ArrayBuffer> {
  return (await fetchShareFile(url, "Failed to load a sung part.")).arrayBuffer();
}

export async function savePlayerMix(
  songId: string,
  mix: PlayerMixRequest
): Promise<{ revision: number }> {
  return request<{ revision: number }>(`/songs/${songId}/player/mix`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(mix),
    // The last save is sent as the tab is hidden or closed; let it outlive the page.
    keepalive: true,
  });
}

export async function fetchInstrumentalMidi(
  sessionId: string,
  expandRepeats: boolean
): Promise<Blob> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const query = new URLSearchParams({ expand_repeats: String(expandRepeats) });
  const response = await fetchWithTimeout(
    `${API_BASE}/sessions/${sessionId}/instrumental-midi?${query.toString()}`,
    { headers, cache: "no-store" }
  );
  if (!response.ok) {
    throw await errorFromResponse(response, "Failed to load instrumental MIDI.");
  }
  return response.blob();
}

export async function fetchSolfegeSettings(
  sessionId: string
): Promise<SolfegeSettingsResponse> {
  return request<SolfegeSettingsResponse>(`/sessions/${sessionId}/solfege-settings`, {
    method: "GET",
  });
}

export async function updateSolfegeSettings(
  sessionId: string,
  settings: Pick<SolfegeSettings, "system" | "mode">
): Promise<SolfegeSettingsResponse> {
  return request<SolfegeSettingsResponse>(`/sessions/${sessionId}/solfege-settings`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings }),
  });
}

export async function fetchVoicebanks(): Promise<VoicebankOption[]> {
  const response = await request<VoicebankListResponse>("/api/voicebanks", { method: "GET" });
  return Array.isArray(response.voicebanks) ? response.voicebanks : [];
}

export async function chat(
  sessionId: string,
  message: string,
  selectedVoicebankId?: string | null,
  expandRepeats = true,
  scorePlayerTakes: ScorePlayerTake[] = []
): Promise<ChatRequestResult> {
  const body: {
    message: string;
    selected_voicebank_id?: string;
    expand_repeats: boolean;
    score_player_takes: ScorePlayerTake[];
  } = { message, expand_repeats: expandRepeats, score_player_takes: scorePlayerTakes };
  if (selectedVoicebankId) {
    body.selected_voicebank_id = selectedVoicebankId;
  }
  let headers = await withAppCheckHeaders({
    "Content-Type": "application/json",
    Accept: "text/event-stream, application/json",
  });
  headers = await withAuthHeaders(headers);
  const controller = new AbortController();
  const acceptedDeadlineMs = Date.now() + API_REQUEST_TIMEOUT_SECONDS * 1000;
  const response = await fetchWithTimeout(
    `${API_BASE}/sessions/${sessionId}/chat`,
    {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    },
    API_REQUEST_TIMEOUT_SECONDS
  );
  if (!response.ok) {
    throw await errorFromResponse(response, `Request failed: ${response.status}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("text/event-stream")) {
    return { response: normalizeChatResponse((await response.json()) as ChatResponse) };
  }
  if (!response.body) {
    throw new Error("Synthesis stream did not provide a response body.");
  }

  const reader = response.body.getReader();
  const iterator = parseServerSentEvents(reader)[Symbol.asyncIterator]();
  let first: IteratorResult<{ event: string; data: unknown }>;
  try {
    first = await nextBeforeDeadline(
      iterator,
      acceptedDeadlineMs,
      () => new SynthesisJobDetailsTimeoutError()
    );
  } catch (error) {
    controller.abort();
    await reader.cancel().catch(() => undefined);
    throw error;
  }
  if (first.done || first.value.event !== "accepted") {
    controller.abort();
    await reader.cancel().catch(() => undefined);
    throw new Error("Synthesis stream ended before the job was accepted.");
  }
  const accepted = normalizeChatResponse(first.value.data as ChatResponse);
  const normalizedIterator = normalizeChatStreamEvents(iterator)[Symbol.asyncIterator]();
  const stream: ChatStream = {
    [Symbol.asyncIterator]() {
      return normalizedIterator;
    },
    async cancel() {
      controller.abort();
      await reader.cancel().catch(() => undefined);
    },
  };
  return { response: accepted, stream };
}

async function* normalizeChatStreamEvents(
  iterator: AsyncIterator<{ event: string; data: unknown }>
): AsyncGenerator<ChatStreamEvent> {
  for (;;) {
    const next = await iterator.next();
    if (next.done) return;
    yield {
      event: next.value.event as ChatStreamEvent["event"],
      data: normalizeProgressResponse(next.value.data as ProgressResponse),
    };
  }
}

function normalizeChatResponse(response: ChatResponse): ChatResponse {
  if (response.type === "chat_audio") {
    return { ...response, audio_url: withApiBase(response.audio_url) };
  }
  if (response.type === "chat_progress") {
    return { ...response, progress_url: withApiBase(response.progress_url) };
  }
  return response;
}

async function* parseServerSentEvents(
  reader: ReadableStreamDefaultReader<Uint8Array>
): AsyncGenerator<{ event: string; data: unknown }> {
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = buffer.search(/\r?\n\r?\n/);
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        const separator = buffer.slice(boundary).match(/^\r?\n\r?\n/)?.[0] || "\n\n";
        buffer = buffer.slice(boundary + separator.length);
        const parsed = parseServerSentEventBlock(block);
        if (parsed) yield parsed;
        boundary = buffer.search(/\r?\n\r?\n/);
      }
      if (done) break;
    }
    const parsed = parseServerSentEventBlock(buffer);
    if (parsed) yield parsed;
  } finally {
    reader.releaseLock();
  }
}

function parseServerSentEventBlock(block: string): { event: string; data: unknown } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) };
}

export async function fetchSynthesisEstimate(
  sessionId: string,
  expandRepeats: boolean,
  partId?: string | null
): Promise<SynthesisCreditEstimate> {
  const params = new URLSearchParams({
    expand_repeats: String(expandRepeats),
  });
  if (partId) params.set("part_id", partId);
  const response = await request<{ synthesis_credit_estimate: SynthesisCreditEstimate }>(
    `/sessions/${sessionId}/synthesis-estimate?${params.toString()}`,
    { method: "GET" }
  );
  return response.synthesis_credit_estimate;
}

export async function fetchProgress(progressUrl: string): Promise<ProgressResponse> {
  let headers = await withAppCheckHeaders();
  headers = await withAuthHeaders(headers);
  const response = await fetchWithTimeout(withApiBase(progressUrl), {
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    throw await errorFromResponse(response, `Request failed: ${response.status}`);
  }
  return normalizeProgressResponse((await response.json()) as ProgressResponse);
}

export async function exportMix(
  sessionId: string,
  tracks: ExportMixTrackRequest[],
  billingReferenceJobId: string
): Promise<ExportMixResponse> {
  const payload = await request<ExportMixResponse>(`/sessions/${sessionId}/export-mix`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      format: "wav",
      billing_reference_job_id: billingReferenceJobId,
      tracks,
    }),
  });
  return {
    ...payload,
    progress_url: withApiBase(payload.progress_url),
  };
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

// Sing All (docs/specs/sing-all-core-step3-spec.md, "API contract").

export type SingAllMode = "lyrics" | "solfege" | "both";
export type SingAllTakeMode = "lyrics" | "solfege";

export type SingAllOpen = { language: string; solfegeOnly: boolean };

/** Which mix files a Sing All makes: full and part-dominant, full only, or none. */
export type SingAllMixes = "all" | "full" | "none";

export type SingAllRowChoice = {
  rowId: string;
  include: boolean;
  voice: string;
  voicePicked: "recommended" | "user";
};

export type SingAllChoices = {
  language?: string;
  solfegeOnly?: boolean;
  mode?: SingAllMode;
  verse?: string;
  repeats?: boolean;
  rows?: SingAllRowChoice[];
  styleNote?: string;
  mixes?: SingAllMixes;
};

export type SingAllRow = SingAllRowChoice & {
  sourcePartId: string;
  partName: string;
  partId: string | null;
  name: string;
  split: boolean;
  hasWords: boolean;
  note: null | "no_words" | "likely_instrument" | "undecided" | "voice_out_of_range";
  pitchRange: { lowest_note?: string; highest_note?: string };
};

export type SingAllLine =
  | { kind: "singing"; parts: number; takes: number; secondsEach: number; credits: number }
  | { kind: "preparing"; credits: number }
  | { kind: "instrumentals"; credits: number }
  | { kind: "mixes"; files: number; credits: number }
  | { kind: "total"; credits: number }
  | { kind: "balance"; before: number; after: number };

export type SingAllNote = {
  code: string;
  partId: string | null;
  partName: string | null;
};

export type SingAllControls = {
  voiceColor?: string | null;
  airiness?: number;
  clarity?: number;
  gender?: number;
  intensity?: number;
};

export type SingAllQuote = {
  quoteId: string;
  blocked: null | "free_plan" | "nothing_to_sing" | "not_enough_credits" | "style_unavailable";
  rows: SingAllRow[];
  voices: Array<{ id: string; name: string }>;
  options: {
    language: string;
    languages: Array<{ code: string; label: string | null }>;
    solfegeOnly: boolean;
    mode: SingAllMode;
    verse: string;
    verses: string[];
    repeats: boolean;
    repeatsAvailable: boolean;
    /** The choice in effect: "all" is answered as "full" when no instrument sounds. Absent before step 4. */
    mixes?: SingAllMixes;
    /** How many mix files each choice makes; equal when no instrument sounds. Absent before step 4. */
    mixFiles?: { all: number; full: number };
  };
  lines: SingAllLine[];
  total: number;
  balance: number;
  notes: SingAllNote[];
  style: {
    note: string;
    controls: Record<string, SingAllControls>;
    changes: Record<string, SingAllControls>;
  };
  choicesHash: string;
};

export type SingAllQuoteBlocked =
  | { blocked: "already_running"; activeJobId: string; activeSongId: string }
  | { blocked: "no_voice_for_language"; language: string; supported: string[] };

export type SingAllQuoteResponse = SingAllQuote | SingAllQuoteBlocked;

export const isSingAllQuote = (response: SingAllQuoteResponse): response is SingAllQuote =>
  "quoteId" in response && typeof response.quoteId === "string";

export type SingAllItemStatus = "queued" | "running" | "done" | "failed" | "stopped" | "skipped";
export type SingAllStatus = "running" | "needs_decision" | "completed" | "partial" | "failed" | "cancelled";

/** A paused job's question (docs/specs/sing-all-failures-step6-spec.md, "API"). */
export type SingAllDecision = {
  pause: number;
  stage: "splitting" | "solfege";
  deadline: string;
  parts: Array<{
    sourcePartId: string;
    name: string;
    state: "review" | "failed";
    reason: "split_review" | "split_failed" | "parts_changed" | "solfege_failed";
    /** The split's own explanation; null for solfège. */
    message: string | null;
    madeParts: string[];
    rows: number;
    heldCredits: number;
  }>;
  /** Everything not paused: the other rows' takes, the instrumentals and the mixes. */
  keep: { rows: number; heldCredits: number };
};

export type SingAllRetryQuote = {
  jobId: string;
  takes: Array<{ rowId: string; name: string; mode: SingAllTakeMode; voiceName: string; credits: number }>;
  instrumentals: number;
  mixes: null | { files: number; credits: number; included: boolean };
  total: number;
};
export type SingAllStage = "reading" | "splitting" | "solfege" | "singing" | "packaging" | "done";

export type SingAllProgress = {
  jobId: string;
  songId: string;
  title: string;
  status: SingAllStatus;
  stage: SingAllStage;
  endReason: string | null;
  cancelRequested: boolean;
  /** The owner gets an email when the job ends (step 5); absent from older backends. */
  emailOnEnd?: boolean;
  /** Non-null only while the job is paused (status needs_decision); absent before step 6. */
  decision?: SingAllDecision | null;
  /** A take failed while singing and the job hasn't been retried. */
  retryable?: boolean;
  retryOf?: string | null;
  retriedBy?: string | null;
  choices: {
    language: string;
    solfegeOnly: boolean;
    mode: SingAllMode;
    verse: string;
    repeats: boolean;
    /** Absent from a backend before step 4. */
    mixes?: SingAllMixes;
  };
  counts: {
    splits: { done: number; total: number };
    takes: { done: number; failed: number; total: number; skipped?: number };
  };
  rows: Array<{
    rowId: string;
    name: string;
    voice: string;
    voiceName: string;
    split: boolean;
    takes: Array<{
      mode: SingAllTakeMode;
      status: SingAllItemStatus;
      reason: string | null;
      /** A retry job's take sung by an earlier job. */
      carried?: true;
    }>;
  }>;
  credits: { reserved: number; charged: number; refunded: number };
  receipt: Array<{
    itemId: string;
    type: "take" | "split" | "instrumental" | "mixes";
    rowId: string | null;
    mode: SingAllTakeMode | null;
    name: string | null;
    credits: number;
    outcome: "charged" | "refunded" | "included" | "earlier";
    reason: string | null;
    /** The number of mix files on a "mixes" row, else null. */
    files?: number | null;
  }>;
  notes: SingAllNote[];
  elapsedS: number;
  remainingS: number | null;
  render: { state: "none" | "waiting" | "running" | "ended"; placeInLine: number | null };
  /** The mixes and zip run: null until the packaging stage, and when no take was done. */
  package?: null | {
    state: "mixing" | "zipping" | "done" | "failed";
    mixes: { done: number; total: number };
  };
  createdAt: string;
  endedAt: string | null;
};

export async function requestSingAllQuote(
  songId: string,
  choices: SingAllChoices
): Promise<SingAllQuoteResponse> {
  return request("/sing-all/quote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ songId, choices }),
  });
}

/** "Sing it". Until the S3 backend lands it answers the job record: only `jobId` is relied on. */
export async function startSingAll(quoteId: string): Promise<SingAllProgress> {
  return request("/sing-all/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quoteId }),
  });
}

export async function fetchActiveSingAll(): Promise<SingAllProgress | null> {
  const response = await request<{ job: SingAllProgress | null }>("/sing-all/active", { method: "GET" });
  return response.job ?? null;
}

export async function fetchSingAllJob(jobId: string): Promise<SingAllProgress> {
  return request(`/sing-all/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
}

/** Answer a paused job: go on with what can be sung (accepting these review splits), or cancel. */
export async function answerSingAllDecision(
  jobId: string,
  answer: { action: "cancel" } | { action: "continue"; accept: string[]; heldCredits: number }
): Promise<SingAllProgress> {
  return request(`/sing-all/jobs/${encodeURIComponent(jobId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(answer),
  });
}

export async function fetchSingAllRetryQuote(jobId: string): Promise<SingAllRetryQuote> {
  return request(`/sing-all/jobs/${encodeURIComponent(jobId)}/retry`, { method: "GET" });
}

/** Start the retry; `total` is the mini quote's total the card showed. */
export async function startSingAllRetry(jobId: string, total: number): Promise<SingAllProgress> {
  return request(`/sing-all/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ total }),
  });
}

export type SingAllDownload = { url: string; fileName: string; expiresAt: string };

/** A short-lived link to a finished job's zip; the page navigates to it. */
export async function fetchSingAllDownload(jobId: string): Promise<SingAllDownload> {
  return request(`/sing-all/jobs/${encodeURIComponent(jobId)}/download`, { method: "GET" });
}
