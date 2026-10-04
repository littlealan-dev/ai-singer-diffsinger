import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// A demo song is a score shipped with the app (ui/public/demo-scores), loaded
// through the same upload request as a user's file. While it is the current
// score, the starter prompts name its real parts and verses.

const SESSION_ID = "demo-songs-session";

type Backend = { uploads: () => Array<{ name: string; body: string }> };

async function mockBackend(page: Page): Promise<Backend> {
  const uploads: Array<{ name: string; body: string }> = [];
  let currentScore = "";
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
      const body = request.postDataBuffer()?.toString("utf-8") ?? "";
      const name = /filename="([^"]+)"/.exec(body)?.[1] ?? "";
      const xmlStart = body.indexOf("<?xml");
      const xmlEnd = body.indexOf("</score-partwise>");
      currentScore = body.slice(xmlStart, xmlEnd + "</score-partwise>".length);
      uploads.push({ name, body: currentScore });
      return json({
        session_id: SESSION_ID,
        score_id: `score-${uploads.length}`,
        parsed: true,
        current_score: { version: 1 },
        score_summary: { duration_seconds: 20, parts: [] },
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: currentScore });
    }
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    return json({});
  });
  return { uploads: () => [...uploads] };
}

const starterPrompts = (page: Page) =>
  page.locator(".starting-conversation-button").allTextContents();

test("demo songs load like an upload and bring their own starter prompts", async ({ page }) => {
  test.setTimeout(120_000);
  const backend = await mockBackend(page);
  await signInAsE2EUser(page, "demo-songs");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the toolbar.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  // With no score, the score panel and the chat both offer the two demos.
  const cards = page.locator(".demo-song-card");
  await expect(cards).toHaveCount(2);
  await expect(page.locator(".demo-song-link")).toHaveText(["Amazing Grace", "Happy Birthday"]);
  expect(await starterPrompts(page)).toContain("sing the vocal part, verse 1");

  // A card uploads the shipped file, and the starter prompts follow the song.
  await cards.filter({ hasText: "Amazing Grace" }).click();
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
  expect(backend.uploads().map((upload) => upload.name)).toEqual(["amazing-grace.xml"]);
  expect(backend.uploads()[0].body).toContain("<work-title>Amazing Grace</work-title>");
  expect(backend.uploads()[0].body).toContain('<miscellaneous-field name="sightsinger-demo">amazing-grace</miscellaneous-field>');
  await expect.poll(() => starterPrompts(page)).toEqual([
    "sing the soprano part, verse 1",
    "sing the alto part in solfege",
    "sing the bass part, verse 2",
  ]);

  // The "+" menu offers the demos too.
  await page.locator(".composer-toolbar").getByRole("button", { name: "Add a score" }).click();
  const menu = page.getByRole("menu", { name: "Add a score" });
  await expect(menu.getByRole("menuitem")).toHaveCount(3);
  await menu.getByRole("menuitem", { name: /Happy Birthday/ }).click();
  await expect(menu).toHaveCount(0);
  await expect.poll(() => backend.uploads().map((upload) => upload.name)).toEqual([
    "amazing-grace.xml",
    "happy-birthday.xml",
  ]);
  expect(backend.uploads()[1].body).toContain('<miscellaneous-field name="sightsinger-demo">happy-birthday</miscellaneous-field>');
  await expect.poll(() => starterPrompts(page)).toEqual([
    "sing the alto part",
    "sing the men's part",
    "sing the alto part in solfege",
  ]);

  // The user's own file brings back the generic prompts.
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "my-score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect.poll(() => backend.uploads().map((upload) => upload.name)).toEqual([
    "amazing-grace.xml",
    "happy-birthday.xml",
    "my-score.xml",
  ]);
  await expect.poll(() => starterPrompts(page)).toContain("sing the vocal part, verse 1");
});
