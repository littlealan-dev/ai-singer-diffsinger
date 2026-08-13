import type { APIRequestContext, Page } from "@playwright/test";
import { firebaseIdToken } from "./auth";

const API_BASE = "http://127.0.0.1:8000";
const CONTROL_TOKEN = "local-ui-regression-control";

export type E2EState = {
  score_version?: number;
  history_length?: number;
  files?: Record<string, unknown>;
  current_score?: Record<string, unknown>;
  score_summary?: { parts?: Array<{ note_count?: number; part_name?: string }> } | null;
  synthesis?: {
    score_sha256?: string;
    source_musicxml_path?: string;
    part_id?: string;
    part_index?: number;
    lyric_selection?: { id?: string; number?: string; name?: string };
    output_path?: string;
    duration_seconds?: number;
  } | null;
  job?: {
    id?: string;
    status?: string;
    error?: string;
  } | null;
};

async function e2eRequest(
  page: Page,
  request: APIRequestContext,
  path: string,
  method: "GET" | "POST" = "GET",
) {
  const token = await firebaseIdToken(page);
  return request.fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "X-E2E-Control-Token": CONTROL_TOKEN,
    },
  });
}

export async function getE2EState(page: Page, request: APIRequestContext, sessionId: string) {
  const response = await e2eRequest(page, request, `/_e2e/sessions/${sessionId}/state`);
  if (!response.ok()) throw new Error(`Unable to read E2E state: ${response.status()}`);
  return response.json() as Promise<E2EState>;
}

export async function clearLocalArtifacts(page: Page, request: APIRequestContext, sessionId: string) {
  const response = await e2eRequest(
    page,
    request,
    `/_e2e/sessions/${sessionId}/clear-local-artifacts`,
    "POST",
  );
  if (!response.ok()) throw new Error(`Unable to clear local artifacts: ${response.status()}`);
}
