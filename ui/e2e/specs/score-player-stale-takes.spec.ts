import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// Each take records its part's take_signature: the music it was sung from.
// When a score edit changes a part's music, that part's earlier take no longer
// matches the score. It stays in the player until the next take lands, which
// then replaces it; its audio stays in the chat.

const SESSION_ID = "stale-takes-session";

type Take = { jobId: string; part: string; signature: string };
type ChatBody = {
  score_player_takes: Array<{ part_id: string | null; label: string; take_signature?: string | null }>;
};

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

const summary = (voiceSignature: string) => ({
  duration_seconds: 4,
  parts: [
    { part_id: "Voice", part_index: 0, part_name: "Voice", has_lyrics: true, take_signature: voiceSignature },
    { part_id: "Voice 2", part_index: 1, part_name: "Voice 2", has_lyrics: true, take_signature: "voice2-1" },
  ],
});

type Backend = {
  nextTake: (take: Take) => void;
  nextEdit: () => void;
  chatBodies: () => ChatBody[];
};

async function mockBackend(page: Page): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let take: Take | null = null;
  let edit = false;
  const chatBodies: ChatBody[] = [];
  const completed = (current: Take) => ({
    status: "done",
    job_id: current.jobId,
    job_kind: "synthesis",
    step: "done",
    message: "Here is the rendered audio.",
    progress: 1,
    audio_url: `/sessions/${SESSION_ID}/audio?file=${current.jobId}.wav`,
    audio_track: {
      key: `id:${current.part}`,
      label: current.part,
      part_id: current.part,
      verse_number: "1",
      take_signature: current.signature,
    },
    expand_repeats: false,
    actual_duration_seconds: 2,
    performance_midi: null,
  });
  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
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
        score_id: "score-stale",
        parsed: true,
        current_score: { version: 1 },
        score_summary: summary("voice-1"),
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/audio")) return route.fulfill({ contentType: "audio/wav", body: silentWav(2) });
    if (route_.endsWith("/chat")) {
      chatBodies.push(request.postDataJSON() as ChatBody);
      if (edit) {
        edit = false;
        // A tool edited Voice's music: the reply carries the new score and summary.
        return json({
          type: "chat_text",
          message: "I edited the Voice part. Please check the score.",
          current_score: { version: 2 },
          score_summary: summary("voice-2"),
        });
      }
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
  });
  return {
    nextTake: (next) => {
      take = next;
      edit = false;
    },
    nextEdit: () => {
      edit = true;
    },
    chatBodies: () => [...chatBodies],
  };
}

async function send(page: Page, message: string) {
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("send-message").click();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

const vocalRows = (page: Page) => page.locator(".score-track-row.vocal-track");

test("a take sung before its part was edited is replaced when the next take lands", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await mockBackend(page);
  await signInAsE2EUser(page, "score-player-stale-takes");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({ name: "score.xml", mimeType: "application/xml", buffer: xml });
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();

  backend.nextTake({ jobId: "job-voice", part: "Voice", signature: "voice-1" });
  await send(page, "sing Voice");
  backend.nextTake({ jobId: "job-voice2", part: "Voice 2", signature: "voice2-1" });
  await send(page, "sing Voice 2");
  await expect(vocalRows(page)).toHaveCount(2);

  // An edit to Voice leaves both takes in the player until the next take.
  backend.nextEdit();
  await send(page, "edit the Voice part");
  await page.waitForTimeout(500);
  await expect(vocalRows(page)).toHaveCount(2);

  // The next take reports each take's signature, then replaces the stale one.
  backend.nextTake({ jobId: "job-voice2-again", part: "Voice 2", signature: "voice2-1" });
  await send(page, "sing Voice 2 again");
  expect(backend.chatBodies().at(-1)?.score_player_takes).toEqual([
    { part_id: "Voice", label: "Voice", expand_repeats: false, take_signature: "voice-1" },
    { part_id: "Voice 2", label: "Voice 2", expand_repeats: false, take_signature: "voice2-1" },
  ]);
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(page.getByTestId("score-vocal-track-id:Voice 2")).toBeVisible();
});
