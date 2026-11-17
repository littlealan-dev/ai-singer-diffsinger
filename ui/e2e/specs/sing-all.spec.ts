import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// The Sing All card, on a mocked backend that answers as the API contract in
// docs/specs/sing-all-core-step3-spec.md says: the chat reply that opens it, the
// quote, "Sing it", the running job and its end, and a reload.

type Json = Record<string, unknown>;
type Answer = { status?: number; body: Json; hold?: Promise<void> };
type Backend = {
  quotes: Json[];
  starts: Json[];
  nextQuote: (answer: (choices: Json) => Answer) => void;
  nextStart: (answer: Answer) => void;
  setJob: (job: Json | null, { active }?: { active?: boolean }) => void;
  history: Json[];
};

const SONG = "song-1";

const ROWS = [
  { rowId: "P1", sourcePartId: "P1", partName: "Soprano", partId: "P1", name: "Soprano", split: false,
    hasWords: true, note: null, include: true, voice: "voice-a", voicePicked: "recommended",
    pitchRange: { lowest_note: "C4", highest_note: "G5" } },
  { rowId: "P2", sourcePartId: "P2", partName: "Alto", partId: "P2", name: "Alto", split: false,
    hasWords: true, note: null, include: true, voice: "voice-b", voicePicked: "recommended",
    pitchRange: { lowest_note: "G3", highest_note: "C5" } },
];

/** A quote for the given choices, priced as the contract's lines: 1 credit per take. */
function quoteFor(choices: Json, overrides: Json = {}): Json {
  const picks = new Map(((choices.rows as Json[] | undefined) ?? []).map((row) => [row.rowId, row]));
  const rows = ROWS.map((row) => ({ ...row, ...(picks.get(row.rowId) ?? {}) }));
  const sung = rows.filter((row) => row.include).length;
  const note = typeof choices.styleNote === "string" ? choices.styleNote : "";
  const total = sung + 1;
  return {
    quoteId: `quote-${Math.random().toString(36).slice(2)}`,
    blocked: null,
    rows,
    voices: [{ id: "voice-a", name: "Voice A" }, { id: "voice-b", name: "Voice B" }],
    options: {
      language: choices.language ?? "en",
      languages: [{ code: "en", label: null }, { code: "ja", label: null }],
      solfegeOnly: false,
      mode: choices.mode ?? "lyrics",
      verse: choices.verse ?? "1",
      verses: ["1", "2"],
      repeats: choices.repeats ?? true,
      repeatsAvailable: true,
    },
    lines: [
      { kind: "singing", parts: sung, takes: sung, secondsEach: 30, credits: sung },
      { kind: "instrumentals", credits: 1 },
      { kind: "total", credits: total },
      { kind: "balance", before: 42, after: 42 - total },
    ],
    total,
    balance: 42,
    notes: [],
    style: note
      ? { note, controls: {}, changes: { P1: { airiness: 10 } } }
      : { note: "", controls: {}, changes: {} },
    choicesHash: "hash",
    ...overrides,
  };
}

function progress(overrides: Json = {}): Json {
  return {
    jobId: "job-1",
    songId: SONG,
    title: "Test Hymn",
    status: "running",
    stage: "reading",
    endReason: null,
    cancelRequested: false,
    choices: { language: "en", solfegeOnly: false, mode: "lyrics", verse: "1", repeats: true },
    counts: { splits: { done: 0, total: 0 }, takes: { done: 0, failed: 0, total: 2 } },
    rows: [
      { rowId: "P1", name: "Soprano", voice: "voice-a", voiceName: "Voice A", split: false,
        takes: [{ mode: "lyrics", status: "queued", reason: null }] },
      { rowId: "P2", name: "Alto", voice: "voice-b", voiceName: "Voice B", split: false,
        takes: [{ mode: "lyrics", status: "queued", reason: null }] },
    ],
    credits: { reserved: 3, charged: 0, refunded: 0 },
    receipt: [],
    notes: [],
    elapsedS: 20,
    remainingS: 180,
    render: { state: "none", placeInLine: null },
    createdAt: "2026-01-01T10:00:00Z",
    endedAt: null,
    ...overrides,
  };
}

async function mockBackend(page: Page): Promise<Backend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const quoteAnswers: Array<(choices: Json) => Answer> = [];
  const startAnswers: Answer[] = [];
  const backend: Backend = {
    quotes: [],
    starts: [],
    nextQuote: (answer) => quoteAnswers.push(answer),
    nextStart: (answer) => startAnswers.push(answer),
    setJob: (next, { active = true } = {}) => {
      job = next;
      jobActive = active;
    },
    history: [],
  };
  let job: Json | null = null;
  let jobActive = false;
  const summary = {
    duration_seconds: 30,
    available_verses: ["1", "2"],
    parts: [
      { part_id: "P1", part_index: 0, part_name: "Soprano", has_lyrics: true },
      { part_id: "P2", part_index: 1, part_name: "Alto", has_lyrics: true },
    ],
    performance_midi: null,
  };

  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    const at = url.pathname;
    if (at === "/credits") {
      return json({ balance: 42, reserved: 0, available: 42, is_expired: false, overdrafted: false });
    }
    if (at === "/readyz") return json({ ready: true, status: "ready" });
    if (at === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (at === "/api/voicebanks") return json({ voicebanks: [{ id: "voice-a", name: "Voice A" }] });
    if (at === "/songs") return json({ songs: [] });
    if (at === "/sessions" && request.method() === "POST") return json({ session_id: "song-new" });

    if (at === "/sing-all/quote") {
      const body = request.postDataJSON() as { songId: string; choices: Json };
      backend.quotes.push(body.choices);
      const answer = (quoteAnswers.shift() ?? ((choices: Json) => ({ body: quoteFor(choices) })))(body.choices);
      if (answer.hold) await answer.hold;
      return json(answer.body, answer.status ?? 200);
    }
    if (at === "/sing-all/jobs") {
      backend.starts.push(request.postDataJSON());
      const answer = startAnswers.shift() ?? { body: progress() };
      if ((answer.status ?? 200) === 200) backend.setJob(answer.body);
      return json(answer.body, answer.status ?? 200);
    }
    if (at === "/sing-all/active") return json({ job: jobActive ? job : null });
    if (at.startsWith("/sing-all/jobs/")) return job ? json(job) : json({ detail: { code: "job_not_found" } }, 404);

    if (at === `/songs/${SONG}/workspace`) {
      return json({
        session_id: SONG,
        song: { id: SONG, title: "Test Hymn", title_edited: false, created_at: "2026-01-01T00:00:00Z" },
        score: {
          score_id: "score-1",
          current_score: { version: 1 },
          score_summary: summary,
          solfege_settings: { system: "movable_do", mode: "major", revision: 1 },
        },
        messages: backend.history,
        jobs: [],
        player: { score_id: "score-1", revision: 0, repeats: false, vocals: [], instruments: [],
          bus: { muted: false, solo: false, level_db: 0 } },
      });
    }
    if (at.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: xml });
    if (at.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (at.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (at.endsWith("/stop")) {
      return json({ result: "stopped", jobs: [], singAll: { jobId: "job-1", result: "stopping" } });
    }
    if (at.endsWith("/chat")) {
      const message = String(request.postDataJSON()?.message ?? "");
      if (message !== "Sing All") throw new Error(`Unexpected chat message: ${message}`);
      return json({
        type: "chat_text",
        message: "Here's the quote for singing every part.",
        singAll: { language: "en", solfegeOnly: false },
      });
    }
    return json({});
  });
  return backend;
}

async function openSong(page: Page, testId: string) {
  const backend = await mockBackend(page);
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = ".announcement-overlay { display: none !important; }";
      document.head.append(style);
    });
  });
  await signInAsE2EUser(page, testId);
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  return backend;
}

async function goToSong(page: Page) {
  await page.goto(`/app/song/${SONG}`);
  await expect(page.getByTestId("song-title")).toHaveText("Test Hymn");
}

const card = (page: Page) => page.getByTestId("sing-all-card");

async function openCard(page: Page) {
  await page.getByTestId("sing-all-button").click();
  await expect(page.getByTestId("chat-stream")).toContainText("Sing All");
  await expect(card(page)).toBeVisible();
}

test("the button opens the card; Customise asks again and Apply sends the style note", async ({ page }) => {
  const backend = await openSong(page, "sing-all-quote");
  await goToSong(page);
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  backend.nextQuote((choices) => ({ body: quoteFor(choices), hold: held }));
  await openCard(page);

  await expect(card(page)).toContainText("Reading the score…");
  expect(backend.quotes[0]).toEqual({ language: "en", solfegeOnly: false });
  release();
  await expect(card(page)).toContainText("2 vocal parts · English · verse 1 of 2 · with repeats · 0:30");
  await expect(card(page)).toContainText("Voices: Soprano · Voice A, Alto · Voice B");
  await expect(page.getByTestId("sing-all-sing-it")).toHaveText("Sing it · 3 cr");

  await card(page).getByRole("button", { name: /Customise/ }).click();
  await expect(page.getByTestId("sing-all-sing-it")).toHaveCount(0);
  await card(page).getByRole("checkbox").nth(1).uncheck();
  await expect.poll(() => backend.quotes.length).toBe(2);
  expect(backend.quotes[1]).toMatchObject({
    rows: [
      { rowId: "P1", include: true, voice: "voice-a", voicePicked: "recommended" },
      { rowId: "P2", include: false, voice: "voice-b", voicePicked: "recommended" },
    ],
  });
  expect(backend.quotes[1].styleNote).toBeUndefined();
  await expect(card(page)).toContainText("Total 2 cr");

  await card(page).getByRole("combobox", { name: "Soprano voice" }).selectOption("voice-b");
  await expect.poll(() => backend.quotes.length).toBe(3);
  expect((backend.quotes[2].rows as Json[])[0]).toMatchObject({ voice: "voice-b", voicePicked: "user" });

  await card(page).getByRole("textbox", { name: "Style notes" }).fill("Warm and soft");
  expect(backend.quotes).toHaveLength(3);
  await card(page).getByRole("button", { name: "Apply" }).click();
  await expect.poll(() => backend.quotes.length).toBe(4);
  expect(backend.quotes[3].styleNote).toBe("Warm and soft");
  await expect(card(page)).toContainText("Style: “Warm and soft”");
  await expect(card(page)).toContainText("Soprano · Voice B · airiness +10");
  await expect(page.getByTestId("sing-all-sing-it")).toHaveText("Sing it · 2 cr");
});

test("a quote that couldn't be made can be tried again", async ({ page }) => {
  const backend = await openSong(page, "sing-all-quote-failed");
  await goToSong(page);
  backend.nextQuote(() => ({ status: 503, body: { detail: { code: "quote_unavailable" } } }));
  await openCard(page);
  await expect(card(page)).toContainText("The quote couldn't be made.");
  await card(page).getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("sing-all-sing-it")).toHaveText("Sing it · 3 cr");
});

test("a free plan sees Upgrade, and too few credits sees Top up", async ({ page }) => {
  const backend = await openSong(page, "sing-all-blocked");
  await goToSong(page);
  backend.nextQuote((choices) => ({ body: quoteFor(choices, { blocked: "free_plan" }) }));
  await openCard(page);
  await expect(card(page).getByRole("button", { name: "Upgrade to sing" })).toBeVisible();

  // "Both" asks once, and Apply once more.
  for (let index = 0; index < 2; index += 1) {
    backend.nextQuote((choices) => ({ body: quoteFor(choices, { blocked: "not_enough_credits" }) }));
  }
  await card(page).getByRole("button", { name: /Customise/ }).click();
  await card(page).getByRole("button", { name: "Both" }).click();
  await card(page).getByRole("button", { name: "Apply" }).click();
  await expect(card(page).getByRole("button", { name: "Top up" })).toBeVisible();
  await expect(page.getByTestId("sing-all-sing-it")).toHaveCount(0);
});

test("Sing it after the score changed shows the new quote, then the job runs to its receipt", async ({ page }) => {
  const backend = await openSong(page, "sing-all-run");
  await goToSong(page);
  await openCard(page);
  await expect(page.getByTestId("sing-all-sing-it")).toHaveText("Sing it · 3 cr");

  backend.nextStart({ status: 409, body: { detail: { code: "quote_changed" } } });
  await page.getByTestId("sing-all-sing-it").click();
  await expect(card(page)).toContainText("The score changed, so here is the new quote.");
  expect(backend.quotes).toHaveLength(2);

  await page.getByTestId("sing-all-sing-it").click();
  await expect(card(page)).toContainText("Reading the score");
  await expect(card(page)).toContainText("You can close this page.");
  await expect(page.getByTestId("sing-all-pill")).toContainText("Sing All · Reading the score");
  expect(backend.starts).toHaveLength(2);

  backend.setJob(progress({
    stage: "singing",
    counts: { splits: { done: 0, total: 0 }, takes: { done: 1, failed: 0, total: 2 } },
  }));
  await expect(card(page)).toContainText("Singing (1 of 2)");
  await expect(page.getByTestId("sing-all-pill")).toContainText("Singing 1/2");

  backend.setJob(progress({
    status: "completed",
    stage: "done",
    endedAt: "2026-01-01T10:05:00Z",
    remainingS: null,
    counts: { splits: { done: 0, total: 0 }, takes: { done: 2, failed: 0, total: 2 } },
    credits: { reserved: 0, charged: 3, refunded: 0 },
    receipt: [
      { itemId: "take:P1:lyrics", type: "take", rowId: "P1", mode: "lyrics", name: "Soprano", credits: 1,
        outcome: "charged", reason: null },
      { itemId: "take:P2:lyrics", type: "take", rowId: "P2", mode: "lyrics", name: "Alto", credits: 1,
        outcome: "charged", reason: null },
      { itemId: "instrumental", type: "instrumental", rowId: null, mode: null, name: null, credits: 1,
        outcome: "charged", reason: null },
    ],
  }), { active: false });
  await expect(card(page)).toContainText("Done: every part is sung");
  await expect(card(page).locator(".sing-all-receipt")).toContainText("Instrumentals");
  await expect(card(page).locator(".sing-all-receipt")).toContainText("Charged3 cr");
  await expect(page.getByTestId("sing-all-pill")).toHaveCount(0);
});

test("Stop asks first, then asks the Song to stop", async ({ page }) => {
  const backend = await openSong(page, "sing-all-stop");
  await goToSong(page);
  await openCard(page);
  await page.getByTestId("sing-all-sing-it").click();
  await expect(card(page)).toContainText("Reading the score");

  await card(page).getByRole("button", { name: "Stop" }).click();
  await expect(card(page)).toContainText("Stop Sing All? Parts already sung are kept and charged.");
  const stopRequest = page.waitForRequest((request) => request.url().endsWith(`/sessions/${SONG}/stop`));
  backend.setJob(progress({ cancelRequested: true }));
  await card(page).getByRole("alertdialog").getByRole("button", { name: "Stop" }).click();
  await stopRequest;
  await expect(card(page)).toContainText("Stopping…");
});

test("a reload shows the card again from the Song's history", async ({ page }) => {
  const backend = await openSong(page, "sing-all-reload");
  backend.history.push(
    { id: "turn-1", role: "user", content: "Sing All", timestamp: "2026-01-01T10:00:00Z" },
    { id: "turn-2", role: "assistant", content: "Sing All started: 2 parts, 3 credits held.",
      timestamp: "2026-01-01T10:00:01Z", singAllJobId: "job-1" },
    { id: "turn-3", role: "assistant", content: "Sing All finished: 1 of 2 parts sung.",
      timestamp: "2026-01-01T10:05:00Z", singAllJobId: "job-1" },
  );
  backend.setJob(progress({
    status: "partial",
    stage: "done",
    endReason: null,
    remainingS: null,
    endedAt: "2026-01-01T10:05:00Z",
    counts: { splits: { done: 0, total: 0 }, takes: { done: 1, failed: 1, total: 2 } },
    credits: { reserved: 0, charged: 2, refunded: 1 },
    receipt: [
      { itemId: "take:P1:lyrics", type: "take", rowId: "P1", mode: "lyrics", name: "Soprano", credits: 1,
        outcome: "charged", reason: null },
      { itemId: "take:P2:lyrics", type: "take", rowId: "P2", mode: "lyrics", name: "Alto", credits: 1,
        outcome: "refunded", reason: "take_failed" },
      { itemId: "instrumental", type: "instrumental", rowId: null, mode: null, name: null, credits: 1,
        outcome: "charged", reason: null },
    ],
  }), { active: false });
  await goToSong(page);
  await expect(card(page)).toHaveCount(1);
  await expect(card(page)).toContainText("Done, but some parts couldn't be sung");
  await expect(card(page).locator(".sing-all-receipt")).toContainText("1 cr refunded");
});

test("from another Song the pill opens the running job's Song", async ({ page }) => {
  const backend = await openSong(page, "sing-all-pill");
  backend.setJob(progress({ songId: "song-other", title: "Other Song", stage: "splitting",
    counts: { splits: { done: 1, total: 2 }, takes: { done: 0, failed: 0, total: 4 } } }));
  await page.reload();
  await goToSong(page);
  const pill = page.getByTestId("sing-all-pill");
  await expect(pill).toContainText("Sing All · Splitting 1/2");
  await expect(page.getByTestId("sing-all-button")).toBeEnabled();
  await pill.click();
  await expect(page).toHaveURL(/\/app\/song\/song-other$/);
});
