import { expect, test, type Page } from "@playwright/test";

import { signInAsE2EUser } from "../auth";

// After a score with several parts or verses is loaded, the first plain reply
// offers a part/verse picker. A reply carrying a quote has already settled the
// part and verse, so it never gets the picker and removes one shown earlier.

const SESSION_ID = "selection-picker-session";

type Reply = { message: string; selectionResolved?: boolean };

async function mockBackend(page: Page): Promise<{ reply: (next: Reply) => void }> {
  let next: Reply | null = null;
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
      currentScore = body.slice(body.indexOf("<?xml"), body.indexOf("</score-partwise>") + "</score-partwise>".length);
      return json({
        session_id: SESSION_ID,
        score_id: "score-1",
        parsed: true,
        current_score: { version: 1 },
        score_summary: {
          duration_seconds: 13.5,
          available_verses: ["1"],
          parts: [
            { part_id: "Alto", part_index: 0, part_name: "Alto", has_lyrics: true },
            { part_id: "Men", part_index: 1, part_name: "Men", has_lyrics: true },
          ],
        },
        performance_midi: null,
      });
    }
    if (route_.endsWith("/score")) return route.fulfill({ contentType: "application/xml", body: currentScore });
    if (route_.endsWith("/synthesis-estimate")) return route.fulfill({ status: 503, json: {} });
    if (route_.endsWith("/chat")) {
      if (!next) throw new Error("No reply queued for this chat turn.");
      const reply = next;
      next = null;
      return json({
        type: "chat_text",
        message: reply.message,
        ...(reply.selectionResolved ? { selection_resolved: true } : {}),
      });
    }
    return json({});
  });
  return { reply: (reply) => { next = reply; } };
}

async function loadHappyBirthday(page: Page) {
  await page.locator(".composer-toolbar").getByRole("button", { name: "Add a score" }).click();
  await page.getByRole("menu", { name: "Add a score" }).getByRole("menuitem", { name: /Happy Birthday/ }).click();
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
}

async function send(page: Page, message: string) {
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("send-message").click();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

const picker = (page: Page) => page.locator(".selection-panel");

test("a quote reply settles the part and verse, so the picker is not offered", async ({ page }) => {
  test.setTimeout(120_000);
  await page.addInitScript(() => {
    localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true");
    localStorage.setItem("sightsinger.solfege-guide-dismissed", "true");
  });
  const backend = await mockBackend(page);
  await signInAsE2EUser(page, "selection-picker");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  // A quote first: no picker, and none on a later plain reply.
  await loadHappyBirthday(page);
  backend.reply({ message: "Here is the quote for the Alto part.", selectionResolved: true });
  await send(page, "sing for Sarah");
  await expect(page.getByText("Here is the quote for the Alto part.")).toBeVisible();
  await expect(picker(page)).toHaveCount(0);
  backend.reply({ message: "The song is 13.5 seconds long." });
  await send(page, "how long is it?");
  await expect(page.getByText("The song is 13.5 seconds long.")).toBeVisible();
  await expect(picker(page)).toHaveCount(0);

  // Loading the score again asks again: a plain reply gets the picker, and a
  // quote afterwards removes it.
  await loadHappyBirthday(page);
  backend.reply({ message: "Hello! Which part should I sing?" });
  await send(page, "hi");
  await expect(picker(page)).toHaveCount(1);
  await expect(page.getByTestId("part-selection").locator("option")).toHaveText(["Alto", "Men"]);
  backend.reply({ message: "Here is the quote for the Men part.", selectionResolved: true });
  await send(page, "sing the men's part");
  await expect(page.getByText("Here is the quote for the Men part.")).toBeVisible();
  await expect(picker(page)).toHaveCount(0);
});
