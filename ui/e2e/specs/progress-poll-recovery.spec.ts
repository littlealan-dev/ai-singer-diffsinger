import { expect, test, type Page, type Route } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

const SESSION_ID = "poll-recovery";
const SCORE_ID = "score-poll-recovery";
const JOB_ID = "job-poll-recovery";
const PROGRESS_URL = `/sessions/${SESSION_ID}/progress?job_id=${JOB_ID}`;
const INTERRUPTED_MESSAGE =
  "Preparing the singing line was interrupted before it finished. Please send your request again.";

type ProgressReply = { status: number; body?: Record<string, unknown> };

const running: ProgressReply = {
  status: 200,
  body: {
    status: "running",
    job_id: JOB_ID,
    job_kind: "preprocess",
    score_id: SCORE_ID,
    step: "preprocess",
    message: "Preparing the Alto part.",
    progress: 0.05,
  },
};
// What Cloud Run answers while the job's instance is being replaced.
const unavailable: ProgressReply = { status: 503 };

// A preprocess job answered with plain JSON progress, then polled. Each poll
// takes the next scripted reply; the last one repeats. Returns the time each
// progress request arrived.
async function startPolledPreprocessJob(
  page: Page,
  testId: string,
  replies: ProgressReply[]
): Promise<number[]> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const progressRequests: number[] = [];

  await page.route("http://127.0.0.1:8000/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (route.request().method() === "OPTIONS") {
      return route.fulfill({ status: 204 });
    }
    if (url.pathname === "/credits") {
      return json({
        balance: 100,
        reserved: 0,
        available: 100,
        is_expired: false,
        overdrafted: false,
      });
    }
    if (url.pathname === "/readyz") {
      return json({ ready: true, status: "ready" });
    }
    if (url.pathname === "/maintenance/status") {
      return json({ enabled: false, allowed: true });
    }
    if (url.pathname === "/api/voicebanks") {
      return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    }
    if (url.pathname === "/sessions") {
      return json({ session_id: SESSION_ID });
    }
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return json({ session_id: SESSION_ID, score_id: SCORE_ID, parsed: true });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/chat")) {
      return json({
        type: "chat_progress",
        message: "Preparing the Alto part.",
        job_id: JOB_ID,
        progress_url: PROGRESS_URL,
      });
    }
    if (url.pathname.endsWith("/progress")) {
      const reply = replies[Math.min(progressRequests.length, replies.length - 1)];
      progressRequests.push(Date.now());
      if (reply.body === undefined) {
        return route.fulfill({ status: reply.status, body: "Service Unavailable" });
      }
      return route.fulfill({ status: reply.status, json: reply.body });
    }
    return json({});
  });

  await signInAsE2EUser(page, testId);
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();

  await page.getByTestId("chat-input").fill("Sing the alto part");
  await page.getByTestId("send-message").click();
  return progressRequests;
}

test("an interrupted job is reported after polls fail while its instance is replaced", async ({
  page,
}) => {
  const progressRequests = await startPolledPreprocessJob(page, "poll-recovery-interrupted", [
    running,
    unavailable,
    unavailable,
    {
      status: 200,
      body: {
        status: "error",
        job_id: JOB_ID,
        job_kind: "preprocess",
        score_id: SCORE_ID,
        step: "error",
        message: INTERRUPTED_MESSAGE,
        error: INTERRUPTED_MESSAGE,
        progress: 1,
      },
    },
  ]);

  await expect(page.getByRole("alert")).toContainText(INTERRUPTED_MESSAGE);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
  expect(progressRequests).toHaveLength(4);
  await page.waitForTimeout(2500);
  expect(progressRequests).toHaveLength(4);
});

test("a single failed poll does not end polling of a running job", async ({ page }) => {
  const progressRequests = await startPolledPreprocessJob(page, "poll-recovery-blip", [
    running,
    unavailable,
    running,
    running,
    {
      status: 200,
      body: {
        status: "done",
        job_id: JOB_ID,
        job_kind: "preprocess",
        score_id: SCORE_ID,
        step: "done",
        message: "The Alto part is ready to sing.",
        progress: 1,
      },
    },
  ]);

  await expect(page.getByText("The Alto part is ready to sing.")).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(progressRequests).toHaveLength(5);
  await page.waitForTimeout(2500);
  expect(progressRequests).toHaveLength(5);
});

test("failed polls back off, and a successful poll restores the normal interval", async ({
  page,
}) => {
  const progressRequests = await startPolledPreprocessJob(page, "poll-recovery-backoff", [
    running,
    unavailable,
    unavailable,
    unavailable,
    running,
    running,
    {
      status: 200,
      body: {
        status: "done",
        job_id: JOB_ID,
        job_kind: "preprocess",
        score_id: SCORE_ID,
        step: "done",
        message: "The Alto part is ready to sing.",
        progress: 1,
      },
    },
  ]);

  await expect(page.getByText("The Alto part is ready to sing.")).toBeVisible();
  expect(progressRequests).toHaveLength(7);
  const gaps = progressRequests.slice(1).map((time, index) => time - progressRequests[index]);
  // After a success: 1.2 s. After the 1st, 2nd and 3rd failure in a row: 1.2,
  // 2.4 and 4.8 s. After the next success: 1.2 s again.
  const expected = [1200, 1200, 2400, 4800, 1200, 1200];
  gaps.forEach((gap, index) => {
    expect(gap, `gap ${index + 1}`).toBeGreaterThanOrEqual(expected[index] - 200);
    expect(gap, `gap ${index + 1}`).toBeLessThan(expected[index] + 1000);
  });
});
