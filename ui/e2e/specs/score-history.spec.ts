import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { signInAsE2EUser } from "../auth";
import { LATEST_ANNOUNCEMENT_ID } from "../../src/announcements";

// Auth/Firestore are local emulators; render responses and media are deterministic.
test("re-upload preserves chat playback without restoring old mixer tracks", async ({ page }, testInfo) => {
  const autoplayErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("Recovered audio autoplay failed")) {
      autoplayErrors.push(message.text());
    }
  });
  const seed = await fetch("http://127.0.0.1:8080/v1/projects/demo-sightsinger-e2e/databases/(default)/documents/users/e2e-score-history", {
    method: "PATCH", headers: {
      Authorization: "Bearer owner",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ fields: { metadata: { mapValue: { fields: {
      lastSeenAnnouncementId: { stringValue: LATEST_ANNOUNCEMENT_ID },
    } } } } }),
  });
  expect(seed.ok).toBeTruthy();
  await page.addInitScript(() => localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true"));
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const wav = Buffer.alloc(44 + 16000);
  wav.write("RIFF"); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
  wav.write("data", 36); wav.writeUInt32LE(16000, 40);
  let scoreId = "A";
  let uploads = 0;
  let rejectUpload = false;
  let renders = 0;
  let renewals = 0;
  let expired = false;
  let expiredRequests = 0;
  let expiredMediaRequests = 0;
  let downloadRefresh = false;
  let downloadOnlyPlaybackRequests = 0;
  let currentScoreRecovery = false;
  let currentScoreRecoveryWaveformRequests = 0;
  let exportMixRequests = 0;
  let progressRequests = 0;
  let initiallyExpired = true;
  let originalExpiresAt = Math.floor(Date.now() / 1000) - 10;
  const playbackToken = (expiresAt: number) =>
    `${Buffer.from(JSON.stringify({ exp: expiresAt })).toString("base64url")}.test-signature`;
  await page.route("http://127.0.0.1:8000/**", async route => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (url.pathname === "/credits") return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    if (url.pathname === "/readyz") return json({ ready: true, status: "ready" });
    if (url.pathname === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (url.pathname === "/api/voicebanks") return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    if (url.pathname === "/sessions") return json({ session_id: "history" });
    if (url.pathname.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (url.pathname.endsWith("/upload")) {
      if (rejectUpload) return route.fulfill({ status: 400, json: { detail: "Invalid score" } });
      scoreId = ++uploads === 1 ? "A" : "B";
      return json({ session_id: "history", score_id: scoreId, parsed: true });
    }
    if (url.pathname.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (url.pathname.endsWith("/chat")) {
      renders++;
      return json({ type: "chat_progress", message: "Preparing take", job_id: "J1", progress_url: "/sessions/history/progress?job_id=J1" });
    }
    if (url.pathname.endsWith("/progress")) {
      progressRequests++;
      if (expired) renewals++;
      const generation = expired
        ? "renewed"
        : currentScoreRecovery
          ? "current-score-recovery"
          : downloadRefresh
            ? "download-only"
            : "original";
      return json({ status: "done", job_id: "J1", score_id: "A", score_version_no: 1,
        audio_url: `/sessions/history/audio?file=J1.wav&generation=${generation}&playback_token=${playbackToken(originalExpiresAt + (expired ? 3600 : 0))}`,
        audio_track: { key: "id:solo", label: "Solo", part_id: "solo", part_index: 0 }, actual_duration_seconds: 1 });
    }
    if (url.pathname.endsWith("/export-mix")) {
      exportMixRequests++;
      return json({ status: "queued", progress_url: "/unexpected-export" });
    }
    if (url.pathname.endsWith("/audio")) {
      if (
        url.searchParams.get("generation") === "current-score-recovery" &&
        route.request().resourceType() === "fetch"
      ) {
        currentScoreRecoveryWaveformRequests++;
      }
      if (url.searchParams.get("generation") === "download-only" && !url.searchParams.has("download")) {
        downloadOnlyPlaybackRequests++;
      }
      if ((initiallyExpired || expired) && url.searchParams.get("generation") === "original") {
        expiredRequests++;
        if (route.request().resourceType() === "media") {
          expiredMediaRequests++;
        }
        return route.fulfill({ status: 401 });
      }
      const downloadHeaders = route.request().resourceType() === "document"
        ? { "Content-Disposition": "attachment; filename=J1.wav" }
        : {};
      return route.fulfill({ contentType: "audio/wav", body: wav, headers: {
        "Cache-Control": "no-store",
        ...downloadHeaders,
      } });
    }
    return json({});
  });
  await signInAsE2EUser(page, "score-history");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const upload = () => page.getByTestId("score-upload-input").setInputFiles({ name: "score.xml", mimeType: "application/xml", buffer: xml });
  await upload();
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();
  const audio = page.getByTestId("synthesis-audio");
  await expect(audio).toHaveCount(1);
  await expect(page.getByTestId("synthesis-audio-recover")).toBeVisible();
  await expect(audio).not.toHaveAttribute("src");
  expect(expiredMediaRequests).toBe(0);
  initiallyExpired = false;
  originalExpiresAt = Math.floor(Date.now() / 1000) + 60;
  await page.getByTestId("synthesis-audio-recover").click();
  await expect(audio).toBeVisible();
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.paused)).toBe(false);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0);
  await audio.evaluate((element: HTMLAudioElement) => {
    element.pause();
    element.currentTime = 0;
  });
  expiredRequests = 0;
  await expect(page.locator(".multitrack-lanes")).toContainText("Solo");
  await page.getByRole("slider", { name: "Solo volume" }).evaluate((element: HTMLInputElement) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(element, "0");
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.getByRole("button", { name: "Mute Solo", exact: true }).evaluate((element: HTMLButtonElement) => element.click());
  const playerUrlBeforeDownload = await audio.getAttribute("src");
  const progressRequestsBeforeMessageDownload = progressRequests;
  downloadRefresh = true;
  const messageDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download audio" }).click();
  await messageDownload;
  expect(progressRequests).toBe(progressRequestsBeforeMessageDownload + 1);
  await expect(audio).toHaveAttribute("src", playerUrlBeforeDownload as string);
  await page.waitForTimeout(250);
  expect(downloadOnlyPlaybackRequests).toBe(0);
  downloadRefresh = false;
  const progressRequestsBeforeDownload = progressRequests;
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download track" }).evaluate((element: HTMLButtonElement) => element.click());
  await download;
  expect(progressRequests).toBe(progressRequestsBeforeDownload + 1);
  expect(exportMixRequests).toBe(0);
  currentScoreRecovery = true;
  await audio.evaluate((element) => {
    element.dataset.recoveryElement = "preserved";
  });
  await audio.dispatchEvent("error");
  await expect(audio).toHaveAttribute("src", /generation=current-score-recovery/);
  await expect(audio).toHaveAttribute("data-recovery-element", "preserved");
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThanOrEqual(2);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.paused)).toBe(false);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0);
  const currentScorePlaybackTime = await audio.evaluate((element: HTMLAudioElement) => element.currentTime);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(currentScorePlaybackTime);
  await page.waitForTimeout(250);
  expect(currentScoreRecoveryWaveformRequests).toBe(0);
  expect(autoplayErrors).toEqual([]);
  await audio.evaluate((element: HTMLAudioElement) => {
    element.pause();
    element.currentTime = 0;
  });
  currentScoreRecovery = false;
  rejectUpload = true;
  await upload();
  await expect(page.getByText("Invalid score", { exact: true })).toBeVisible();
  await expect(page.locator(".multitrack-lanes")).toContainText("Solo");
  rejectUpload = false;
  await upload();
  await expect(page.locator(".multitrack-lanes")).toContainText("No tracks yet");
  await expect(audio).toHaveCount(1);
  expired = true;
  await page.evaluate((expiresAt) => {
    Date.now = () => (expiresAt + 1) * 1000;
    window.dispatchEvent(new Event("focus"));
  }, originalExpiresAt);
  const recover = page.getByTestId("synthesis-audio-recover");
  await expect(recover).toBeVisible();
  expect(expiredRequests).toBe(0);
  await expect(audio).toHaveAttribute("data-recovery-element", "preserved");
  await recover.click();
  await expect(audio).toHaveAttribute("src", /generation=renewed/);
  await expect(audio).toHaveAttribute("data-recovery-element", "preserved");
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThanOrEqual(2);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.paused)).toBe(false);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0);
  const historicalPlaybackTime = await audio.evaluate((element: HTMLAudioElement) => element.currentTime);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(historicalPlaybackTime);
  await expect(page.locator(".multitrack-lanes")).toContainText("No tracks yet");
  expect(renders).toBe(1);
  expect(renewals).toBe(1);
  expect(expiredRequests).toBe(0);
  expect(autoplayErrors).toEqual([]);
  expect(scoreId).toBe("B");
  await page.screenshot({ path: testInfo.outputPath("retained-chat-audio.png"), fullPage: true });
});
