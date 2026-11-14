import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { signInAsE2EUser } from "../auth";
import { getE2EState } from "../support";

// The Songs flows against the real local backend: regression planner, real synthesis, storage on.
const fixture = path.resolve(import.meta.dirname, "..", "fixtures", "basic-one-part.xml");
test.describe.configure({ mode: "serial" });

const songTitle = (page: Page) => page.getByTestId("song-title");
const chat = (page: Page) => page.getByTestId("chat-stream");
const vocalRows = (page: Page) => page.locator(".score-track-row.vocal-track");
const songRows = (page: Page) => page.getByRole("dialog", { name: "My songs" }).getByTestId("song-row");
const songRow = (page: Page, title: string) => songRows(page).filter({ hasText: title });
const choicePopup = (page: Page) => page.getByRole("dialog", { name: "New song or replace?" });
const songIdOf = (page: Page) => /\/app\/song\/([^/?#]+)/.exec(page.url())?.[1] ?? "";

async function dismissOverlays(page: Page) {
  const decline = page.getByRole("button", { name: "Decline" });
  await decline.waitFor({ state: "visible", timeout: 3_000 }).catch(() => undefined);
  if (await decline.isVisible()) await decline.click({ force: true });
  const notes = page.getByRole("button", { name: "Awesome, let's go!" });
  await notes.waitFor({ state: "visible", timeout: 2_000 }).catch(() => undefined);
  if (await notes.isVisible()) await notes.click({ force: true });
  const openMenu = page.locator(".user-menu-button[aria-expanded='true']");
  if (await openMenu.isVisible()) await openMenu.click({ force: true });
}

async function start(page: Page, id: string) {
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = ".announcement-overlay { display: none !important; }";
      document.head.append(style);
    });
  });
  await signInAsE2EUser(page, id);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await dismissOverlays(page);
}

async function chooseScore(page: Page, name: string) {
  const buffer = await readFile(fixture);
  await page.getByTestId("score-upload-input").setInputFiles({ name, mimeType: "application/xml", buffer });
}

async function startSong(page: Page, name: string) {
  await chooseScore(page, name);
  await expect(page).toHaveURL(/\/app\/song\/[0-9a-f]{32}$/, { timeout: 60_000 });
  await expect(songTitle(page)).toHaveText(name.replace(/\.xml$/, ""));
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
}

async function send(page: Page, message: string) {
  await expect(page.getByTestId("chat-input")).toBeEnabled({ timeout: 60_000 });
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("send-message").click();
}

async function openMySongs(page: Page) {
  await page.getByTestId("my-songs").click();
  await expect(page.getByRole("dialog", { name: "My songs" })).toBeVisible();
}

type Player = { scoreId?: string; vocals?: Array<{ sourceJobId?: string; muted?: boolean }> } | null;
async function state(page: Page, request: Parameters<typeof getE2EState>[1], songId: string) {
  return (await getE2EState(page, request, songId)) as Awaited<ReturnType<typeof getE2EState>> & {
    player?: Player;
    score_id?: string;
  };
}

test("Songs without the chat: title, reload, rename, My songs, delete, unknown address", async ({ page }) => {
  await start(page, `songs-real-basics-${Date.now()}`);
  await startSong(page, "Morning Hymn.xml");
  const first = page.url();

  await page.reload();
  await expect(songTitle(page)).toHaveText("Morning Hymn");
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();

  await songTitle(page).click();
  await page.getByRole("textbox", { name: "Song title" }).fill("Hymn practice");
  await page.keyboard.press("Enter");
  await expect(songTitle(page)).toHaveText("Hymn practice");
  await page.reload();
  await expect(songTitle(page)).toHaveText("Hymn practice");

  await page.getByTestId("new-song").click();
  await expect(page).toHaveURL(/\/app$/);
  await expect(songTitle(page)).toHaveCount(0);
  await startSong(page, "Evening Round.xml");
  const second = page.url();
  expect(second).not.toBe(first);

  await openMySongs(page);
  await expect(songRows(page).locator(".songs-drawer-row-title")).toHaveText(["Evening Round", "Hymn practice"]);
  await expect(songRow(page, "Hymn practice")).toContainText("No sung parts yet");
  await songRow(page, "Hymn practice").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(first);
  await expect(songTitle(page)).toHaveText("Hymn practice");

  await openMySongs(page);
  await songRow(page, "Evening Round").getByRole("button", { name: "Delete" }).click();
  await songRow(page, "Evening Round").getByRole("button", { name: "Delete" }).click();
  await expect(songRows(page)).toHaveCount(1);
  await songRow(page, "Hymn practice").getByRole("button", { name: "Delete" }).click();
  await songRow(page, "Hymn practice").getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/\/app$/);
  await openMySongs(page);
  await expect(page.getByRole("dialog", { name: "My songs" })).toContainText("No songs yet.");

  await page.goto(second);
  await expect(page.getByRole("alert")).toContainText("This song isn't available");
  await page.goto("/app/song/does-not-exist");
  await expect(page.getByRole("alert")).toContainText("This song isn't available");
});

test("A real take: player record, reload, mix, leaving mid-take, Replace during a take", async ({ page, request }) => {
  test.setTimeout(1_500_000);
  await start(page, `songs-real-take-${Date.now()}`);
  await startSong(page, "Morning Hymn.xml");
  const songId = songIdOf(page);
  const songAddress = page.url();

  // 1. A take sung by the real workers lands in the player and in the server's record.
  await send(page, "[e2e:basic] prepare this fixture");
  await expect(chat(page)).toContainText("ready to quote", { timeout: 60_000 });
  await send(page, "[e2e:confirm] Start the quoted synthesis.");
  await expect(vocalRows(page)).toHaveCount(1, { timeout: 540_000 });
  await expect.poll(async () => (await state(page, request, songId)).player?.vocals?.length ?? 0, {
    timeout: 30_000,
  }).toBe(1);
  const firstTake = (await state(page, request, songId)).player!.vocals![0].sourceJobId;

  // 2. The mix is saved; a reload brings back chat, track and mix.
  await vocalRows(page).first().locator(".score-track-mute-btn").click();
  await page.waitForTimeout(2_000);
  expect((await state(page, request, songId)).player?.vocals?.[0]?.muted).toBe(true);
  await page.reload();
  await expect(songTitle(page)).toHaveText("Morning Hymn");
  await expect(chat(page)).toContainText("[e2e:confirm] Start the quoted synthesis.");
  await expect(page.getByTestId("synthesis-audio").first()).toBeVisible({ timeout: 30_000 });
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(vocalRows(page).first().locator(".score-track-mute-btn")).toHaveAttribute("aria-pressed", "true");

  // 3. Leaving while a take sings: it finishes on the server and is there on return.
  await send(page, "[e2e:confirm] Start the quoted synthesis.");
  await expect(page.getByTestId("stop-work")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("new-song")).toBeEnabled({ timeout: 60_000 });
  await page.getByTestId("new-song").click();
  await expect(page).toHaveURL(/\/app$/);
  await openMySongs(page);
  const workingRow = songRow(page, "Morning Hymn");
  await expect(workingRow).toContainText("Working…");
  await page.getByRole("button", { name: "Close My songs" }).click();
  await expect.poll(async () => (await state(page, request, songId)).job?.status ?? "pending", {
    timeout: 540_000,
  }).toMatch(/^(completed|failed)$/);
  const away = await state(page, request, songId);
  expect(away.job?.status).toBe("completed");
  expect(away.player?.vocals?.[0]?.sourceJobId).toBe(away.job?.id);
  expect(away.player?.vocals?.[0]?.muted).toBe(true);  // the second take of the part keeps its mix
  await openMySongs(page);
  await expect(songRow(page, "Morning Hymn")).toContainText("1 sung part");
  await songRow(page, "Morning Hymn").getByRole("button", { name: "Open" }).click();
  await expect(page).toHaveURL(songAddress);
  await expect(vocalRows(page)).toHaveCount(1);
  await expect(page.getByTestId("synthesis-audio")).toHaveCount(2, { timeout: 30_000 });
  await expect(page.getByTestId("stop-work")).toHaveCount(0);

  // 4. Replace while a take sings: stopped and replaced, or too late and kept.
  await send(page, "[e2e:confirm] Start the quoted synthesis.");
  await expect(page.getByTestId("stop-work")).toBeVisible({ timeout: 60_000 });
  // The input is disabled until the reply (the take's "accepted") arrives.
  await expect(page.getByTestId("score-upload-input")).toBeEnabled({ timeout: 60_000 });
  await chooseScore(page, "Spring Canon.xml");
  await expect(choicePopup(page)).toBeVisible({ timeout: 30_000 });
  const working = (await choicePopup(page).textContent())?.includes("the running work is stopped");
  await choicePopup(page).getByRole("button", { name: /^Replace/ }).click();
  const tooLate = page.getByText("The current job is just finishing and can't be stopped.");
  await expect(songTitle(page).filter({ hasText: "Spring Canon" }).or(tooLate)).toBeVisible({ timeout: 120_000 });
  if (await tooLate.isVisible()) {
    await expect(songTitle(page)).toHaveText("Morning Hymn");
  } else {
    await expect(vocalRows(page)).toHaveCount(0);
    // Live, the take's bubble says "Stopped."; the refund line is in the saved chat.
    if (working) await expect(chat(page)).toContainText("Stopped.");
    const after = await state(page, request, songId);
    expect(after.player?.vocals ?? []).toEqual([]);
    expect(after.player?.scoreId).toBe(after.score_id);
    await page.reload();
    await expect(songTitle(page)).toHaveText("Spring Canon");
    await expect(vocalRows(page)).toHaveCount(0);
    if (working) await expect(chat(page)).toContainText("Stopped the take");
    await expect(page.getByTestId("stop-work")).toHaveCount(0);
  }
});
