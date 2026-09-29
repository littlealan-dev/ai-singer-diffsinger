import { expect, test, type Page, type Request } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// The score player gets each take's audio when that take's job completes: it
// is downloaded and decoded once, and replaces the previous take of its part.
// Takes are silent WAVs of distinct lengths, so the player's timeline shows
// which take it actually plays. Playback tokens expire quickly here, so any
// re-download of an earlier take fails with 401 and is caught.

const SESSION_ID = "takes-session";
const TOKEN_TTL_SECONDS = 20;

type Take = { jobId: string; part: string; seconds: number };

function silentWav(seconds: number): Buffer {
  const sampleRate = 8_000;
  const dataBytes = Math.round(seconds * sampleRate) * 2;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + dataBytes, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(dataBytes, 40);
  return Buffer.concat([header, Buffer.alloc(dataBytes)]);
}

const token = () =>
  `${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_SECONDS })).toString("base64url")}.sig`;
const tokenExpired = (url: URL) => {
  const value = url.searchParams.get("playback_token");
  if (!value) return false;
  return JSON.parse(Buffer.from(value.split(".")[0], "base64url").toString()).exp * 1000 < Date.now();
};

type Backend = {
  nextTake: (take: Take) => void;
  // Downloads made by the player's decoder (the chat's <audio> element uses Range requests).
  decoderDownloads: () => string[];
  expiredDownloads: () => number;
};

async function mockBackend(page: Page): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let take: Take | null = null;
  const decoderDownloads: string[] = [];
  let expiredDownloads = 0;
  const completed = (current: Take) => ({
    status: "done",
    job_id: current.jobId,
    job_kind: "synthesis",
    step: "done",
    message: "Here is the rendered audio.",
    progress: 1,
    audio_url: `/sessions/${SESSION_ID}/audio?file=${current.jobId}.wav&playback_token=${token()}`,
    audio_track: { key: `id:${current.part}`, label: current.part, part_id: current.part, verse_number: "1" },
    actual_duration_seconds: current.seconds,
    performance_midi: null,
    performance_midi_published: false,
  });
  const handler = async (route: Parameters<Parameters<Page["route"]>[1]>[0]) => {
    const request: Request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    const route_ = url.pathname;
    if (route_ === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (route_ === "/readyz") return json({ ready: true, status: "ready" });
    if (route_ === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (route_ === "/api/voicebanks") return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    if (route_ === "/sessions") return json({ session_id: SESSION_ID });
    if (route_.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (route_.endsWith("/upload")) {
      return json({
        session_id: SESSION_ID,
        score_id: `score-${Date.now()}`,
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: 8,
          parts: [
            { part_id: "Voice", part_index: 0, part_name: "Voice", has_lyrics: true },
            { part_id: "Voice 2", part_index: 1, part_name: "Voice 2", has_lyrics: true },
          ],
        },
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/audio")) {
      const file = url.searchParams.get("file") ?? "";
      if (tokenExpired(url)) {
        if (!request.headers()["range"]) expiredDownloads += 1;
        return route.fulfill({ status: 401, json: { detail: "Unauthorized" } });
      }
      if (!request.headers()["range"]) decoderDownloads.push(file);
      const seconds = Number(/-(\d+)s\.wav$/.exec(file)?.[1] ?? 1);
      return route.fulfill({ contentType: "audio/wav", body: silentWav(seconds) });
    }
    if (route_.endsWith("/chat")) {
      if (!take) throw new Error("No take queued for this chat turn.");
      const accepted = {
        type: "chat_progress",
        message: "Starting the take.",
        job_id: take.jobId,
        progress_url: `/sessions/${SESSION_ID}/progress?job_id=${take.jobId}`,
      };
      const body = [
        `event: accepted\ndata: ${JSON.stringify(accepted)}\n`,
        `event: completed\ndata: ${JSON.stringify(completed(take))}\n`,
      ].join("\n");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `${body}\n` });
    }
    if (route_.endsWith("/progress")) return take ? json(completed(take)) : json({});
    return json({});
  };
  await page.route("http://127.0.0.1:8000/**", handler);
  return {
    nextTake: (next) => {
      take = next;
    },
    decoderDownloads: () => [...decoderDownloads],
    expiredDownloads: () => expiredDownloads,
  };
}

async function sing(page: Page, backend: Backend, take: Take) {
  backend.nextTake(take);
  await page.getByTestId("chat-input").fill(`sing ${take.part}`);
  await page.getByTestId("send-message").click();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

async function uploadScore(page: Page, name: string) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({ name, mimeType: "application/xml", buffer: xml });
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
}

const vocalRows = (page: Page) => page.locator(".score-track-row.vocal-track");
const timeline = (page: Page) => page.locator(".score-player-seek-time");

test("takes replace, append and clear in the score player without mixing up their audio", async ({ page }) => {
  test.setTimeout(180_000);
  await page.addInitScript(() => localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true"));
  const backend = await mockBackend(page);
  await signInAsE2EUser(page, "score-player-takes");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  await uploadScore(page, "first-score.xml");

  // First take of "Voice": 3 s.
  await sing(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:03");

  // A new take of the same part (5 s) replaces it: one row, and the player
  // plays the new audio, not the previous take's.
  await sing(page, backend, { jobId: "job-b-5s", part: "Voice", seconds: 5 });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:05");

  // Let the earlier takes' tokens expire, then add another part (2 s). The
  // player must keep "Voice" from its decoded audio rather than download it again.
  await page.waitForTimeout((TOKEN_TTL_SECONDS + 2) * 1000);
  await sing(page, backend, { jobId: "job-c-2s", part: "Voice 2", seconds: 2 });
  await expect(vocalRows(page)).toHaveCount(2);
  await expect(timeline(page)).toHaveText("0:00 / 0:05");
  await expect(page.getByRole("button", { name: "Play score player" })).toBeEnabled();
  expect(backend.expiredDownloads()).toBe(0);
  expect(backend.decoderDownloads()).toEqual(["job-a-3s.wav", "job-b-5s.wav", "job-c-2s.wav"]);

  // A new score clears every take from the player.
  await uploadScore(page, "second-score.xml");
  await expect(vocalRows(page)).toHaveCount(0);

  // A take on the new score is the only track, with its own audio.
  await sing(page, backend, { jobId: "job-d-4s", part: "Voice", seconds: 4 });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:04");
  expect(backend.decoderDownloads()).toEqual([
    "job-a-3s.wav",
    "job-b-5s.wav",
    "job-c-2s.wav",
    "job-d-4s.wav",
  ]);
});
