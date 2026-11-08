import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { signInAsE2EUser } from "../auth";

const SESSION_ID = "no-selection-dialog-session";

async function mockBackend(page: Page): Promise<void> {
  let currentScore = "";
  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    const routePath = url.pathname;
    if (routePath === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (routePath === "/readyz") return json({ ready: true, status: "ready" });
    if (routePath === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (routePath === "/api/voicebanks") return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    if (routePath === "/sessions") return json({ session_id: SESSION_ID });
    if (routePath.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    if (routePath.endsWith("/upload")) {
      const body = request.postDataBuffer()?.toString("utf-8") ?? "";
      currentScore = body.slice(body.indexOf("<?xml"), body.indexOf("</score-partwise>") + "</score-partwise>".length);
      return json({
        session_id: SESSION_ID,
        score_id: "score-1",
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: 13.5,
          available_verses: ["1", "2"],
          parts: [
            { part_id: "P1", part_index: 0, part_name: "Solo", has_lyrics: true },
            { part_id: "P2", part_index: 1, part_name: "Duet", has_lyrics: true },
          ],
        },
        performance_midi: null,
      });
    }
    if (routePath.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: currentScore });
    if (routePath.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (routePath.endsWith("/chat")) {
      expect(request.postDataJSON()).not.toHaveProperty("selection");
      return json({ type: "chat_text", message: "Which part and verse would you like to sing?" });
    }
    return json({});
  });
}

test("a plain reply for a two-part two-verse score has no selection dialog", async ({ page }) => {
  test.setTimeout(120_000);
  await mockBackend(page);
  await signInAsE2EUser(page, "no-selection-dialog");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  const fixturePath = path.join(path.dirname(fileURLToPath(import.meta.url)), "../fixtures/two-verses-one-part.xml");
  const firstPartScore = readFileSync(fixturePath, "utf8");
  const firstPart = firstPartScore.match(/<part id="P1">[\s\S]*?<\/part>/)?.[0];
  expect(firstPart).toBeTruthy();
  const score = firstPartScore
    .replace("</part-list>", '<score-part id="P2"><part-name>Duet</part-name></score-part></part-list>')
    .replace("</score-partwise>", `${firstPart?.replace('id="P1"', 'id="P2"')}</score-partwise>`);
  const uploadResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/upload"));
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "two-parts-two-verses.xml",
    mimeType: "application/xml",
    buffer: Buffer.from(score),
  });
  expect((await uploadResponse).ok()).toBeTruthy();
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();

  await page.getByTestId("chat-input").fill("Please sing this score.");
  await page.getByTestId("send-message").click();
  await expect(page.getByText("Which part and verse would you like to sing?")).toBeVisible();
  await expect(page.getByTestId("part-selection")).toHaveCount(0);
  await expect(page.getByTestId("verse-selection")).toHaveCount(0);
  await expect(page.getByTestId("use-selection")).toHaveCount(0);
});
