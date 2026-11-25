import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

const API = "http://127.0.0.1:8000";
const TOKEN = "fictional-share-secret-1";
const SONG_ID = "fictional-song-id";
const SONG_TITLE = "Private Analytics Test Song";
const JOB_ID = "fictional-job-id";
const MEASUREMENT_ID = "G-E2ETEST";

type Args = [string, string, Record<string, unknown>?];

const stubURL = "/analytics-blank?utm_source=test";

async function probeAnalytics(page: Page, granted = false) {
  await page.addInitScript((consented) => {
    if (consented) localStorage.setItem("analytics_consent", "granted");
    const calls: unknown[][] = [];
    const probe = (...args: unknown[]) => calls.push(args);
    Object.assign(window, { gtag: probe, analyticsProbe: { calls, probe } });
  }, granted);

  await page.route("**/src/firebase.ts*", async (route) => {
    // The managed regression runner leaves the Analytics measurement ID unset.
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /measurementId: import\.meta\.env\.VITE_FIREBASE_MEASUREMENT_ID/,
      `measurementId: "${MEASUREMENT_ID}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.route("https://firebase.googleapis.com/**/webConfig", async (route) => {
    const appId = /\/apps\/(.+)\/webConfig/.exec(new URL(route.request().url()).pathname)?.[1];
    await route.fulfill({ json: { appId, measurementId: MEASUREMENT_ID } });
  });
  await page.route("https://firebaseinstallations.googleapis.com/**", (route) =>
    route.fulfill({
      json: {
        fid: "c123456789012345678901",
        refreshToken: "test-refresh-token",
        authToken: { token: "test-installation-token", expiresIn: "3600s" },
      },
    }),
  );
  await page.route("**/googletagmanager.com/**", (route) =>
    route.fulfill({ contentType: "application/javascript", body: "" }),
  );
  await page.route("**/google-analytics.com/**", (route) => route.fulfill({ status: 204 }));
}

async function captured(page: Page): Promise<Args[]> {
  return page.evaluate(() => (window as unknown as { analyticsProbe: { calls: Args[] } }).analyticsProbe.calls);
}

async function events(page: Page, name: string) {
  return (await captured(page)).filter(([kind, event]) => kind === "event" && event === name)
    .map(([, , params]) => {
      // Firebase's own metadata is outside the application event contract.
      const { origin: _origin, send_to: _sendTo, ...eventParams } = params ?? {};
      return eventParams;
    });
}

async function blankPage(page: Page) {
  await page.route("**/analytics-blank?*", (route) =>
    route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Analytics test</title>" }),
  );
  await page.goto(stubURL);
}

test("analytics ranges match all specified boundaries", async ({ page }) => {
  await blankPage(page);
  const ranges = await page.evaluate(async () => {
    const { countRange, creditsRange, durationRange } = await import("/src/analytics.ts");
    return {
      counts: [0, 1, 2, 4, 5, 8, 9, 16, 17].map(countRange),
      credits: [0, 1, 5, 6, 10, 11, 20, 21, 50, 51, 100, 101, 2.5].map(creditsRange),
      seconds: [0, 59, 60, 299, 300, 899, 900, 3599, 3600].map(durationRange),
    };
  });
  expect(ranges).toEqual({
    counts: ["0", "1", "2-4", "2-4", "5-8", "5-8", "9-16", "9-16", "17+"],
    credits: ["0", "1-5", "1-5", "6-10", "6-10", "11-20", "11-20",
      "21-50", "21-50", "51-100", "51-100", "101+", "1-5"],
    seconds: ["<1m", "<1m", "1-5m", "1-5m", "5-15m", "5-15m", "15-60m", "15-60m", "60m+"],
  });
});

for (const consent of ["missing", "denied", "throwing"] as const) {
  test(`analytics remains uninitialized when consent is ${consent}`, async ({ page }) => {
    await probeAnalytics(page);
    await blankPage(page);
    const wasInitialized = await page.evaluate(async (state) => {
      if (state === "denied") localStorage.setItem("analytics_consent", "denied");
      const firebase = await import("/src/firebase.ts");
      if (state === "throwing") {
        const read = Storage.prototype.getItem;
        Storage.prototype.getItem = function(key) {
          if (key === "analytics_consent") throw new DOMException("Read blocked", "SecurityError");
          return read.call(this, key);
        };
      }
      firebase.logAnalyticsEvent("existing_event", {});
      firebase.logPageView();
      const win = window as unknown as { gtag: unknown; analyticsProbe: { probe: unknown } };
      return win.gtag !== win.analyticsProbe.probe;
    }, consent);
    expect(wasInitialized).toBe(false);
    expect(await captured(page)).toEqual([]);
  });
}

function silentWav(seconds: number): Buffer {
  const size = 8000 * seconds * 2;
  const result = Buffer.alloc(44 + size);
  result.write("RIFF");
  result.writeUInt32LE(36 + size, 4);
  result.write("WAVEfmt ", 8);
  result.writeUInt32LE(16, 16);
  result.writeUInt16LE(1, 20);
  result.writeUInt16LE(1, 22);
  result.writeUInt32LE(8000, 24);
  result.writeUInt32LE(16000, 28);
  result.writeUInt16LE(2, 32);
  result.writeUInt16LE(16, 34);
  result.write("data", 36);
  result.writeUInt32LE(size, 40);
  return result;
}

async function mockShare(page: Page, isOwner = false) {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const audioRequests: URL[] = [];
  const base = `/public/s/${TOKEN}`;
  await page.route(`${API}/**`, (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `${base}/score`) return route.fulfill({ contentType: "application/xml", body: xml });
    if (url.pathname === `${base}/audio`) {
      audioRequests.push(url);
      return route.fulfill({ contentType: "audio/wav", body: silentWav(12) });
    }
    if (url.pathname !== base) return route.fulfill({ status: 404, json: {} });
    return route.fulfill({
      json: {
        title: SONG_TITLE, is_owner: isOwner, song_id: isOwner ? SONG_ID : null,
        score: { version: 1, url: `${base}/score`, summary: { duration_seconds: 12, performance_midi: null } },
        player: {
          repeats: false, instruments: [], midi_url: null,
          vocals: ["lyrics", "solfege"].map((mode) => ({
            key: `P1:${mode}`, label: "Private part", voice_name: "Private voice", mode, duration_seconds: 12,
            muted: false, solo: false, volume: 1, audio_url: `${base}/audio?track=${mode}`,
          })),
          bus: { muted: false, solo: false, level_db: 0 },
        },
        info: {
          title: SONG_TITLE, subtitle: "Private subtitle", composer: "Private composer",
          lyricist: "", arranger: "", credits: "", score_copyright: "", producer: "", description: "",
          license: "cc_by_4", voices: ["Private voice"],
        },
        credits: { voices: ["Private voice"] },
      },
    });
  });
  return audioRequests;
}

test("Accept enables feature events on a shared Song without sending its URL or title", async ({ page }) => {
  await probeAnalytics(page);
  await mockShare(page);
  await page.goto(`/s/${TOKEN}?utm_source=test&song=${SONG_ID}&job=${JOB_ID}`);
  await expect(page.getByRole("heading", { name: SONG_TITLE })).toBeVisible();
  expect(await captured(page)).toEqual([]);
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await page.getByRole("button", { name: "Song info", exact: true }).click();
  await expect.poll(() => events(page, "share_control_use")).toEqual([{ control: "info_fold", env: "dev" }]);
  const calls = await captured(page);
  const config = calls.find(([kind]) => kind === "config")?.[2];
  expect(config).toMatchObject({ page_location: new URL("/s", page.url()).href, page_title: "SightSinger" });
  const serialized = JSON.stringify(calls);
  for (const privateValue of [TOKEN, SONG_ID, SONG_TITLE, JOB_ID, "Private composer", "Private voice"]) {
    expect(serialized).not.toContain(privateValue);
  }
});

test("Share tracks only one play, one of each listen mark, and one first-track request", async ({ page }) => {
  await probeAnalytics(page, true);
  const audio = await mockShare(page);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(`/s/${TOKEN}`);
  await expect.poll(() => events(page, "share_page_view")).toEqual([{
    signed_in: false, is_owner: false, layout: "laptop", parts: "2-4",
    both_modes: true, has_instruments: false, info_fields: 2, license: "cc_by_4", env: "dev",
  }]);
  expect(audio).toEqual([]);

  await page.getByRole("button", { name: "Solfège", exact: true }).click();
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  const seek = page.getByRole("slider", { name: "Seek playback position" });
  await seek.focus();
  await seek.press("End");
  await seek.press("Home");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await seek.press("End");

  await expect.poll(() => events(page, "share_listen_progress")).toEqual(
    [25, 50, 75, 100].map((percent) => ({ percent, env: "dev" })),
  );
  expect(await events(page, "share_play")).toEqual([{
    mode: "solfege", parts_on: "1", instruments_on: false, env: "dev",
  }]);
  expect((await events(page, "share_control_use")).map(({ control }) => control)).toEqual(["mode_switch", "seek"]);
  expect(audio).toHaveLength(2);
  expect(audio.filter((request) => request.searchParams.get("first") === "1")
    .map((request) => request.searchParams.get("track"))).toEqual(["solfege"]);
});

test("the owner never sends the play-count query flag", async ({ page }) => {
  const audio = await mockShare(page, true);
  await page.goto(`/s/${TOKEN}`);
  const play = page.getByRole("button", { name: "Play", exact: true });
  await expect(play).toBeEnabled();
  expect(audio).toEqual([]);
  await play.click();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  expect(audio).toHaveLength(2);
  expect(audio.every((request) => !request.searchParams.has("first"))).toBe(true);
});

async function mountSingAllHarness(page: Page) {
  await page.route("**/src/App.tsx", async (route) => {
    const response = await route.fetch();
    const transformed = await response.text();
    const runtime = /from "([^"]*react_jsx-dev-runtime\.js[^"]*)"/.exec(transformed)?.[1];
    if (!runtime) throw new Error("Vite did not provide the React JSX runtime.");
    const body = `
      import runtime from ${JSON.stringify(runtime)};
      import SingAllCard from "/src/components/SingAllCard.tsx";
      const noop = () => {};
      export default function Harness() {
        return runtime.jsxDEV(SingAllCard, {
          songId: ${JSON.stringify(SONG_ID)}, songTitle: ${JSON.stringify(SONG_TITLE)},
          open: { language: "en", solfegeOnly: false }, planKey: "free", demoSong: true,
          onStarted: noop, onLoadJob: noop, onOpenSong: noop, onTopUp: noop, onUpgrade: noop, onStop: noop
        }, undefined, false);
      }
    `;
    await route.fulfill({ contentType: "application/javascript", body });
  });

  let quotes = 0;
  await page.route(`${API}/sing-all/**`, (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (new URL(request.url()).pathname === "/sing-all/jobs") {
      return route.fulfill({ json: { jobId: JOB_ID, credits: { reserved: 3 } } });
    }
    const choices = request.postDataJSON().choices;
    const mode = choices.mode ?? "lyrics";
    const takes = mode === "both" ? 2 : 1;
    return route.fulfill({
      json: {
        quoteId: `fixture-quote-${++quotes}`, choicesHash: "test-hash", total: 3, balance: 10,
        blocked: null, notes: [], style: { note: "", controls: {}, changes: {} },
        voices: [{ id: "private-voice", name: "Private voice" }],
        rows: [{ rowId: "P1", sourcePartId: "P1", name: "Private part", partName: "Private part",
          split: false, hasWords: true, include: true, voice: "private-voice", voicePicked: "recommended",
          note: null, pitchRange: {} }],
        options: {
          language: "en", languages: [{ code: "en", label: "English" }], mode, verse: "1",
          verses: ["1"], repeats: false, repeatsAvailable: false, mixes: "none", mixFiles: { all: 0, full: 0 },
        },
        lines: [{ kind: "singing", parts: 1, takes, credits: 3, secondsEach: 90 },
          { kind: "total", credits: 3 }, { kind: "balance", before: 10, after: 7 }],
      },
    });
  });
  await page.goto(`/app/song/${SONG_ID}?job=${JOB_ID}`);
}

test("Sing All reports the card once, each displayed quote once, and a started job once", async ({ page }) => {
  await probeAnalytics(page, true);
  await mountSingAllHarness(page);
  const card = page.getByTestId("sing-all-card");
  await expect(card.getByTestId("sing-all-sing-it")).toBeEnabled();
  await card.getByRole("button", { name: /Customise/ }).click();
  await card.getByRole("button", { name: "Both", exact: true }).click();
  await card.getByRole("button", { name: "Apply", exact: true }).click();
  await card.getByTestId("sing-all-sing-it").click();
  await expect.poll(() => events(page, "sing_all_start")).toHaveLength(1);
  await card.getByTestId("sing-all-sing-it").click();
  expect(await events(page, "sing_all_card_open")).toEqual([{ demo_song: true, plan_key: "free", env: "dev" }]);
  expect(await events(page, "sing_all_quote_changed")).toEqual([{ field: "mode", env: "dev" }]);
  const quotes = await events(page, "sing_all_quote_shown");
  expect(quotes.length).toBeGreaterThanOrEqual(2);
  expect(quotes[0]).toEqual({
    parts: "1", splits: "0", takes: "1", open_staffs: "0", mode: "lyrics", mixes: "none",
    repeats: false, credits_range: "1-5", affordable: true, env: "dev",
  });
  expect(quotes.at(-1)).toMatchObject({ mode: "both", takes: "2-4" });
  expect(await events(page, "sing_all_start")).toEqual([{
    parts: "1", splits: "0", takes: "2-4", mode: "both", mixes: "none",
    credits_range: "1-5", voice_changed: false, env: "dev",
  }]);
  const serialized = JSON.stringify(await captured(page));
  for (const privateValue of [SONG_ID, SONG_TITLE, JOB_ID, "Private part", "Private voice", "fixture-quote-"]) {
    expect(serialized).not.toContain(privateValue);
  }
});
