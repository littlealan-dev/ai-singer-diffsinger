import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";


test("stream-error performs bounded recovery and unlocks chat", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true");
  });
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let progressRequests = 0;

  await page.route("http://127.0.0.1:8000/**", async (route) => {
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
      return json({ session_id: "stream-recovery" });
    }
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return json({
        session_id: "stream-recovery",
        score_id: "score-stream-recovery",
        parsed: true,
      });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/chat")) {
      const progressUrl =
        "/sessions/stream-recovery/progress?job_id=job-stream-recovery";
      const body = [
        "event: accepted",
        `data: ${JSON.stringify({
          type: "chat_progress",
          message: "Preparing take",
          job_id: "job-stream-recovery",
          progress_url: progressUrl,
        })}`,
        "",
        "event: stream-error",
        `data: ${JSON.stringify({
          status: "running",
          job_id: "job-stream-recovery",
          message: "We couldn’t confirm the final job status. Checking for updates…",
        })}`,
        "",
      ].join("\n");
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body,
      });
    }
    if (url.pathname.endsWith("/progress")) {
      progressRequests += 1;
      return json({
        status: "running",
        job_id: "job-stream-recovery",
        score_id: "score-stream-recovery",
        step: "render",
        message: "Rendering.",
        progress: 0.5,
      });
    }
    return json({});
  });

  await signInAsE2EUser(page, "synthesis-stream-recovery");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();

  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();

  await expect(page.getByRole("alert")).toContainText(
    "We couldn’t confirm the final job status"
  );
  await expect.poll(() => progressRequests).toBe(3);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await page.waitForTimeout(1500);
  expect(progressRequests).toBe(3);
});
