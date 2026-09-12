import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { signInAsE2EUser } from "../auth";
import { LATEST_ANNOUNCEMENT_ID } from "../../src/announcements";

// Auth/Firestore are local emulators; render responses and media are deterministic.
test("re-upload preserves chat playback without restoring old mixer tracks", async ({ page }, testInfo) => {
  const seed = await fetch("http://127.0.0.1:8080/v1/projects/demo-sightsinger-e2e/databases/(default)/documents/users/e2e-score-history", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
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
      if (expired) renewals++;
      return json({ status: "done", job_id: "J1", score_id: "A", score_version_no: 1,
        audio_url: `/sessions/history/audio?file=J1.wav&token=${expired ? "renewed" : "original"}`,
        audio_track: { key: "id:solo", label: "Solo", part_id: "solo", part_index: 0 }, actual_duration_seconds: 1 });
    }
    if (url.pathname.endsWith("/audio")) {
      if (expired && url.searchParams.get("token") === "original") {
        expiredRequests++;
        return route.fulfill({ status: 401 });
      }
      return route.fulfill({ contentType: "audio/wav", body: wav, headers: { "Cache-Control": "no-store" } });
    }
    return json({});
  });
  await signInAsE2EUser(page, "score-history");
  const upload = () => page.getByTestId("score-upload-input").setInputFiles({ name: "score.xml", mimeType: "application/xml", buffer: xml });
  await upload();
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();
  const audio = page.getByTestId("synthesis-audio");
  await expect(audio).toHaveCount(1);
  await expect(page.locator(".multitrack-lanes")).toContainText("Solo");
  rejectUpload = true;
  await upload();
  await expect(page.getByText("Invalid score", { exact: true })).toBeVisible();
  await expect(page.locator(".multitrack-lanes")).toContainText("Solo");
  rejectUpload = false;
  await upload();
  await expect(page.locator(".multitrack-lanes")).toContainText("No tracks yet");
  await expect(audio).toHaveCount(1);
  expired = true;
  // Discard buffered media and change the request URL without changing its expired token.
  await audio.evaluate((element: HTMLAudioElement) => {
    const expiredUrl = `${element.src}&fresh=1`;
    element.removeAttribute("src"); element.load();
    element.src = expiredUrl; element.load();
    void element.play().catch(() => undefined);
  });
  await expect(audio).toHaveAttribute("src", /token=renewed/);
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThanOrEqual(2);
  await expect(page.locator(".multitrack-lanes")).toContainText("No tracks yet");
  expect(renders).toBe(1);
  expect(renewals).toBe(1);
  expect(expiredRequests).toBeGreaterThan(0);
  expect(scoreId).toBe("B");
  await page.screenshot({ path: testInfo.outputPath("retained-chat-audio.png"), fullPage: true });
});
