import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// The first take publishes instrumental MIDI and is playing when a second,
// vocal-only take for another part lands (the instruments were already paid
// for). Adding that take must leave the score player ready again: seek bar and
// real-time mix enabled, and Play starts playback. The player used to be marked
// loading after it had reported ready for the same render, and stayed disabled.

const SESSION_ID = "second-take-ready";
const TAKE_SECONDS = 30;
const PERFORMANCE_MIDI = {
  version: 1,
  has_instrumental_parts: true,
  original_midi_available: true,
  expanded_midi_available: true,
  instrumental_parts: [
    {
      part_index: 2,
      part_id: "P3",
      raw_part_id: "P3",
      label: "Piano",
      eligible: true,
      has_lyrics: false,
      midi_program: 0,
      percussion: false,
    },
  ],
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

/** A type-1 MIDI file with one one-second piano note. */
function oneTrackMidi(): Buffer {
  const events = Buffer.from([
    0x00, 0xc0, 0x00,
    0x00, 0x90, 60, 0x64,
    0x87, 0x40, 0x80, 60, 0x40,
    0x00, 0xff, 0x2f, 0x00,
  ]);
  const trackHeader = Buffer.from([0x4d, 0x54, 0x72, 0x6b, 0, 0, 0, 0]);
  trackHeader.writeUInt32BE(events.length, 4);
  const header = Buffer.from([0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 1, 0, 1, 0x01, 0xe0]);
  return Buffer.concat([header, trackHeader, events]);
}

const take = (jobId: string, partId: string, label: string, midiPublished: boolean) => ({
  status: "done",
  job_id: jobId,
  job_kind: "synthesis",
  step: "done",
  message: `Here is the ${label} take.`,
  progress: 1,
  audio_url: `/sessions/${SESSION_ID}/audio?file=${jobId}.wav`,
  audio_track: { key: `id:${partId}`, label, part_id: partId, verse_number: "SSSolfege" },
  expand_repeats: false,
  actual_duration_seconds: TAKE_SECONDS,
  performance_midi: PERFORMANCE_MIDI,
  performance_midi_published: midiPublished,
});

async function mockBackend(page: Page) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const takes = [
    take("job-women-2", "Women2", "Women 2", true),
    take("job-men", "Men", "Men", false),
  ];
  let chats = 0;
  // The fallback synth is enough here; the 148 MB SoundFont would only slow the test.
  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    const p = url.pathname;
    if (p === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (p === "/readyz") return json({ ready: true, status: "ready" });
    if (p === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (p === "/api/voicebanks") return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    if (p === "/sessions") return json({ session_id: SESSION_ID });
    if (p.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (p.endsWith("/upload")) {
      return json({
        session_id: SESSION_ID,
        score_id: "score-second-take",
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: TAKE_SECONDS,
          parts: [
            { part_id: "Women2", part_index: 0, part_name: "Women 2", has_lyrics: true },
            { part_id: "Men", part_index: 1, part_name: "Men", has_lyrics: true },
          ],
        },
        performance_midi: null,
      });
    }
    if (p.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (p.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (p.endsWith("/instrumental-midi")) return route.fulfill({ contentType: "audio/midi", body: oneTrackMidi() });
    if (p.endsWith("/audio")) return route.fulfill({ contentType: "audio/wav", body: silentWav(TAKE_SECONDS) });
    if (p.endsWith("/chat")) {
      const completed = takes[Math.min(chats, takes.length - 1)];
      chats += 1;
      const accepted = {
        type: "chat_progress",
        message: "Starting the take.",
        job_id: completed.job_id,
        progress_url: `/sessions/${SESSION_ID}/progress?job_id=${completed.job_id}`,
      };
      const body = [
        `event: accepted\ndata: ${JSON.stringify(accepted)}\n`,
        `event: completed\ndata: ${JSON.stringify(completed)}\n`,
      ].join("\n");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `${body}\n` });
    }
    if (p.endsWith("/progress")) {
      const jobId = url.searchParams.get("job_id");
      return json(takes.find((t) => t.job_id === jobId) ?? takes[0]);
    }
    return json({});
  });
}

test("the score player is ready again after a vocal-only take lands during playback", async ({ page }) => {
  test.setTimeout(120_000);
  await mockBackend(page);
  await signInAsE2EUser(page, "score-player-second-take");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({ name: "score.xml", mimeType: "application/xml", buffer: xml });
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();

  const seek = page.locator(".score-player-seek-slider");
  const mix = page.getByRole("button", { name: "Download mix in real time" });
  const play = page.getByRole("button", { name: "Play score player" });
  const pause = page.getByRole("button", { name: "Pause score player" });
  const stop = page.getByRole("button", { name: "Stop score player" });

  await page.getByTestId("chat-input").fill("sing Women 2");
  await page.getByTestId("send-message").click();
  await expect(seek).toBeEnabled({ timeout: 30_000 });
  await expect(mix).toBeEnabled();

  // The first take is playing when the second one lands.
  await play.click();
  await expect(pause).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("chat-input").fill("sing Men");
  await page.getByTestId("send-message").click();
  await expect(page.getByText("Here is the Men take.")).toBeVisible();

  await expect(seek).toBeEnabled({ timeout: 30_000 });
  await expect(mix).toBeEnabled();

  // Back to the start, then Play starts playback again.
  await stop.click();
  await expect(play).toBeVisible();
  await play.click();
  await expect(pause).toBeVisible({ timeout: 15_000 });
});
