import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// "With Repeats" chooses how the next take is rendered; it does not change
// what the score player plays. The player plays the instrumental MIDI in the
// order its takes were rendered, and keeps takes of one order only. As in the
// real backend, an upload carries no MIDI: the first take publishes it. The
// instrumental MIDI lasts 9 s with repeats and 6 s in written order, longer
// than any take, so the player's timeline shows which MIDI it plays.

const SESSION_ID = "repeats-session";
const MIDI_SECONDS = { true: 9, false: 6 } as const;
const PERFORMANCE_MIDI = {
  version: 1,
  has_instrumental_parts: true,
  original_midi_available: true,
  expanded_midi_available: true,
  instrumental_parts: [
    {
      part_index: 2,
      part_id: "Piano",
      raw_part_id: "P3",
      label: "Piano",
      eligible: true,
      has_lyrics: false,
      midi_program: 0,
      percussion: false,
    },
  ],
};

type Take = {
  jobId: string;
  part: string;
  seconds: number;
  expandRepeats: boolean;
  publishesMidi?: boolean;
};
type ChatBody = {
  expand_repeats: boolean;
  score_player_takes: Array<{ part_id: string | null; label: string; expand_repeats: boolean }>;
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

/** One piano note lasting `seconds`, at the default 120 bpm. */
function singleNoteMidi(seconds: number): Buffer {
  const division = 480;
  let ticks = seconds * 2 * division;
  const length = [ticks & 0x7f];
  while ((ticks >>= 7)) length.unshift((ticks & 0x7f) | 0x80);
  const events = Buffer.from([
    0x00, 0xc0, 0x00, // program: acoustic grand piano
    0x00, 0x90, 0x3c, 0x64, // note on, middle C
    ...length, 0x80, 0x3c, 0x40, // note off
    0x00, 0xff, 0x2f, 0x00, // end of track
  ]);
  const header = Buffer.from([0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, division >> 8, division & 0xff]);
  const trackHeader = Buffer.from([0x4d, 0x54, 0x72, 0x6b, 0, 0, 0, 0]);
  trackHeader.writeUInt32BE(events.length, 4);
  return Buffer.concat([header, trackHeader, events]);
}

type Backend = {
  nextTake: (take: Take) => void;
  midiRequests: () => boolean[];
  chatBodies: () => ChatBody[];
};

async function mockBackend(page: Page): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let take: Take | null = null;
  const midiRequests: boolean[] = [];
  const chatBodies: ChatBody[] = [];
  const completed = (current: Take) => ({
    status: "done",
    job_id: current.jobId,
    job_kind: "synthesis",
    step: "done",
    message: "Here is the rendered audio.",
    progress: 1,
    audio_url: `/sessions/${SESSION_ID}/audio?file=${current.jobId}.wav`,
    audio_track: { key: `id:${current.part}`, label: current.part, part_id: current.part, verse_number: "1" },
    expand_repeats: current.expandRepeats,
    actual_duration_seconds: current.seconds,
    performance_midi: PERFORMANCE_MIDI,
    performance_midi_published: Boolean(current.publishesMidi),
  });
  // The fallback synth is enough here; the 148 MB SoundFont would only slow the test.
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
        score_id: `score-${Date.now()}`,
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: 6,
          expanded_duration_seconds: 9,
          parts: [
            { part_id: "Voice", part_index: 0, part_name: "Voice", has_lyrics: true },
            { part_id: "Voice 2", part_index: 1, part_name: "Voice 2", has_lyrics: true },
          ],
        },
        // Instrumental MIDI is generated by the first take, not on upload.
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/instrumental-midi")) {
      const expandRepeats = url.searchParams.get("expand_repeats") === "true";
      midiRequests.push(expandRepeats);
      return route.fulfill({ contentType: "audio/midi", body: singleNoteMidi(MIDI_SECONDS[`${expandRepeats}`]) });
    }
    if (route_.endsWith("/audio")) {
      const seconds = Number(/-(\d+)s\.wav$/.exec(url.searchParams.get("file") ?? "")?.[1] ?? 1);
      return route.fulfill({ contentType: "audio/wav", body: silentWav(seconds) });
    }
    if (route_.endsWith("/chat")) {
      if (!take) throw new Error("No take queued for this chat turn.");
      chatBodies.push(request.postDataJSON() as ChatBody);
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
    },
    midiRequests: () => [...midiRequests],
    chatBodies: () => [...chatBodies],
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
// A render setting, so it sits with the other render settings under the chat input.
const repeatsToggle = (page: Page) =>
  page.locator(".composer-menu-bar").getByRole("switch", { name: "With Repeats" });

test("the instrumental MIDI follows the takes' repeat order, not the toggle", async ({ page }) => {
  test.setTimeout(120_000);
  await page.addInitScript(() => localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true"));
  const backend = await mockBackend(page);
  await signInAsE2EUser(page, "score-player-repeats");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer bar, where the toggle lives.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  await uploadScore(page, "first-score.xml");

  // The toggle starts in written order. No MIDI until a take publishes it;
  // the toggle alone fetches nothing.
  await expect(repeatsToggle(page)).not.toBeChecked();
  await repeatsToggle(page).click();
  await repeatsToggle(page).click();
  await repeatsToggle(page).click();
  await expect(repeatsToggle(page)).toBeChecked();
  await page.waitForTimeout(1_000);
  expect(backend.midiRequests()).toEqual([]);

  // The first take, with repeats, publishes the MIDI: the player loads it with repeats.
  await sing(page, backend, {
    jobId: "job-a-3s", part: "Voice", seconds: 3, expandRepeats: true, publishesMidi: true,
  });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:09");
  expect(backend.chatBodies()[0]).toMatchObject({ expand_repeats: true, score_player_takes: [] });
  expect(backend.midiRequests()).toEqual([true]);

  // Switching to written order sets up the next take only: playback is unchanged.
  await repeatsToggle(page).click();
  await expect(repeatsToggle(page)).not.toBeChecked();
  await page.waitForTimeout(1_000);
  await expect(timeline(page)).toHaveText("0:00 / 0:09");
  expect(backend.midiRequests()).toEqual([true]);

  // A written-order take of another part: the player drops the take with
  // repeats, and the MIDI follows the new take.
  await sing(page, backend, { jobId: "job-b-2s", part: "Voice 2", seconds: 2, expandRepeats: false });
  expect(backend.chatBodies()[1]).toMatchObject({
    expand_repeats: false,
    score_player_takes: [{ part_id: "Voice", label: "Voice", expand_repeats: true }],
  });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(page.getByTestId("score-vocal-track-id:Voice 2")).toBeVisible();
  await expect(timeline(page)).toHaveText("0:00 / 0:06");
  expect(backend.midiRequests()).toEqual([true, false]);

  // A new score clears every track, MIDI included, and resets the toggle to
  // written order.
  await uploadScore(page, "second-score.xml");
  await expect(vocalRows(page)).toHaveCount(0);
  await expect(page.locator(".score-player-seek-time")).toHaveCount(0);
  await expect(repeatsToggle(page)).not.toBeChecked();
  await page.waitForTimeout(1_000);
  expect(backend.midiRequests()).toEqual([true, false]);
});
