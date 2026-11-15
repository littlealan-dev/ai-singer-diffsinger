import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// A Song is a score with its chat, takes and mix, at its own address
// (/app/song/{id}). These tests cover reopening one, the "New song or
// replace?" choice, My songs, and leaving a Song while it works.
//
// The backend is mocked as a small Songs server: like the real one, it saves
// the chat, follows each job, and records a finished take in the Song's player
// by itself, whether or not a page is watching.

type Take = { jobId: string; part: string; seconds: number };
// What the next chat turn answers. A held text reply waits for `releaseReply`;
// a held take keeps running until `finishJob`.
type Reply = { text: string; hold?: boolean } | { take: Take; hold?: boolean };
type Job = Take & {
  sessionId: string;
  scoreId: string;
  turnId: string;
  createdAt: string;
  status: "running" | "done" | "stopped";
};
type Turn = { id: string; role: "user" | "assistant"; content: string; timestamp: string };
type Vocal = { key: string; part: string; jobId: string; seconds: number; muted: boolean; solo: boolean; volume: number };
type Song = {
  id: string;
  isSong: boolean;
  deleted: boolean;
  title: string;
  titleEdited: boolean;
  createdAt: string;
  lastActive: number;
  scoreId: string | null;
  currentJobId: string | null;
  history: Turn[];
  player: { scoreId: string | null; revision: number; vocals: Vocal[]; instruments: unknown[]; bus: unknown };
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

const instrumentalPart = (partIndex: number, partId: string, label: string, program: number) => ({
  part_index: partIndex,
  part_id: partId,
  raw_part_id: partId,
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
  instrumental_parts: [instrumentalPart(2, "P2", "Piano", 0), instrumentalPart(3, "P3", "Guitar", 24)],
};

type Backend = {
  reply: (next: Reply) => void;
  releaseReply: () => void;
  finishJob: (jobId: string) => void;
  setStopResult: (result: "stopped" | "too_late") => void;
  stopRequests: () => number;
  sessionsCreated: () => number;
  uploads: () => string[];
};

async function mockBackend(page: Page, { instruments = false } = {}): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const replies: Reply[] = [];
  const jobs = new Map<string, Job>();
  const songs = new Map<string, Song>();
  const uploads: string[] = [];
  let sessionsCreated = 0;
  let stopRequests = 0;
  let stopResult: "stopped" | "too_late" = "stopped";
  let releaseReply: () => void = () => {};
  let replyGate = new Promise<void>((resolve) => { releaseReply = resolve; });
  const performanceMidi = instruments ? PERFORMANCE_MIDI : null;
  let clock = Date.parse("2026-01-01T10:00:00Z");
  const now = () => new Date((clock += 1_000)).toISOString();
  let turns = 0;
  const emptyBus = { muted: false, solo: false, level_db: 0 };
  const scoreSummary = {
    duration_seconds: 8,
    parts: [
      { part_id: "Voice", part_index: 0, part_name: "Voice", has_lyrics: true },
      { part_id: "Voice 2", part_index: 1, part_name: "Voice 2", has_lyrics: true },
    ],
    performance_midi: performanceMidi,
  };

  const progressUrl = (job: Job) => `/sessions/${job.sessionId}/progress?job_id=${job.jobId}`;
  const audioUrl = (job: Job) => `/sessions/${job.sessionId}/audio?file=${job.jobId}.wav`;
  const progress = (job: Job) => {
    const base = { job_id: job.jobId, job_kind: "synthesis", score_id: job.scoreId };
    if (job.status === "running") return { ...base, status: "running", message: "Singing the take.", progress: 0.4 };
    if (job.status === "stopped") return { ...base, status: "stopped", message: "Stopped." };
    return {
      ...base,
      status: "done",
      step: "done",
      message: "Here is the rendered audio.",
      progress: 1,
      audio_url: audioUrl(job),
      audio_track: { key: `id:${job.part}`, label: job.part, part_id: job.part, verse_number: "1" },
      actual_duration_seconds: job.seconds,
      performance_midi: performanceMidi,
      performance_midi_published: instruments,
    };
  };
  const say = (song: Song, role: Turn["role"], content: string) => {
    const turn = { id: `turn-${(turns += 1)}`, role, content, timestamp: now() };
    song.history.push(turn);
    return turn;
  };
  const running = (sessionId: string) =>
    [...jobs.values()].filter((job) => job.sessionId === sessionId && job.status === "running");
  // The server puts a finished take in its Song's player: one track per part.
  const finish = (job: Job) => {
    job.status = "done";
    const player = songs.get(job.sessionId)?.player;
    if (!player || player.scoreId !== job.scoreId) return;
    const key = `id:${job.part}`;
    const old = player.vocals.find((vocal) => vocal.key === key);
    const vocal: Vocal = {
      key, part: job.part, jobId: job.jobId, seconds: job.seconds,
      muted: old?.muted ?? false, solo: old?.solo ?? false, volume: old?.volume ?? 1,
    };
    player.vocals = old ? player.vocals.map((entry) => (entry === old ? vocal : entry)) : [...player.vocals, vocal];
    player.revision += 1;
  };
  const stopWork = (song: Song) => {
    stopRequests += 1;
    const active = running(song.id);
    if (stopResult === "stopped") {
      active.forEach((job) => {
        job.status = "stopped";
        say(song, "assistant", "Stopped the take. Refunded 0 credits.");
      });
    }
    return active;
  };
  const songRow = (song: Song) => ({
    id: song.id,
    title: song.title,
    created_at: song.createdAt,
    last_active_at: new Date(song.lastActive).toISOString(),
    status: song.currentJobId && jobs.get(song.currentJobId)?.status === "running" ? "working" : "idle",
    track_count: song.player.scoreId === song.scoreId ? song.player.vocals.length : 0,
  });
  const workspace = (song: Song) => ({
    session_id: song.id,
    song: { id: song.id, title: song.title, title_edited: song.titleEdited, created_at: song.createdAt },
    score: {
      score_id: song.scoreId,
      current_score: { version: 1 },
      score_summary: scoreSummary,
      solfege_settings: { system: "movable_do", mode: "major", revision: 1 },
    },
    messages: song.history,
    // A running job, and each take asked for in the chat; a stopped take is in the chat text.
    jobs: [...jobs.values()]
      .filter((job) => job.sessionId === song.id && job.status !== "stopped")
      .map((job) => ({
        ...progress(job), originating_turn_id: job.turnId, created_at: job.createdAt, progress_url: progressUrl(job),
      })),
    player: {
      score_id: song.player.scoreId,
      revision: song.player.revision,
      repeats: false,
      vocals: song.player.vocals.map((vocal) => {
        const job = jobs.get(vocal.jobId)!;
        return {
          key: vocal.key, part_id: vocal.part, part_index: vocal.part === "Voice" ? 0 : 1, label: vocal.part,
          verse_number: "1", take_signature: null, source_job_id: vocal.jobId, expand_repeats: false,
          duration_seconds: vocal.seconds, voice_name: "Test Voice",
          muted: vocal.muted, solo: vocal.solo, volume: vocal.volume,
          audio_url: audioUrl(job), progress_url: progressUrl(job),
        };
      }),
      instruments: song.player.instruments,
      bus: song.player.bus,
    },
  });
  const notFound = { code: "song_not_found", message: "This song isn't available." };

  // The fallback synth is enough here; the 148 MB SoundFont would only slow the test.
  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    const route_ = url.pathname;
    if (route_ === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (route_ === "/readyz") return json({ ready: true, status: "ready" });
    if (route_ === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (route_ === "/api/voicebanks") return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    if (route_ === "/sessions" && request.method() === "POST") {
      sessionsCreated += 1;
      const id = `song-${sessionsCreated}`;
      songs.set(id, {
        id, isSong: false, deleted: false, title: "", titleEdited: false, createdAt: "", lastActive: clock,
        scoreId: null, currentJobId: null, history: [],
        player: { scoreId: null, revision: 0, vocals: [], instruments: [], bus: emptyBus },
      });
      return json({ session_id: id });
    }
    if (route_ === "/songs") {
      const listed = [...songs.values()].filter((song) => song.isSong && !song.deleted);
      listed.sort((left, right) => right.lastActive - left.lastActive);
      return json({ songs: listed.map(songRow) });
    }

    const songId = /^\/(?:sessions|songs)\/([^/]+)/.exec(route_)?.[1] ?? "";
    const song = songs.get(songId);
    const isSongRoute = route_.startsWith("/songs/");
    if (!song || song.deleted || (isSongRoute && !song.isSong)) {
      return json(isSongRoute ? notFound : { detail: "Session not found." }, 404);
    }
    // Like the real server, any request about a Song marks it active.
    song.lastActive = (clock += 1_000);

    if (isSongRoute) {
      if (route_.endsWith("/workspace")) return json(workspace(song));
      if (route_.endsWith("/player/mix")) {
        const mix = request.postDataJSON();
        if (mix.score_id !== song.scoreId) return json({ code: "score_changed", message: "The score changed." }, 409);
        for (const saved of mix.vocals) {
          const vocal = song.player.vocals.find((entry) => entry.key === saved.key);
          if (vocal) Object.assign(vocal, { muted: saved.muted, solo: saved.solo, volume: saved.volume });
        }
        song.player.instruments = mix.instruments;
        song.player.bus = mix.bus;
        song.player.revision += 1;
        return json({ revision: song.player.revision });
      }
      if (request.method() === "PATCH") {
        song.title = String(request.postDataJSON().title).trim();
        song.titleEdited = true;
        return json(songRow(song));
      }
      if (request.method() === "DELETE") {
        if (running(song.id).length) return json({ code: "work_running", message: "Still working." }, 409);
        song.deleted = true;
        return json({ deleted: true });
      }
      return json({});
    }

    if (route_.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (route_.endsWith("/upload")) {
      const body = request.postDataBuffer()?.toString("utf-8") ?? "";
      const name = /filename="([^"]+)"/.exec(body)?.[1] ?? "";
      const replace = /name="replace"\r\n\r\ntrue/.test(body);
      if (running(song.id).length) {
        if (!replace) return json({ code: "work_running", message: "Something is still running." }, 409);
        // The file is good; stop the work first. A job too late to stop keeps the score.
        if (stopWork(song).some((job) => job.status === "running")) {
          return json({ code: "stop_too_late", message: "Too late to stop." }, 409);
        }
      }
      uploads.push(name);
      const scoreId = `score-${uploads.length}`;
      if (!song.isSong) Object.assign(song, { isSong: true, createdAt: now() });
      if (!song.titleEdited) song.title = name.replace(/\.xml$/, "");
      song.scoreId = scoreId;
      song.player = { scoreId, revision: 0, vocals: [], instruments: [], bus: emptyBus };
      return json({
        session_id: song.id,
        score_id: scoreId,
        parsed: true,
        current_score: { version: 1 },
        score_summary: scoreSummary,
        performance_midi: null,
        song: { id: song.id, title: song.title, title_edited: song.titleEdited, created_at: song.createdAt },
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/instrumental-midi")) {
      return route.fulfill({ contentType: "audio/midi", body: multiTrackMidi([0, 24]) });
    }
    if (route_.endsWith("/audio")) {
      const seconds = Number(/-(\d+)s\.wav$/.exec(url.searchParams.get("file") ?? "")?.[1] ?? 1);
      return route.fulfill({ contentType: "audio/wav", body: silentWav(seconds) });
    }
    if (route_.endsWith("/stop")) {
      const active = stopWork(song);
      if (active.length === 0) return json({ result: "nothing_running", jobs: [] });
      return json({
        result: stopResult,
        jobs: active.map((job) => ({
          jobId: job.jobId, jobKind: "synthesis", result: stopResult, refundedCredits: 0,
        })),
      });
    }
    if (route_.endsWith("/chat")) {
      const turn = say(song, "user", String(request.postDataJSON()?.message ?? ""));
      const next = replies.shift();
      if (!next) throw new Error("No reply queued for this chat turn.");
      if ("text" in next) {
        if (next.hold) {
          await replyGate;
          replyGate = new Promise<void>((resolve) => { releaseReply = resolve; });
        }
        say(song, "assistant", next.text);
        return json({ type: "chat_text", message: next.text });
      }
      const job: Job = {
        ...next.take,
        sessionId: song.id,
        scoreId: song.scoreId ?? "",
        turnId: turn.id,
        createdAt: now(),
        status: "running",
      };
      jobs.set(job.jobId, job);
      song.currentJobId = job.jobId;
      const accepted = {
        type: "chat_progress",
        message: "Starting the take.",
        job_id: job.jobId,
        progress_url: progressUrl(job),
      };
      say(song, "assistant", accepted.message);
      // A held take is followed by polling its progress; the others finish in the stream.
      if (next.hold) return json(accepted);
      finish(job);
      const body = [
        `event: accepted\ndata: ${JSON.stringify(accepted)}\n`,
        `event: completed\ndata: ${JSON.stringify(progress(job))}\n`,
      ].join("\n");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `${body}\n` });
    }
    if (route_.endsWith("/progress")) {
      const job = jobs.get(url.searchParams.get("job_id") ?? "");
      return json(job ? progress(job) : {});
    }
    return json({});
  });
  return {
    reply: (next) => { replies.push(next); },
    releaseReply: () => releaseReply(),
    finishJob: (jobId) => {
      const job = jobs.get(jobId);
      if (!job) throw new Error(`Unknown job ${jobId}.`);
      finish(job);
    },
    setStopResult: (result) => { stopResult = result; },
    stopRequests: () => stopRequests,
    sessionsCreated: () => sessionsCreated,
    uploads: () => [...uploads],
  };
}

async function openStudio(page: Page, testId: string, options: { instruments?: boolean } = {}) {
  const backend = await mockBackend(page, options);
  // Hidden on every load, because these tests reload and change address.
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = ".announcement-overlay { display: none !important; }";
      document.head.append(style);
    });
  });
  await signInAsE2EUser(page, testId);
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  return backend;
}

/** Picks a score file. In a Song that already has a score, the choice popup follows. */
async function chooseScore(page: Page, name: string) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({ name, mimeType: "application/xml", buffer: xml });
}

/** Uploads the first score of a new Song, from the start screen. */
async function startSong(page: Page, name: string) {
  await chooseScore(page, name);
  await expect(page).toHaveURL(/\/app\/song\/song-\d+$/);
  await expect(scorePreview(page)).toBeVisible();
  await expect(songTitle(page)).toHaveText(name.replace(/\.xml$/, ""));
}

async function say(page: Page, backend: Backend, text: string, reply: Reply) {
  backend.reply(reply);
  await page.getByTestId("chat-input").fill(text);
  await page.getByTestId("send-message").click();
}

async function sing(page: Page, backend: Backend, take: Take) {
  await say(page, backend, `sing ${take.part}`, { take });
  await expect(page.getByTestId(`score-vocal-track-id:${take.part}`)).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

/** Starts a take that keeps running until `backend.finishJob`. */
async function startLongTake(page: Page, backend: Backend, take: Take) {
  await say(page, backend, `sing ${take.part}`, { take, hold: true });
  // The reply has arrived and the job is being followed.
  await expect(chat(page)).toContainText("Singing the take.");
  await expect(page.getByTestId("stop-work")).toBeVisible();
}

/** Leaves the open Song for the start screen. */
async function newSong(page: Page) {
  await page.getByTestId("new-song").click();
  await expect(page).toHaveURL(/\/app$/);
  await expect(songTitle(page)).toHaveCount(0);
}

const scorePreview = (page: Page) => page.getByTestId("score-preview-surface").locator("svg").first();
const songTitle = (page: Page) => page.getByTestId("song-title");
const chat = (page: Page) => page.getByTestId("chat-stream");
const vocalRows = (page: Page) => page.locator(".score-track-row.vocal-track");
const timeline = (page: Page) => page.locator(".score-player-seek-time");
const choicePopup = (page: Page) => page.getByRole("dialog", { name: "New song or replace?" });
const songRows = (page: Page) => page.getByRole("dialog", { name: "My songs" }).getByTestId("song-row");
const songRow = (page: Page, title: string) => songRows(page).filter({ hasText: title });

async function openMySongs(page: Page) {
  await page.getByTestId("my-songs").click();
  await expect(page.getByRole("dialog", { name: "My songs" })).toBeVisible();
}

test("a reloaded Song comes back with its chat, score, track and title", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-reload");
  await startSong(page, "first-tune.xml");
  await say(page, backend, "hello there", { text: "Hello from the studio." });
  await expect(chat(page)).toContainText("Hello from the studio.");
  await sing(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  await expect(timeline(page)).toHaveText("0:00 / 0:03");

  const address = page.url();
  const sessionsBefore = backend.sessionsCreated();
  await page.reload();
  await expect(page).toHaveURL(address);
  await expect(songTitle(page)).toHaveText("first-tune");
  await expect(scorePreview(page)).toBeVisible();
  await expect(chat(page)).toContainText("hello there");
  await expect(chat(page)).toContainText("Hello from the studio.");
  await expect(chat(page)).toContainText("Here is the rendered audio.");
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:03");
  // The Song is its own session: reopening it starts no new one.
  expect(backend.sessionsCreated()).toBe(sessionsBefore);

  // The reopened Song carries on: the next take lands in the same player.
  await sing(page, backend, { jobId: "job-b-2s", part: "Voice 2", seconds: 2 });
  await expect(vocalRows(page)).toHaveCount(2);
});

test("the mix is saved and comes back after a reload", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-mix", { instruments: true });
  await startSong(page, "first-tune.xml");
  await sing(page, backend, { jobId: "job-a-1s", part: "Voice", seconds: 1 });
  const vocalMute = () => page.getByTestId("score-vocal-track-id:Voice").locator(".score-track-mute-btn");
  const pianoSound = () => page.getByTestId("score-inst-track-P2").locator(".score-track-instrument-trigger");
  const guitarSound = () => page.getByTestId("score-inst-track-P3").locator(".score-track-instrument-trigger");
  const busLevel = () => page.getByRole("slider", { name: "Instrumental Level" });
  await expect(pianoSound().locator(".score-track-instrument-label")).toHaveText("Acoustic Grand Piano");

  await vocalMute().click();
  await pianoSound().click();
  await page.getByRole("menuitem", { name: "Piano", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "Bright Acoustic Piano" }).click();
  await expect(pianoSound().locator(".score-track-instrument-label")).toHaveText("Bright Acoustic Piano");
  await page.getByRole("slider", { name: "Guitar volume" }).fill("0.5");
  await busLevel().fill("-6");
  // The mix is saved about a second after the last change.
  await page.waitForTimeout(1_800);

  await page.reload();
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(vocalMute()).toHaveAttribute("aria-pressed", "true");
  await expect(pianoSound().locator(".score-track-instrument-label")).toHaveText("Bright Acoustic Piano");
  await expect(guitarSound().locator(".score-track-instrument-label")).toHaveText("Acoustic Guitar (Nylon)");
  await expect(page.getByRole("slider", { name: "Guitar volume" })).toHaveValue("0.5");
  await expect(busLevel()).toHaveValue("-6");
});

test("New song from the popup opens a second Song and leaves the first as it was", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-new-from-popup");
  await startSong(page, "first-tune.xml");
  await sing(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  const firstAddress = page.url();

  // Cancel leaves everything alone.
  await chooseScore(page, "second-tune.xml");
  await expect(choicePopup(page)).toBeVisible();
  await choicePopup(page).getByRole("button", { name: "Cancel" }).click();
  await expect(choicePopup(page)).toHaveCount(0);
  expect(backend.uploads()).toEqual(["first-tune.xml"]);
  await expect(songTitle(page)).toHaveText("first-tune");

  // New song is the default choice: Enter picks it.
  await chooseScore(page, "second-tune.xml");
  await expect(choicePopup(page).getByRole("button", { name: /^New song/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(songTitle(page)).toHaveText("second-tune");
  expect(page.url()).not.toBe(firstAddress);
  await expect(page).toHaveURL(/\/app\/song\/song-\d+$/);
  await expect(scorePreview(page)).toBeVisible();
  await expect(vocalRows(page)).toHaveCount(0);
  await expect(chat(page)).not.toContainText("Here is the rendered audio.");

  // The first Song is still in My songs, with its chat and its track.
  await openMySongs(page);
  await expect(songRows(page)).toHaveCount(2);
  await expect(songRow(page, "first-tune")).toContainText("1 sung part");
  await songRow(page, "first-tune").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(firstAddress);
  await expect(songTitle(page)).toHaveText("first-tune");
  await expect(chat(page)).toContainText("Here is the rendered audio.");
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:03");
});

test("Replace keeps the chat, clears the player and follows the new score's title", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-replace");
  await startSong(page, "first-tune.xml");
  await say(page, backend, "hello there", { text: "Hello from the studio." });
  await sing(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  const address = page.url();

  await chooseScore(page, "second-tune.xml");
  await choicePopup(page).getByRole("button", { name: /^Replace/ }).click();
  await expect(songTitle(page)).toHaveText("second-tune");
  await expect(page).toHaveURL(address);
  await expect(vocalRows(page)).toHaveCount(0);
  await expect(chat(page)).toContainText("Hello from the studio.");
  await expect(chat(page)).toContainText("Here is the rendered audio.");
  expect(backend.uploads()).toEqual(["first-tune.xml", "second-tune.xml"]);

  // Escape leaves the title alone; Enter saves it.
  await songTitle(page).click();
  await page.getByRole("textbox", { name: "Song title" }).fill("Not this one");
  await page.keyboard.press("Escape");
  await expect(songTitle(page)).toHaveText("second-tune");
  await songTitle(page).click();
  await page.getByRole("textbox", { name: "Song title" }).fill("My own title");
  await page.keyboard.press("Enter");
  await expect(songTitle(page)).toHaveText("My own title");

  // A title the user chose survives the next Replace, and a reload.
  await chooseScore(page, "third-tune.xml");
  await choicePopup(page).getByRole("button", { name: /^Replace/ }).click();
  await expect.poll(() => backend.uploads()).toHaveLength(3);
  await expect(chat(page)).toContainText("Hello from the studio.");
  await expect(songTitle(page)).toHaveText("My own title");
  await page.reload();
  await expect(songTitle(page)).toHaveText("My own title");
  await expect(chat(page)).toContainText("Hello from the studio.");
  await expect(vocalRows(page)).toHaveCount(0);
});

test("My songs lists Songs newest first, opens them, and deletes after asking", async ({ page }) => {
  test.setTimeout(120_000);
  await openStudio(page, "songs-my-songs");
  await startSong(page, "first-tune.xml");
  const firstAddress = page.url();
  await newSong(page);
  await startSong(page, "second-tune.xml");
  const secondAddress = page.url();
  expect(secondAddress).not.toBe(firstAddress);

  await openMySongs(page);
  await expect(songRows(page).locator(".songs-drawer-row-title")).toHaveText(["second-tune", "first-tune"]);
  await expect(songRow(page, "second-tune")).toContainText("Open now");
  await expect(songRow(page, "first-tune")).toContainText("No sung parts yet");
  await songRow(page, "first-tune").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(firstAddress);
  await expect(songTitle(page)).toHaveText("first-tune");
  await expect(scorePreview(page)).toBeVisible();

  // Delete asks first. "Keep" changes nothing.
  await openMySongs(page);
  await songRow(page, "second-tune").getByRole("button", { name: "Delete" }).click();
  await expect(songRow(page, "second-tune")).toContainText("Delete this song and its takes?");
  await songRow(page, "second-tune").getByRole("button", { name: "Keep" }).click();
  await expect(songRows(page)).toHaveCount(2);
  await songRow(page, "second-tune").getByRole("button", { name: "Delete" }).click();
  await songRow(page, "second-tune").getByRole("button", { name: "Delete" }).click();
  await expect(songRows(page)).toHaveCount(1);
  await expect(page).toHaveURL(firstAddress);

  // Deleting the open Song goes back to the start screen.
  await songRow(page, "first-tune").getByRole("button", { name: "Delete" }).click();
  await songRow(page, "first-tune").getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/\/app$/);
  await expect(songTitle(page)).toHaveCount(0);
  await openMySongs(page);
  await expect(page.getByRole("dialog", { name: "My songs" })).toContainText("No songs yet.");

  // A deleted Song's address no longer opens it.
  await page.goto(secondAddress);
  await expect(page.getByRole("alert")).toContainText("This song isn't available");
});

test("a take keeps running while another Song is open, and shows when you come back", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-leave-during-take");
  await startSong(page, "first-tune.xml");
  const firstAddress = page.url();
  await newSong(page);
  await startSong(page, "second-tune.xml");
  const secondAddress = page.url();

  await startLongTake(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });

  // A working Song says so in My songs, and can't be deleted.
  await openMySongs(page);
  await expect(songRow(page, "second-tune")).toContainText("Working…");
  await expect(songRow(page, "second-tune").getByRole("button", { name: "Delete" })).toBeDisabled();

  // Opening another Song stops nothing.
  await songRow(page, "first-tune").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(firstAddress);
  await expect(songTitle(page)).toHaveText("first-tune");
  await expect(page.getByTestId("stop-work")).toHaveCount(0);
  expect(backend.stopRequests()).toBe(0);

  // Back in the working Song: the take is still in progress, then lands.
  await openMySongs(page);
  await expect(songRow(page, "second-tune")).toContainText("Working…");
  await songRow(page, "second-tune").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(secondAddress);
  await expect(chat(page)).toContainText("sing Voice");
  await expect(page.getByTestId("stop-work")).toBeVisible();
  await expect(vocalRows(page)).toHaveCount(0);
  backend.finishJob("job-a-3s");
  await expect(chat(page)).toContainText("Here is the rendered audio.");
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(timeline(page)).toHaveText("0:00 / 0:03");
  await expect(page.getByTestId("stop-work")).toHaveCount(0);
  expect(backend.stopRequests()).toBe(0);
});

test("a take that finished while its Song was closed is there when it is reopened", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-finished-while-away");
  await startSong(page, "first-tune.xml");
  await newSong(page);
  await startSong(page, "second-tune.xml");
  await startLongTake(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });

  await openMySongs(page);
  await songRow(page, "first-tune").getByRole("button", { name: "Open" }).click();
  await expect(songTitle(page)).toHaveText("first-tune");
  backend.finishJob("job-a-3s");

  await openMySongs(page);
  await expect(songRow(page, "second-tune")).toContainText("1 sung part");
  await expect(songRow(page, "second-tune")).not.toContainText("Working…");
  await songRow(page, "second-tune").getByRole("button", { name: "Open" }).click();
  await expect(songTitle(page)).toHaveText("second-tune");
  await expect(chat(page)).toContainText("Here is the rendered audio.");
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(page.getByTestId("stop-work")).toHaveCount(0);
});

test("New song and Open are held while the assistant's reply is awaited", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-blocked-while-replying");
  await startSong(page, "first-tune.xml");
  await newSong(page);
  await startSong(page, "second-tune.xml");

  await say(page, backend, "hello there", { text: "Hello from the studio.", hold: true });
  await expect(page.getByTestId("new-song")).toBeDisabled();
  await expect(page.getByTestId("new-song")).toHaveAttribute("title", "Wait for the reply first");
  await expect(page.getByTestId("score-upload-input")).toBeDisabled();
  await openMySongs(page);
  await expect(songRow(page, "first-tune").getByRole("button", { name: "Open" })).toBeDisabled();
  await page.getByRole("button", { name: "Close My songs" }).click();

  backend.releaseReply();
  await expect(chat(page)).toContainText("Hello from the studio.");
  await expect(page.getByTestId("new-song")).toBeEnabled();
  await expect(page.getByTestId("score-upload-input")).toBeEnabled();
  await openMySongs(page);
  await expect(songRow(page, "first-tune").getByRole("button", { name: "Open" })).toBeEnabled();
});

test("an unknown Song address says so and links back to the start screen", async ({ page }) => {
  await openStudio(page, "songs-not-available");
  await page.goto("/app/song/no-such-song");
  await expect(page.getByRole("alert")).toContainText("This song isn't available");
  await page.getByRole("link", { name: "Start a new song" }).click();
  await expect(page).toHaveURL(/\/app$/);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await startSong(page, "first-tune.xml");
});

test("a reopened Song with two sung parts shows exactly two vocal tracks", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-no-double-tracks");
  await startSong(page, "first-tune.xml");
  await sing(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  await sing(page, backend, { jobId: "job-b-2s", part: "Voice 2", seconds: 2 });
  // A second take of the first part replaces its track.
  await sing(page, backend, { jobId: "job-c-5s", part: "Voice", seconds: 5 });
  await expect(vocalRows(page)).toHaveCount(2);

  await page.reload();
  await expect(timeline(page)).toHaveText("0:00 / 0:05");
  await expect(vocalRows(page)).toHaveCount(2);
  // All three takes are still in the chat; none of them adds a track of its own.
  await expect(chat(page).getByText("Here is the rendered audio.")).toHaveCount(3);
  await page.waitForTimeout(2_000);
  await expect(vocalRows(page)).toHaveCount(2);
  await expect(timeline(page)).toHaveText("0:00 / 0:05");
});

test("Replace waits when the running job is too late to stop", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await openStudio(page, "songs-replace-too-late");
  await startSong(page, "first-tune.xml");
  await startLongTake(page, backend, { jobId: "job-a-3s", part: "Voice", seconds: 3 });
  backend.setStopResult("too_late");

  await chooseScore(page, "second-tune.xml");
  await expect(choicePopup(page)).toContainText("the running work is stopped");
  await choicePopup(page).getByRole("button", { name: /^Replace/ }).click();
  await expect(page.getByText("The current job is just finishing and can't be stopped.")).toBeVisible();
  await expect(choicePopup(page)).toHaveCount(0);
  await expect(songTitle(page)).toHaveText("first-tune");
  expect(backend.uploads()).toEqual(["first-tune.xml"]);

  // The file is kept: the choice comes back when the job ends.
  backend.finishJob("job-a-3s");
  await expect(choicePopup(page)).toBeVisible();
  await expect(choicePopup(page)).toContainText("second-tune.xml");
  await expect(vocalRows(page)).toHaveCount(1);
  await choicePopup(page).getByRole("button", { name: /^Replace/ }).click();
  await expect(songTitle(page)).toHaveText("second-tune");
  await expect(vocalRows(page)).toHaveCount(0);
});
