import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

const SESSION_ID = "stop-work-session";
const JOB_ID = "synthetic-job";
const SCORE_ID = "synthetic-score";
const PROGRESS_URL = `/sessions/${SESSION_ID}/progress?job_id=${JOB_ID}`;

type Scenario = "chat" | "take" | "too-late" | "split";

function silentWav(): Buffer {
  const dataBytes = 8_000;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + dataBytes, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(8_000, 24);
  header.writeUInt32LE(16_000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(dataBytes, 40);
  return Buffer.concat([header, Buffer.alloc(dataBytes)]);
}

async function mockBackend(page: Page, scenario: Scenario, uploadInitial = true) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let stopRequests = 0;
  let chatRequests = 0;
  let progressRequests = 0;
  let stopped = false;
  let uploadConflict = false;
  let releaseChat: () => void = () => {};
  const chatWait = new Promise<void>((resolve) => { releaseChat = resolve; });

  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (url.pathname === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (url.pathname === "/readyz") return json({ ready: true, status: "ready" });
    if (url.pathname === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (url.pathname === "/api/voicebanks") {
      return json({ voicebanks: [{ id: "synthetic", name: "Synthetic Voice" }] });
    }
    if (url.pathname === "/sessions") return json({ session_id: SESSION_ID });
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return uploadConflict
        ? json({ code: "work_running", message: "Stop the running work before uploading." }, 409)
        : json({ session_id: SESSION_ID, score_id: SCORE_ID, parsed: true });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/stop")) {
      stopRequests += 1;
      stopped = true;
      releaseChat();
      if (scenario === "chat") return json({ result: "nothing_running", jobs: [] });
      const result = scenario === "too-late" ? "too_late" : "stopped";
      return json({ result, jobs: [{ jobId: JOB_ID, jobKind: "synthesis", result, refundedCredits: result === "stopped" ? 3 : 0 }] });
    }
    if (url.pathname.endsWith("/chat")) {
      chatRequests += 1;
      if (chatRequests > 1) return json({ type: "chat_text", message: "Ready again." });
      if (scenario === "chat") {
        await chatWait;
        return json({ type: "chat_text", message: "Stopped.", stopped: true });
      }
      return json({ type: "chat_progress", message: "Preparing synthetic work.", job_id: JOB_ID, progress_url: PROGRESS_URL });
    }
    if (url.pathname.endsWith("/progress")) {
      progressRequests += 1;
      const kind = scenario === "split" ? "preprocess" : "synthesis";
      if (stopped && scenario === "take") {
        return json({ status: "stopped", job_id: JOB_ID, job_kind: kind, ...(uploadInitial ? { score_id: SCORE_ID } : {}), message: "Stopped." });
      }
      if (stopped && scenario === "too-late") {
        return json({
          status: "done", job_id: JOB_ID, job_kind: kind, score_id: SCORE_ID,
          message: "Your synthetic take is ready.", progress: 1,
          audio_url: `/sessions/${SESSION_ID}/audio?file=synthetic.wav`,
          audio_track: { key: "synthetic", label: "Synthetic", part_id: "synthetic", verse_number: "1" },
          actual_duration_seconds: 0.5,
        });
      }
      return json({ status: "running", job_id: JOB_ID, job_kind: kind, ...(uploadInitial ? { score_id: SCORE_ID } : {}), message: "Working on synthetic data.", progress: 0.3 });
    }
    if (url.pathname.endsWith("/audio")) {
      return route.fulfill({ contentType: "audio/wav", body: silentWav() });
    }
    return json({});
  });

  await signInAsE2EUser(page, `stop-${scenario}`);
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  if (uploadInitial) {
    await page.getByTestId("score-upload-input").setInputFiles({ name: "synthetic.xml", mimeType: "application/xml", buffer: xml });
    await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  }
  return { xml, stopRequests: () => stopRequests, progressRequests: () => progressRequests, setUploadConflict: () => { uploadConflict = true; } };
}

async function sendSyntheticRequest(page: Page) {
  await page.getByTestId("chat-input").fill("Sing a synthetic line");
  await page.getByTestId("send-message").click();
}

test("Stop during a chat reply sends the request and unlocks Send", async ({ page }) => {
  const backend = await mockBackend(page, "chat");
  await sendSyntheticRequest(page);
  await expect(page.getByTestId("stop-work")).toBeVisible();
  await page.getByTestId("stop-work").click();
  await expect.poll(backend.stopRequests).toBe(1);
  await expect(page.getByTestId("chat-stream")).toContainText("Stopped.");
  await page.getByTestId("chat-input").fill("A second synthetic line");
  await expect(page.getByTestId("send-message")).toBeEnabled();
  await page.getByTestId("send-message").click();
  await expect(page.getByTestId("chat-stream")).toContainText("Ready again.");
});

test("Stop during a take shows refund, adds no track, and unlocks Send", async ({ page }) => {
  const backend = await mockBackend(page, "take");
  await sendSyntheticRequest(page);
  await expect(page.getByTestId("stop-work")).toBeVisible();
  await page.getByTestId("stop-work").click();
  await expect.poll(backend.stopRequests).toBe(1);
  await expect(page.getByTestId("chat-stream")).toContainText("Stopped. Refunded 3 credits.");
  await expect(page.locator('[data-testid^="score-vocal-track-"]')).toHaveCount(0);
  await page.getByTestId("chat-input").fill("A second synthetic line");
  await expect(page.getByTestId("send-message")).toBeEnabled();
});

test("Too late to stop shows a notice and lets the take finish", async ({ page }) => {
  await mockBackend(page, "too-late");
  await sendSyntheticRequest(page);
  await expect(page.getByTestId("stop-work")).toBeVisible();
  await page.getByTestId("stop-work").click();
  await expect(page.getByRole("status")).toContainText("Too late to stop: it's just finishing");
  await expect(page.getByTestId("chat-stream")).toContainText("Your synthetic take is ready.");
  await expect(page.locator('[data-testid^="score-vocal-track-"]')).toHaveCount(1);
});

test("a running split never shows Stop", async ({ page }) => {
  const backend = await mockBackend(page, "split");
  await sendSyntheticRequest(page);
  await expect.poll(backend.progressRequests).toBeGreaterThan(0);
  await expect(page.getByTestId("stop-work")).toHaveCount(0);
});

test("upload and demo controls stay disabled during work, and 409 appears in chat", async ({ page }) => {
  const backend = await mockBackend(page, "take", false);
  await sendSyntheticRequest(page);
  await expect(page.getByRole("button", { name: "Add a score" })).toBeDisabled();
  await expect(page.locator(".demo-song-card").first()).toBeDisabled();
  await expect(page.getByRole("button", { name: "Add a score" })).toHaveAttribute("title", "Stop the running work first");
  await expect(page.getByTestId("score-upload-input")).toBeDisabled();
  await page.getByTestId("stop-work").click();
  await expect.poll(backend.stopRequests).toBe(1);
  await expect(page.getByTestId("score-upload-input")).toBeEnabled();
  backend.setUploadConflict();
  await page.getByTestId("score-upload-input").setInputFiles({ name: "another-synthetic.xml", mimeType: "application/xml", buffer: backend.xml });
  await expect(page.getByRole("alert")).toContainText("Stop the running work before uploading.");
});
