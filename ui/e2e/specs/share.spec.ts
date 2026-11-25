import { expect, test, type Browser, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// Share (step 7, S3) on a mocked backend that answers as the share API in
// docs/specs/share-step7-codex-spec.md: the owner's share box, My songs, and the
// public page at /s/{token}, which plays the Song with the owner's mix as each
// row's start.

type Json = Record<string, unknown>;
type Vocal = { key: string; label: string; mode: "lyrics" | "solfege"; muted?: boolean; solo?: boolean };
type Backend = {
  attach: (page: Page) => Promise<void>;
  /** The link's token while sharing is on. */
  token: () => string | null;
  shareRequests: Json[];
  /** Soundfont requests from share pages. */
  soundfontRequests: () => number;
  /** Requests to the app's private endpoints (/sessions, /songs) from share pages. */
  privateRequests: string[];
  state: {
    available: boolean;
    noticeAccepted: boolean;
    vocals: Vocal[];
    instruments: boolean;
    pageAnswer: { status: number; body: Json } | null;
  };
};

const SONG = "song-1";
const TITLE = "Test Hymn";

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

/** A type-1 MIDI file with one one-second note on one track. */
function oneTrackMidi(): Buffer {
  const events = Buffer.from([
    0x00, 0xc0, 0x00, 0x00, 0x90, 60, 0x64, 0x87, 0x40, 0x80, 60, 0x40, 0x00, 0xff, 0x2f, 0x00,
  ]);
  const trackHeader = Buffer.from([0x4d, 0x54, 0x72, 0x6b, 0, 0, 0, 0]);
  trackHeader.writeUInt32BE(events.length, 4);
  const header = Buffer.from([0x4d, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 1, 0, 1, 0x01, 0xe0]);
  return Buffer.concat([header, trackHeader, events]);
}

const PERFORMANCE_MIDI = {
  version: 1,
  has_instrumental_parts: true,
  original_midi_available: true,
  expanded_midi_available: false,
  instrumental_parts: [{
    part_index: 2, part_id: "P3", raw_part_id: "P3", label: "Piano", eligible: true,
    has_lyrics: false, midi_program: 0, percussion: false,
  }],
};

async function createBackend(): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const wav = silentWav(2);
  let token: string | null = null;
  let tokens = 0;
  let soundfonts = 0;
  const backend: Backend = {
    attach: async () => undefined,
    token: () => token,
    shareRequests: [],
    soundfontRequests: () => soundfonts,
    privateRequests: [],
    state: { available: true, noticeAccepted: false, vocals: [], instruments: false, pageAnswer: null },
  };
  const { state } = backend;
  const summary = () => ({
    duration_seconds: 2,
    parts: [
      { part_id: "P1", part_index: 0, part_name: "Soprano", has_lyrics: true },
      { part_id: "P2", part_index: 1, part_name: "Alto", has_lyrics: true },
    ],
    performance_midi: state.instruments ? PERFORMANCE_MIDI : null,
  });
  const shareUrl = (page: Page) => `${new URL(page.url()).origin}/s/${token}`;
  // Made-up credits; Arranger and Score copyright left empty, so the page leaves them out.
  const songInfo = () => ({
    title: TITLE, subtitle: "For made-up voices", composer: "Made-up Composer", lyricist: "",
    arranger: "", credits: "Arranged by Made-up Name", score_copyright: "", producer: "Pat Producer",
    license: "cc_by_4",
    description: "Made-up notes.\nSecond line.", voices: state.vocals.length ? ["Voice A"] : [],
  });
  const publicPage = () => ({
    title: TITLE,
    is_owner: false,
    song_id: null,
    score: {
      version: 1,
      url: `/public/s/${token}/score`,
      summary: { duration_seconds: 2, performance_measure_map: null, performance_midi: summary().performance_midi },
    },
    player: {
      repeats: false,
      vocals: state.vocals.map((vocal) => ({
        key: vocal.key, mode: vocal.mode, label: vocal.label, duration_seconds: 2, voice_name: "Voice A",
        muted: Boolean(vocal.muted), solo: Boolean(vocal.solo), volume: 1,
        audio_url: `/public/s/${token}/audio?track=${encodeURIComponent(vocal.key)}`,
      })),
      instruments: [],
      bus: { muted: false, solo: false, level_db: 0 },
      midi_url: state.instruments ? `/public/s/${token}/midi?expand_repeats=false` : null,
    },
    credits: { voices: state.vocals.length ? ["Voice A"] : [] },
    info: songInfo(),
  });

  backend.attach = async (page: Page) => {
    await page.route("**/soundfonts/**", (route) => {
      if (new URL(page.url()).pathname.startsWith("/s/")) soundfonts += 1;
      return route.fulfill({ status: 404 });
    });
    await page.route("http://127.0.0.1:8000/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
      const at = url.pathname;
      if (new URL(page.url()).pathname.startsWith("/s/") && /^\/(sessions|songs)\b/.test(at)) {
        backend.privateRequests.push(at);
      }
      if (at === "/credits") {
        return json({ balance: 42, reserved: 0, available: 42, is_expired: false, overdrafted: false });
      }
      if (at === "/readyz") return json({ ready: true, status: "ready" });
      if (at === "/maintenance/status") return json({ enabled: false, allowed: true });
      if (at === "/api/voicebanks") return json({ voicebanks: [{ id: "voice-a", name: "Voice A" }] });
      if (at === "/songs") {
        return json({ songs: [{
          id: SONG, title: TITLE, created_at: "2026-01-01T00:00:00Z", last_active_at: new Date().toISOString(),
          status: "idle", track_count: state.vocals.length, shared: Boolean(token),
        }] });
      }
      if (at === `/songs/${SONG}/share`) {
        if (request.method() === "DELETE") {
          backend.shareRequests.push({ method: "DELETE" });
          token = null;
          return json({ enabled: false, url: null });
        }
        const body = (request.postDataJSON() ?? {}) as Json;
        backend.shareRequests.push({ method: "POST", ...body });
        if (!state.noticeAccepted) {
          if (body.notice_accepted !== true) {
            return json({ code: "share_notice_required", message: "Accept the notice first." }, 409);
          }
          state.noticeAccepted = true;
        }
        token ??= `tok${String(++tokens).padStart(19, "0")}`;
        return json({ enabled: true, url: shareUrl(page) });
      }
      if (at === `/songs/${SONG}/workspace`) {
        return json({
          session_id: SONG,
          song: {
            id: SONG, title: TITLE, title_edited: false, created_at: "2026-01-01T00:00:00Z",
            share: { available: state.available, enabled: Boolean(token), url: token ? shareUrl(page) : null },
            info: songInfo(),
          },
          score: {
            score_id: "score-1", current_score: { version: 1 }, score_summary: summary(),
            solfege_settings: { system: "movable_do", mode: "major", revision: 1 },
          },
          messages: [],
          jobs: [],
          player: {
            score_id: "score-1", revision: 0, repeats: false, instruments: [],
            bus: { muted: false, solo: false, level_db: 0 },
            vocals: state.vocals.map((vocal) => ({
              key: vocal.key, label: vocal.label, mode: vocal.mode, part_id: vocal.key.split(":")[0],
              part_index: 0, source_job_id: `take-${vocal.key}`, muted: Boolean(vocal.muted),
              solo: Boolean(vocal.solo), volume: 1, audio_url: `http://127.0.0.1:8000/audio/${vocal.key}.wav`,
              progress_url: "",
            })),
          },
        });
      }
      if (at.startsWith("/public/s/")) {
        const [, , , pageToken, file] = at.split("/");
        if (state.pageAnswer) return json(state.pageAnswer.body, state.pageAnswer.status);
        if (!token || pageToken !== token) {
          return json({ code: "share_off", message: "This link was turned off." }, 404);
        }
        if (file === "score") return route.fulfill({ contentType: "application/xml", body: xml });
        if (file === "midi") return route.fulfill({ contentType: "audio/midi", body: oneTrackMidi() });
        if (file === "audio") return route.fulfill({ contentType: "audio/wav", body: wav });
        return json(publicPage());
      }
      if (at.startsWith("/audio/")) return route.fulfill({ contentType: "audio/wav", body: wav });
      if (at.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
      if (at.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
      if (at.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
      if (at === "/sing-all/active") return json({ job: null });
      return json({});
    });
  };
  return backend;
}

async function hideOverlays(page: Page) {
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = ".announcement-overlay { display: none !important; }";
      document.head.append(style);
    });
  });
}

async function declineCookies(page: Page) {
  const decline = page.getByRole("button", { name: "Decline" });
  await decline.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await decline.isVisible()) await decline.click();
}

async function openSongAsOwner(page: Page, backend: Backend, testId: string) {
  await backend.attach(page);
  await hideOverlays(page);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await signInAsE2EUser(page, testId);
  await declineCookies(page);
  await page.goto(`/app/song/${SONG}`);
  await expect(page.getByTestId("song-title")).toHaveText(TITLE);
}

/** A signed-out viewer, in a browser context of their own. */
async function openAsViewer(browser: Browser, backend: Backend, link: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await backend.attach(page);
  await page.goto(link);
  await declineCookies(page);
  return page;
}

const shareBox = (page: Page) => page.getByRole("dialog", { name: "Share this song" });
const row = (page: Page, key: string) => page.getByTestId(`share-row-${key}`);
const rowSwitch = (page: Page, key: string) => row(page, key).getByRole("button");

async function shareFromTheApp(page: Page): Promise<string> {
  await page.getByTestId("share-song").click();
  const notice = page.getByRole("dialog", { name: "Before you share" });
  if (await notice.isVisible().catch(() => false)) await notice.getByRole("button", { name: "Share" }).click();
  const link = shareBox(page).getByRole("textbox", { name: "Share link" });
  await expect(link).toHaveValue(/\/s\/tok/);
  return link.inputValue();
}

test("start rows: the owner's mute, solo and bus decide where each row starts", async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { startRows } = await import("/src/share/startRows.ts");
    const vocal = (key: string, extra: Record<string, unknown> = {}) => ({
      key, mode: "lyrics", label: key, muted: false, solo: false, volume: 0.8, audio_url: "", ...extra,
    });
    const part = (id: string, extra: Record<string, unknown> = {}) => ({
      part_index: 0, part_id: id, raw_part_id: id, label: id, eligible: true, midi_program: 0,
      percussion: false, ...extra,
    });
    const instrument = (id: string, extra: Record<string, unknown> = {}) => ({
      raw_part_id: id, muted: false, solo: false, volume: 0.5, gm_program: 0, soundfont_bank: 0,
      preset_kind: "melodic", unassigned: false, ...extra,
    });
    const midi = { instrumental_parts: [part("P3"), part("P4"), part("P5", { program_source: "user_assignable" })] };
    const bus = { muted: false, solo: false, level_db: -6 };
    const on = (rows: { vocals: Array<{ key: string; on: boolean }>; instruments: Array<{ rawPartId: string; on: boolean }> }) => ({
      vocals: rows.vocals.map((r) => `${r.key}:${r.on}`),
      instruments: rows.instruments.map((r) => `${r.rawPartId}:${r.on}`),
    });
    const plain = startRows(
      { repeats: false, vocals: [vocal("A"), vocal("B", { muted: true })], instruments: [instrument("P3"), instrument("P4", { muted: true })], bus, midi_url: "x" },
      midi
    );
    const soloed = startRows(
      { repeats: false, vocals: [vocal("A", { solo: true }), vocal("B")], instruments: [instrument("P3")], bus, midi_url: "x" },
      midi
    );
    const busSolo = startRows(
      { repeats: false, vocals: [vocal("A")], instruments: [instrument("P3"), instrument("P4")], bus: { ...bus, solo: true }, midi_url: "x" },
      midi
    );
    const busMuted = startRows(
      { repeats: false, vocals: [vocal("A")], instruments: [instrument("P3")], bus: { ...bus, muted: true }, midi_url: "x" },
      midi
    );
    return {
      plain: on(plain),
      soloed: on(soloed),
      busSolo: on(busSolo),
      busMuted: on(busMuted),
      levels: [plain.vocals[0].level, Math.round(plain.instruments[0].level * 1000) / 1000],
      silent: plain.silent.map((r: { rawPartId: string }) => r.rawPartId),
      noMidi: on(startRows({ repeats: false, vocals: [vocal("A")], instruments: [], bus, midi_url: null }, null)),
    };
  });
  expect(result.plain).toEqual({ vocals: ["A:true", "B:false"], instruments: ["P3:true", "P4:false"] });
  expect(result.soloed).toEqual({ vocals: ["A:true", "B:false"], instruments: ["P3:false", "P4:false"] });
  expect(result.busSolo).toEqual({ vocals: ["A:false"], instruments: ["P3:true", "P4:true"] });
  expect(result.busMuted).toEqual({ vocals: ["A:true"], instruments: ["P3:false", "P4:false"] });
  expect(result.levels).toEqual([0.8, 0.251]);  // 0.5 at -6 dB
  expect(result.silent).toEqual(["P5"]);  // a part with no sound chosen is not a row
  expect(result.noMidi).toEqual({ vocals: ["A:true"], instruments: [] });
});

test("Share shows the notice the first time, then the link, and Copy copies it", async ({ page }) => {
  const backend = await createBackend();
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-first-time");

  await page.getByTestId("share-song").click();
  const notice = page.getByRole("dialog", { name: "Before you share" });
  await expect(notice).toContainText("Share only music you have the right to share.");
  await notice.getByRole("button", { name: "Cancel" }).click();
  await expect(notice).toHaveCount(0);
  expect(backend.token()).toBeNull();

  await page.getByTestId("share-song").click();
  await notice.getByRole("button", { name: "Share" }).click();
  const link = shareBox(page).getByRole("textbox", { name: "Share link" });
  await expect(link).toHaveValue(`http://127.0.0.1:${new URL(page.url()).port}/s/${backend.token()}`);
  expect(backend.shareRequests.at(-1)).toEqual({ method: "POST", notice_accepted: true });
  await shareBox(page).getByRole("button", { name: "Copy" }).click();
  await expect(shareBox(page).getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(await link.inputValue());

  // Once accepted, sharing goes straight to the link; while on, the box opens on it.
  await page.keyboard.press("Escape");
  await page.reload();
  await expect(page.getByTestId("song-title")).toHaveText(TITLE);
  const requests = backend.shareRequests.length;
  await page.getByTestId("share-song").click();
  await expect(link).toHaveValue(/\/s\/tok/);
  expect(backend.shareRequests.length).toBe(requests);
});

test("Turn off asks first; sharing again makes a new link and the old one stays off", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-turn-off");
  const first = await shareFromTheApp(page);

  await shareBox(page).getByRole("button", { name: "Turn off sharing" }).click();
  const confirm = shareBox(page).getByRole("alertdialog", { name: "Turn off sharing?" });
  await expect(confirm).toContainText("Sharing again makes a new link.");
  await confirm.getByRole("button", { name: "Keep sharing" }).click();
  expect(backend.token()).not.toBeNull();
  await shareBox(page).getByRole("button", { name: "Turn off sharing" }).click();
  await confirm.getByRole("button", { name: "Turn off" }).click();
  await expect(shareBox(page)).toHaveCount(0);
  expect(backend.token()).toBeNull();

  const viewer = await openAsViewer(browser, backend, first);
  await expect(viewer.getByText("This link was turned off")).toBeVisible();

  const second = await shareFromTheApp(page);
  expect(second).not.toBe(first);
  await viewer.goto(second);
  await expect(viewer.getByRole("heading", { name: TITLE })).toBeVisible();
  await viewer.goto(first);
  await expect(viewer.getByText("This link was turned off")).toBeVisible();
  await viewer.context().close();
});

test("My songs marks a shared Song, and its Share action turns the link off there too", async ({ page }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-my-songs");
  await shareFromTheApp(page);
  await page.keyboard.press("Escape");

  await page.getByTestId("my-songs").click();
  const drawer = page.getByRole("dialog", { name: "My songs" });
  const songRow = drawer.getByTestId("song-row").filter({ hasText: TITLE });
  await expect(songRow.getByText("Shared", { exact: true })).toBeVisible();
  await songRow.getByRole("button", { name: "Share" }).click();
  await expect(shareBox(page).getByRole("textbox", { name: "Share link" })).toHaveValue(/\/s\/tok/);
  const info = shareBox(page).getByRole("region", { name: "Song info" });
  await expect(info).toContainText("Made-up Composer");
  await expect(info.getByRole("button", { name: "Edit" })).toHaveCount(0);
  await shareBox(page).getByRole("button", { name: "Turn off sharing" }).click();
  await shareBox(page).getByRole("button", { name: "Turn off" }).click();
  await expect(shareBox(page)).toHaveCount(0);
  await expect(drawer).toBeVisible();
  await expect(songRow.getByText("Shared", { exact: true })).toHaveCount(0);
});

test("the page, signed out: title, score, one row per sung part, and Play plays", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push(
    { key: "P1:lyrics", label: "Soprano", mode: "lyrics" },
    { key: "P2:lyrics", label: "Alto", mode: "lyrics" },
  );
  await openSongAsOwner(page, backend, "share-page");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  await expect(viewer.getByRole("heading", { name: TITLE })).toBeVisible();
  await expect(viewer).toHaveTitle(`${TITLE} · SightSinger`);
  await expect(viewer.locator('meta[name="robots"]')).toHaveAttribute("content", "noindex, nofollow");
  await expect(viewer.locator(".share-song-score svg").first()).toBeVisible();
  await expect(viewer.getByTestId(/^share-row-/)).toHaveCount(2);
  // Song info under Parts carries the voices; the old Voices line under Parts is gone.
  const info = viewer.locator(".share-song-info");
  await expect(info).toContainText("ComposerMade-up Composer");
  await expect(info).toContainText("VoicesVoice A");
  await expect(info.getByRole("link", { name: "CC BY 4.0" })).toHaveAttribute(
    "href", "https://creativecommons.org/licenses/by/4.0/");
  await expect(info).toContainText("CreditsArranged by Made-up Name");
  await expect(info).not.toContainText("Arranger");
  await expect(info.locator(".share-song-description")).toHaveText("Made-up notes.\nSecond line.");
  await expect(viewer.locator(".share-song-subtitle")).toHaveText("For made-up voices");
  await expect(viewer.locator(".share-song-credits")).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/app");
  await expect(viewer.getByRole("link", { name: "Open in SightSinger" })).toHaveCount(0);

  const play = viewer.getByRole("button", { name: "Play" });
  await expect(play).toBeEnabled();
  await play.click();
  await expect(viewer.getByRole("button", { name: "Pause" })).toBeVisible();

  // Nothing to download or edit, and no instruments, so no soundfont.
  await expect(viewer.getByRole("button", { name: /download|export/i })).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: /download/i })).toHaveCount(0);
  await expect(viewer.getByTestId("chat-input")).toHaveCount(0);
  await expect(viewer.getByRole("combobox")).toHaveCount(0);
  await expect(viewer.getByRole("slider")).toHaveCount(2);  // the seek bar and the volume, no faders
  expect(backend.soundfontRequests()).toBe(0);
  expect(backend.privateRequests).toEqual([]);
  await viewer.context().close();
});

test("the Share dialog shows read-only Song info and Edit opens the panel", async ({ page }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-info-edit");
  await shareFromTheApp(page);
  const info = shareBox(page).getByRole("region", { name: "Song info" });
  await expect(info.getByRole("term")).toHaveText([
    "Title", "Subtitle", "Composer", "Lyrics by", "Arranger", "Credits", "Score copyright",
    "Produced by", "Voices", "License", "Description",
  ]);
  await expect(info.getByRole("definition")).toHaveText([
    TITLE, "For made-up voices", "Made-up Composer", "—", "—", "Arranged by Made-up Name", "—",
    "Pat Producer", "Voice A", "CC BY 4.0", "Made-up notes.\nSecond line.",
  ]);
  await expect(shareBox(page).getByRole("textbox")).toHaveCount(1);
  await info.getByRole("button", { name: "Edit" }).click();
  await expect(shareBox(page)).toHaveCount(0);
  const panel = page.getByTestId("song-info-panel");
  await expect(panel.getByLabel("Composer")).toHaveValue("Made-up Composer");
  await expect(panel.getByLabel("License")).toHaveValue("cc_by_4");
});

test("the score opens in page view, switches to horizontal, and the page fits a phone", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-layout");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  const score = viewer.locator(".share-song-score");
  await expect(viewer.getByRole("button", { name: "Page view" })).toHaveAttribute("aria-pressed", "true");
  await expect(score.locator(".score-page-virtualizer svg").first()).toBeVisible();
  await expect(score).not.toHaveClass(/horizontal-layout/);

  await viewer.getByRole("button", { name: "Horizontal view" }).click();
  await expect(viewer.getByRole("button", { name: "Horizontal view" })).toHaveAttribute("aria-pressed", "true");
  await expect(score).toHaveClass(/horizontal-layout/);
  await expect(score.locator("svg").first()).toBeVisible();

  // The app's zoom control: steps of 10%, kept when the view switches.
  await expect(viewer.locator(".zoom-value")).toHaveText("100%");
  await viewer.getByRole("button", { name: "Zoom in" }).click();
  await expect(viewer.locator(".zoom-value")).toHaveText("110%");
  await expect(score.locator("svg").first()).toBeVisible();
  await viewer.getByRole("button", { name: "Page view" }).click();
  await expect(viewer.locator(".zoom-value")).toHaveText("110%");
  await expect(score.locator(".score-page-virtualizer svg").first()).toBeVisible();
  await viewer.getByRole("button", { name: "Zoom out" }).click();
  await viewer.getByRole("button", { name: "Zoom out" }).click();
  await expect(viewer.locator(".zoom-value")).toHaveText("90%");
  await expect(score.locator(".score-page-virtualizer svg").first()).toBeVisible();

  // On a phone the page stays inside the screen; the score scrolls in its own box.
  await viewer.setViewportSize({ width: 375, height: 812 });
  const widths = await viewer.evaluate(() => ({
    screen: document.documentElement.clientWidth,
    page: Math.round(document.querySelector(".share-page")!.getBoundingClientRect().right),
    left: Math.round(document.querySelector(".share-page")!.getBoundingClientRect().left),
  }));
  expect(widths.left).toBeGreaterThanOrEqual(0);
  expect(widths.page).toBeLessThanOrEqual(widths.screen);
  await expect(score).toHaveCSS("overflow-y", "auto");
});

test("a laptop puts the tracks left of the score; a portrait screen shows two parts a row, a phone one; the list folds away", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push(
    { key: "P1:lyrics", label: "Soprano", mode: "lyrics" },
    { key: "P2:lyrics", label: "Alto", mode: "lyrics" },
    { key: "P3:lyrics", label: "Tenor", mode: "lyrics" },
  );
  await openSongAsOwner(page, backend, "share-parts");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  const box = async (key: string) => (await row(viewer, key).boundingBox())!;
  const area = async (selector: string) => (await viewer.locator(selector).boundingBox())!;

  // A laptop: the tracks in the left third, one per row; the player bar (with zoom and the
  // view toggle) above the score in the other two thirds; the page fits the window.
  await viewer.setViewportSize({ width: 1280, height: 720 });
  await expect(row(viewer, "P3:lyrics")).toBeVisible();
  const [tracks, controls, score] = [
    await area(".share-song-tracks"), await area(".share-song-controls"), await area(".share-song-score"),
  ];
  expect(tracks.x + tracks.width).toBeLessThanOrEqual(controls.x);
  expect(Math.round(score.width / tracks.width)).toBe(2);
  expect(controls.y + controls.height).toBeLessThanOrEqual(score.y);
  expect(Math.abs(controls.x - score.x)).toBeLessThan(1);
  for (const name of ["Play", "Stop", "Zoom in", "Page view"]) {
    const button = (await viewer.getByRole("button", { name, exact: true }).boundingBox())!;
    expect(button.y).toBeGreaterThanOrEqual(controls.y);
    expect(button.y + button.height).toBeLessThanOrEqual(controls.y + controls.height);
  }
  expect((await box("P2:lyrics")).y).toBeGreaterThan((await box("P1:lyrics")).y);
  expect(await viewer.evaluate(() => document.body.scrollHeight)).toBeLessThanOrEqual(720);

  await viewer.setViewportSize({ width: 768, height: 1024 });
  let [soprano, alto, tenor] = [await box("P1:lyrics"), await box("P2:lyrics"), await box("P3:lyrics")];
  expect(alto.y).toBe(soprano.y);
  expect(alto.x).toBeGreaterThan(soprano.x);
  expect(tenor.y).toBeGreaterThan(soprano.y);
  expect(tenor.x).toBe(soprano.x);

  await viewer.setViewportSize({ width: 375, height: 812 });
  [soprano, alto] = [await box("P1:lyrics"), await box("P2:lyrics")];
  expect(alto.y).toBeGreaterThan(soprano.y);
  expect(alto.x).toBe(soprano.x);

  const toggle = viewer.getByRole("button", { name: /^Parts/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toContainText("3 of 3 on");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(viewer.getByTestId(/^share-row-/)).toHaveCount(3);
  await expect(row(viewer, "P1:lyrics")).toBeHidden();
  await toggle.click();
  await expect(row(viewer, "P1:lyrics")).toBeVisible();
  await viewer.context().close();
});

test("the owner's mix: a muted part starts off, a soloed one alone starts on, and a reload restores it", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push(
    { key: "P1:lyrics", label: "Soprano", mode: "lyrics", muted: true },
    { key: "P2:lyrics", label: "Alto", mode: "lyrics" },
  );
  await openSongAsOwner(page, backend, "share-mix");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  await expect(rowSwitch(viewer, "P1:lyrics")).toHaveAttribute("aria-pressed", "false");
  await expect(rowSwitch(viewer, "P2:lyrics")).toHaveAttribute("aria-pressed", "true");
  await rowSwitch(viewer, "P1:lyrics").click();
  await expect(rowSwitch(viewer, "P1:lyrics")).toHaveAttribute("aria-pressed", "true");
  await viewer.reload();
  await expect(rowSwitch(viewer, "P1:lyrics")).toHaveAttribute("aria-pressed", "false");

  backend.state.vocals[0].muted = false;
  backend.state.vocals[1].solo = true;
  await viewer.reload();
  await expect(rowSwitch(viewer, "P1:lyrics")).toHaveAttribute("aria-pressed", "false");
  await expect(rowSwitch(viewer, "P2:lyrics")).toHaveAttribute("aria-pressed", "true");
  await viewer.context().close();
});

test("takes in both modes open on lyrics, and the switch shows the solfège ones", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.vocals.push(
    { key: "P1:lyrics", label: "Soprano", mode: "lyrics" },
    { key: "P2:lyrics", label: "Alto", mode: "lyrics" },
    { key: "P1:solfege", label: "Soprano (solfège)", mode: "solfege" },
  );
  await openSongAsOwner(page, backend, "share-modes");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  const sungIn = viewer.getByRole("group", { name: "Sung in" });
  await expect(sungIn.getByRole("button", { name: "Lyrics" })).toHaveAttribute("aria-pressed", "true");
  await expect(viewer.getByTestId(/^share-row-/)).toHaveCount(2);
  await expect(row(viewer, "P1:solfege")).toHaveCount(0);
  await sungIn.getByRole("button", { name: "Solfège" }).click();
  await expect(viewer.getByTestId(/^share-row-/)).toHaveCount(1);
  await expect(row(viewer, "P1:solfege")).toBeVisible();

  backend.state.vocals.splice(2, 1);
  await viewer.reload();
  await expect(viewer.getByTestId(/^share-row-/)).toHaveCount(2);
  await expect(viewer.getByRole("group", { name: "Sung in" })).toHaveCount(0);
  await viewer.context().close();
});

test("instruments: the soundfont loads only on the first Play with an instrument on", async ({ page, browser }) => {
  const backend = await createBackend();
  backend.state.noticeAccepted = true;
  backend.state.instruments = true;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-instruments");
  const link = await shareFromTheApp(page);

  const viewer = await openAsViewer(browser, backend, link);
  await expect(row(viewer, "inst-P3")).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Play" })).toBeEnabled();
  await viewer.waitForTimeout(500);
  expect(backend.soundfontRequests()).toBe(0);
  await viewer.getByRole("button", { name: "Play" }).click();
  await expect.poll(() => backend.soundfontRequests()).toBeGreaterThan(0);
  await expect(viewer.getByRole("button", { name: "Pause" })).toBeVisible({ timeout: 20_000 });
  await viewer.context().close();
});

test("a link that was turned off, and too many requests, each say so", async ({ browser, baseURL }) => {
  const backend = await createBackend();
  const viewer = await openAsViewer(browser, backend, `${baseURL}/s/tok0000000000000000099`);
  await expect(viewer.getByText("This link was turned off")).toBeVisible();
  backend.state.pageAnswer = { status: 429, body: { code: "rate_limited", message: "Too many requests." } };
  await viewer.reload();
  await expect(viewer.getByText("Too many requests. Try again in a minute.")).toBeVisible();
  await viewer.context().close();
});

test("Share stays visible and disabled when sharing isn't available to this user", async ({ page }) => {
  const backend = await createBackend();
  backend.state.available = false;
  backend.state.vocals.push({ key: "P1:lyrics", label: "Soprano", mode: "lyrics" });
  await openSongAsOwner(page, backend, "share-not-available");
  await expect(page.getByTestId("score-tracks-panel")).toBeVisible();
  await expect(page.getByTestId("share-song")).toBeVisible();
  await expect(page.getByTestId("share-song")).toBeDisabled();
  await page.getByTestId("my-songs").click();
  const drawer = page.getByRole("dialog", { name: "My songs" });
  await expect(drawer.getByTestId("song-row")).toHaveCount(1);
  await expect(drawer.getByRole("button", { name: "Share" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Share" })).toBeDisabled();
});
