import { expect, test, type Page } from "@playwright/test";

import { signInAsE2EUser } from "../auth";

// The one-time tour steps through the studio's controls, and the voice button
// names the selected voice.

const LONG_VOICE = "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)";

async function mockBackend(page: Page): Promise<void> {
  await page.route("**/soundfonts/FluidR3_GM.sf2", (route) => route.fulfill({ status: 404 }));
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown) => route.fulfill({ json: body });
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (path === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (path === "/readyz") return json({ ready: true, status: "ready" });
    if (path === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (path === "/api/voicebanks") return json({ voicebanks: [{ id: "liee", name: LONG_VOICE }] });
    if (path === "/sessions") return json({ session_id: "feature-tour-session" });
    if (path.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    return json({});
  });
}

async function openStudio(page: Page, testId: string): Promise<void> {
  await mockBackend(page);
  // The tour waits until no release announcement is showing. The emulator's
  // rules cannot record a test account dismissing one, and the account's
  // Firestore uid is generated at sign-in, so it cannot be marked seen up
  // front. Set the page's date before the latest announcement takes effect.
  await page.clock.setFixedTime(new Date("2026-08-01T12:00:00Z"));
  await signInAsE2EUser(page, testId, { featureTour: true });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
}

const tour = (page: Page) => page.getByTestId("feature-tour");

async function expectStep(page: Page, count: string, title: string, target: string): Promise<void> {
  await expect(tour(page).locator(".feature-tour-count")).toHaveText(count);
  await expect(tour(page).locator(".feature-tour-title")).toHaveText(title);
  // The control the step points at is highlighted, and only that one.
  await expect(page.locator(".feature-tour-target")).toHaveCount(1);
  await expect(page.locator(`[data-tour="${target}"]`)).toHaveClass(/feature-tour-target/);
}

test("the tour steps through the controls once, with Next, Back and Done", async ({ page }) => {
  test.setTimeout(120_000);
  await openStudio(page, "feature-tour-steps");

  await expectStep(page, "1 / 5", "Solfege", "solfege");
  await expect(tour(page).getByRole("button", { name: "Back" })).toBeDisabled();
  await tour(page).getByRole("button", { name: "Next" }).click();
  await expectStep(page, "2 / 5", "With Repeats", "repeats");
  await tour(page).getByRole("button", { name: "Back" }).click();
  await expectStep(page, "1 / 5", "Solfege", "solfege");
  for (const [count, title, target] of [
    ["2 / 5", "With Repeats", "repeats"],
    ["3 / 5", "Collapse the chat", "chat-collapse"],
    ["4 / 5", "Page or horizontal view", "score-layout"],
    ["5 / 5", "Download the mix", "export-mix"],
  ]) {
    await tour(page).getByRole("button", { name: "Next" }).click();
    await expectStep(page, count, title, target);
  }
  await tour(page).getByRole("button", { name: "Done" }).click();
  await expect(tour(page)).toHaveCount(0);
  await expect(page.locator(".feature-tour-target")).toHaveCount(0);

  // Shown once.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Studio Chat" })).toBeVisible();
  await page.waitForTimeout(1_000);
  await expect(tour(page)).toHaveCount(0);
});

test("Skip ends the tour for good", async ({ page }) => {
  test.setTimeout(120_000);
  await openStudio(page, "feature-tour-skip");
  await expectStep(page, "1 / 5", "Solfege", "solfege");
  await tour(page).getByRole("button", { name: "Skip" }).click();
  await expect(tour(page)).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Studio Chat" })).toBeVisible();
  await page.waitForTimeout(1_000);
  await expect(tour(page)).toHaveCount(0);
});

test("the voice button shows a microphone and the voice name, cut short when too long", async ({ page }) => {
  test.setTimeout(120_000);
  await mockBackend(page);
  await signInAsE2EUser(page, "feature-tour-voice");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();

  const voiceButton = page.locator(".composer-voice-tool");
  await expect(voiceButton.locator(".composer-voice-name")).toHaveText("Auto");
  await expect(voiceButton.locator("svg.lucide-mic")).toHaveCount(1);
  const autoWidth = await voiceButton.evaluate((button) => button.getBoundingClientRect().width);

  await voiceButton.click();
  await page.getByRole("listbox", { name: "Select AI voice" }).getByText(LONG_VOICE).click();

  await expect(voiceButton.locator(".composer-voice-name")).toHaveText(LONG_VOICE);
  const fit = await voiceButton.evaluate((button) => {
    const name = button.querySelector(".composer-voice-name") as HTMLElement;
    return {
      width: button.getBoundingClientRect().width,
      truncated: name.scrollWidth > name.clientWidth,
      inside: name.getBoundingClientRect().right <= button.getBoundingClientRect().right,
      ellipsis: getComputedStyle(name).textOverflow,
    };
  });
  // Fixed width whatever the name; a long one ends in "…" inside the button.
  expect(fit.width).toBe(autoWidth);
  expect(fit).toMatchObject({ truncated: true, inside: true, ellipsis: "ellipsis" });
});
