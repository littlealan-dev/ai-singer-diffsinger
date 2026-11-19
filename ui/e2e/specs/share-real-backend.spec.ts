import { expect, test, type Browser, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { signInAsE2EUser } from "../auth";
import { chooseReplace, getE2EState } from "../support";

// Share against the real local backend (SHARE_LINKS=on in the harness): a real
// take, its link, the public page signed out, the owner and another user, a
// live mix change, turning off, a new link, and a Replace.
const fixture = path.resolve(import.meta.dirname, "..", "fixtures", "basic-one-part.xml");

const songTitle = (page: Page) => page.getByTestId("song-title");
const chat = (page: Page) => page.getByTestId("chat-stream");
const vocalRows = (page: Page) => page.locator(".score-track-row.vocal-track");
const shareBox = (page: Page) => page.getByRole("dialog", { name: "Share this song" });
const shareLink = (page: Page) => shareBox(page).getByRole("textbox", { name: "Share link" });
const songIdOf = (page: Page) => /\/app\/song\/([^/?#]+)/.exec(page.url())?.[1] ?? "";

async function dismissOverlays(page: Page) {
  const decline = page.getByRole("button", { name: "Decline" });
  await decline.waitFor({ state: "visible", timeout: 3_000 }).catch(() => undefined);
  if (await decline.isVisible()) await decline.click({ force: true });
  const notes = page.getByRole("button", { name: "Awesome, let's go!" });
  await notes.waitFor({ state: "visible", timeout: 2_000 }).catch(() => undefined);
  if (await notes.isVisible()) await notes.click({ force: true });
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

async function upload(page: Page, name: string) {
  const buffer = await readFile(fixture);
  await page.getByTestId("score-upload-input").setInputFiles({ name, mimeType: "application/xml", buffer });
}

async function send(page: Page, message: string) {
  await expect(page.getByTestId("chat-input")).toBeEnabled({ timeout: 60_000 });
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("send-message").click();
}

/** A page in a browser context of its own: signed out, or signed in as another user. */
async function openElsewhere(browser: Browser, baseURL: string, link: string, signInAs?: string) {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  const privateRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.port === "8000" && /^\/(sessions|songs)\b/.test(url.pathname)) privateRequests.push(url.pathname);
  });
  if (signInAs) await start(page, signInAs);
  privateRequests.length = 0;
  await page.goto(link);
  await dismissOverlays(page);
  return { page, privateRequests };
}

const turnedOff = (page: Page) => page.getByText("This link was turned off");

test("a real take shared: the page, the owner, a live mix, turning off, a new link and a Replace", async ({
  page, request, browser, baseURL,
}) => {
  test.setTimeout(1_200_000);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const run = Date.now();
  await start(page, `share-real-owner-${run}`);
  await upload(page, "Shared Hymn.xml");
  await expect(page).toHaveURL(/\/app\/song\/[0-9a-f]{32}$/, { timeout: 60_000 });
  await expect(songTitle(page)).toHaveText("Shared Hymn");
  const songId = songIdOf(page);
  await expect(page.getByTestId("share-song")).toHaveCount(0);  // nothing sung yet

  await send(page, "[e2e:basic] prepare this fixture");
  await expect(chat(page)).toContainText("ready to quote", { timeout: 60_000 });
  await send(page, "[e2e:confirm] Start the quoted synthesis.");
  await expect(vocalRows(page)).toHaveCount(1, { timeout: 540_000 });

  // 1. The first Share shows the notice, then the link; Copy copies it.
  await page.getByTestId("share-song").click();
  const notice = page.getByRole("dialog", { name: "Before you share" });
  await notice.getByRole("button", { name: "Share" }).click();
  await expect(shareLink(page)).toHaveValue(new RegExp(`^${baseURL}/s/[A-Za-z0-9_-]{22}$`));
  const first = await shareLink(page).inputValue();
  await shareBox(page).getByRole("button", { name: "Copy" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(first);
  await page.keyboard.press("Escape");

  // 2. Signed out: the title, the score, one row, and Play plays; no private endpoint is called.
  const viewer = await openElsewhere(browser, baseURL!, first);
  await expect(viewer.page.getByRole("heading", { name: "Shared Hymn" })).toBeVisible();
  await expect(viewer.page.locator(".share-song-score svg").first()).toBeVisible({ timeout: 30_000 });
  await expect(viewer.page.getByTestId(/^share-row-/)).toHaveCount(1);
  await expect(viewer.page.getByRole("link", { name: "Open in SightSinger" })).toHaveCount(0);
  await viewer.page.getByRole("button", { name: "Play" }).click();
  await expect(viewer.page.getByRole("button", { name: "Pause" })).toBeVisible({ timeout: 30_000 });
  await viewer.page.getByRole("button", { name: "Stop" }).click();
  expect(viewer.privateRequests).toEqual([]);

  // 3. Another signed-in user is a viewer too; the owner gets "Open in SightSinger".
  const other = await openElsewhere(browser, baseURL!, first, `share-real-other-${run}`);
  await expect(other.page.getByRole("heading", { name: "Shared Hymn" })).toBeVisible();
  await expect(other.page.getByRole("link", { name: "Open in SightSinger" })).toHaveCount(0);
  await other.page.context().close();
  const ownerView = await page.context().newPage();
  await ownerView.goto(first);
  await ownerView.getByRole("link", { name: "Open in SightSinger" }).click();
  await expect(ownerView).toHaveURL(`${baseURL}/app/song/${songId}`);
  await ownerView.close();

  // 4. Live: the owner mutes the part, and a reload of the page starts it off.
  const rowSwitch = viewer.page.getByTestId(/^share-row-/).getByRole("button");
  await expect(rowSwitch).toHaveAttribute("aria-pressed", "true");
  await vocalRows(page).first().locator(".score-track-mute-btn").click();
  await expect.poll(async () => {
    const state = (await getE2EState(page, request, songId)) as { player?: { vocals?: Array<{ muted?: boolean }> } };
    return state.player?.vocals?.[0]?.muted;
  }, { timeout: 15_000 }).toBe(true);
  await viewer.page.reload();
  await expect(rowSwitch).toHaveAttribute("aria-pressed", "false");

  // 5. Turn off: the link stops, and My songs loses the mark.
  await page.getByTestId("share-song").click();
  await expect(shareLink(page)).toHaveValue(first);
  await shareBox(page).getByRole("button", { name: "Turn off sharing" }).click();
  await shareBox(page).getByRole("button", { name: "Turn off" }).click();
  await expect(shareBox(page)).toHaveCount(0);
  await viewer.page.reload();
  await expect(turnedOff(viewer.page)).toBeVisible();
  await page.getByTestId("my-songs").click();
  const songRow = page.getByRole("dialog", { name: "My songs" }).getByTestId("song-row").filter({ hasText: "Shared Hymn" });
  await expect(songRow).toBeVisible();
  await expect(songRow.getByText("Shared", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Close My songs" }).click();

  // 6. Sharing again makes a new link, with no notice; the old one stays off.
  await page.getByTestId("share-song").click();
  await expect(shareLink(page)).toHaveValue(/\/s\//);
  const second = await shareLink(page).inputValue();
  expect(second).not.toBe(first);
  await page.keyboard.press("Escape");
  await viewer.page.goto(second);
  await expect(viewer.page.getByRole("heading", { name: "Shared Hymn" })).toBeVisible();
  await viewer.page.goto(first);
  await expect(turnedOff(viewer.page)).toBeVisible();

  // 7. A Replace: the same link shows the new score, and nothing sung yet.
  await upload(page, "Shared Canon.xml");
  await chooseReplace(page);
  await expect(songTitle(page)).toHaveText("Shared Canon", { timeout: 60_000 });
  await expect(vocalRows(page)).toHaveCount(0);
  await viewer.page.goto(second);
  await expect(viewer.page.getByRole("heading", { name: "Shared Canon" })).toBeVisible();
  await expect(viewer.page.getByText("No sung parts yet")).toBeVisible();
  await expect(viewer.page.locator(".share-song-score svg").first()).toBeVisible({ timeout: 30_000 });
  expect(viewer.privateRequests).toEqual([]);
  await viewer.page.context().close();
});
