import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// A part written on two staves, such as a piano, arrives from the backend as
// one entry and one MIDI track per staff, all with the same raw part ID. The
// player shows one row for the part, and that row controls every one of its
// MIDI tracks. Here the MIDI has three tracks: piano right hand, piano left
// hand, and guitar.

const SESSION_ID = "instrument-rows-session";
const part = (partIndex: number, partId: string, rawPartId: string, label: string, program: number) => ({
  part_index: partIndex,
  part_id: partId,
  raw_part_id: rawPartId,
  label,
  eligible: true,
  has_lyrics: false,
  midi_program: program,
  percussion: false,
});
const PERFORMANCE_MIDI = {
  version: 1,
  has_instrumental_parts: true,
  original_midi_available: true,
  expanded_midi_available: true,
  instrumental_parts: [
    part(1, "P2-Staff1", "P2", "Piano", 0),
    part(2, "P2-Staff2", "P2", "Piano", 0),
    part(3, "P3", "P3", "Guitar", 24),
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

/** A type-1 MIDI file with one one-second note per track, in the given programs. */
function multiTrackMidi(programs: number[]): Buffer {
  const division = 480;
  const tracks = programs.map((program, index) => {
    const pitch = 60 + index * 4;
    const events = Buffer.from([
      0x00, 0xc0 | index, program,
      0x00, 0x90 | index, pitch, 0x64,
      0x87, 0x40, 0x80 | index, pitch, 0x40, // note off after 960 ticks (1 s at 120 bpm)
      0x00, 0xff, 0x2f, 0x00,
    ]);
    const trackHeader = Buffer.from([0x4d, 0x54, 0x72, 0x6b, 0, 0, 0, 0]);
    trackHeader.writeUInt32BE(events.length, 4);
    return Buffer.concat([trackHeader, events]);
  });
  const header = Buffer.from([
    0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 1, 0, programs.length, division >> 8, division & 0xff,
  ]);
  return Buffer.concat([header, ...tracks]);
}

async function mockBackend(page: Page) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const completed = {
    status: "done",
    job_id: "job-voice",
    job_kind: "synthesis",
    step: "done",
    message: "Here is the rendered audio.",
    progress: 1,
    audio_url: `/sessions/${SESSION_ID}/audio?file=job-voice.wav`,
    audio_track: { key: "id:Voice", label: "Voice", part_id: "Voice", verse_number: "1" },
    expand_repeats: true,
    actual_duration_seconds: 1,
    performance_midi: PERFORMANCE_MIDI,
    performance_midi_published: true,
  };
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
        score_id: "score-instrument-rows",
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: 1,
          parts: [{ part_id: "Voice", part_index: 0, part_name: "Voice", has_lyrics: true }],
        },
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/instrumental-midi")) {
      return route.fulfill({ contentType: "audio/midi", body: multiTrackMidi([0, 0, 24]) });
    }
    if (route_.endsWith("/audio")) return route.fulfill({ contentType: "audio/wav", body: silentWav(1) });
    if (route_.endsWith("/chat")) {
      const accepted = {
        type: "chat_progress",
        message: "Starting the take.",
        job_id: "job-voice",
        progress_url: `/sessions/${SESSION_ID}/progress?job_id=job-voice`,
      };
      const body = [
        `event: accepted\ndata: ${JSON.stringify(accepted)}\n`,
        `event: completed\ndata: ${JSON.stringify(completed)}\n`,
      ].join("\n");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `${body}\n` });
    }
    if (route_.endsWith("/progress")) return json(completed);
    return json({});
  });
}

/**
 * The audio engine's per-track mixer state, in the provider's track order
 * (MIDI tracks first), which is the order the app's mixer addresses. The app
 * renders no DOM for it, so reach the provider's context value by walking up
 * the React fiber tree from the seek bar it renders. The engine keeps its own
 * track order, so its tracks are matched to the provider's by ID. (The
 * provider's own `trackStates` is not used: it keeps only the last of several
 * mixer edits made in one render, while the engine receives them all.)
 */
async function playerTrackStates(page: Page) {
  return page.evaluate(() => {
    const node = document.querySelector(".score-player-seek-time");
    if (!node) return null;
    const fiberKey = Object.keys(node).find((key) => key.startsWith("__reactFiber$"));
    type EngineTrack = { id: string; muted: boolean; soloed: boolean; volume: number };
    type Engine = { getState: () => { tracks: EngineTrack[] } };
    type ContextValue = { tracks?: Array<{ id: string }>; playoutRef?: { current: Engine | null } };
    type Fiber = { return: Fiber | null; memoizedProps?: { value?: ContextValue } };
    let fiber = fiberKey ? ((node as unknown as Record<string, Fiber>)[fiberKey] ?? null) : null;
    while (fiber) {
      const value = fiber.memoizedProps?.value;
      if (value?.playoutRef && value.tracks) {
        const engineTracks = value.playoutRef.current?.getState().tracks ?? [];
        return value.tracks.map(({ id }) => {
          const track = engineTracks.find((engineTrack) => engineTrack.id === id);
          return track ? { muted: track.muted, soloed: track.soloed, volume: track.volume } : null;
        });
      }
      fiber = fiber.return;
    }
    return null;
  });
}

const midiTrackStates = async (page: Page) => (await playerTrackStates(page))?.slice(0, 3);
const mutedStates = async (page: Page) => (await midiTrackStates(page))?.map((state) => state?.muted);
const instrumentRows = (page: Page) => page.locator(".score-track-row.instrument-track");

test("a part on two staves has one row that controls both of its MIDI tracks", async ({ page }) => {
  test.setTimeout(120_000);
  const consoleMessages: string[] = [];
  page.on("console", (message) => consoleMessages.push(message.text()));
  await mockBackend(page);
  await signInAsE2EUser(page, "score-player-instrument-rows");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({ name: "score.xml", mimeType: "application/xml", buffer: xml });
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
  await page.getByTestId("chat-input").fill("sing Voice");
  await page.getByTestId("send-message").click();

  // One row per part: the piano's two staves share a row.
  await expect(instrumentRows(page)).toHaveCount(2);
  await expect(page.getByTestId("score-inst-track-P2")).toHaveCount(1);
  await expect(page.getByTestId("score-inst-track-P3")).toHaveCount(1);
  await expect(page.getByTestId("score-inst-track-P2").locator(".score-track-title")).toHaveText("Piano");
  await expect(page.getByTestId("score-inst-track-P3").locator(".score-track-title")).toHaveText("Guitar");
  await expect.poll(() => playerTrackStates(page).then((states) => states?.length)).toBe(4);

  // The sound picker shows the sound without its category, and a name too long
  // for the button ends in "…" inside it.
  const pianoSound = page.getByTestId("score-inst-track-P2").locator(".score-track-instrument-trigger");
  const guitarSound = page.getByTestId("score-inst-track-P3").locator(".score-track-instrument-trigger");
  await expect(pianoSound.locator(".score-track-instrument-label")).toHaveText("Acoustic Grand Piano");
  await expect(guitarSound.locator(".score-track-instrument-label")).toHaveText("Acoustic Guitar (Nylon)");
  for (const trigger of [pianoSound, guitarSound]) {
    const fits = await trigger.evaluate((button) => {
      const label = button.querySelector(".score-track-instrument-label") as HTMLElement;
      const icon = button.querySelector("svg") as SVGElement;
      const box = button.getBoundingClientRect();
      return (
        label.getBoundingClientRect().right <= box.right &&
        icon.getBoundingClientRect().right <= box.right &&
        getComputedStyle(label).textOverflow === "ellipsis"
      );
    });
    expect(fits).toBe(true);
  }

  // Muting the piano mutes both of its staves and leaves the guitar playing.
  const pianoMute = page.getByTestId("score-inst-track-P2").locator(".score-track-mute-btn");
  await pianoMute.click();
  await expect.poll(() => mutedStates(page)).toEqual([true, true, false]);
  await pianoMute.click();

  // The guitar row controls the third MIDI track, not the piano's second staff.
  await page.getByTestId("score-inst-track-P3").locator(".score-track-mute-btn").click();
  await expect.poll(() => mutedStates(page)).toEqual([false, false, true]);

  // The piano's volume reaches both staves.
  await page.getByTestId("score-inst-track-P2").getByRole("slider", { name: "Piano volume" }).fill("0.5");
  await expect.poll(() => midiTrackStates(page).then((states) => states?.map((state) => state?.volume)))
    .toEqual([0.5, 0.5, expect.any(Number)]);

  expect(consoleMessages.filter((text) => text.includes("same key"))).toEqual([]);
});
