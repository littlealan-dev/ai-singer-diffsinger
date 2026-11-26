import { expect, test, type Page } from "@playwright/test";

import { signInAsE2EUser } from "../auth";

// The user menu's Help item sends "Help" in the chat; the chat model's reply asks what to help with.

const REPLY = "I can sing parts and explain the app. What would you like help with?";

async function openStudio(page: Page, testId: string, chatReply: () => Promise<void> = async () => {}) {
  const chatBodies: Array<Record<string, unknown>> = [];
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
    if (path === "/api/voicebanks") return json({ voicebanks: [{ id: "made-up-voice", name: "Made-up Voice" }] });
    if (path === "/sessions") return json({ session_id: "help-session" });
    if (path.endsWith("/chat")) {
      chatBodies.push(request.postDataJSON());
      await chatReply();
      return json({ type: "chat_text", message: REPLY });
    }
    if (path.endsWith("/solfege-settings")) return json({ system: "movable_do", mode: "major", revision: 1 });
    return json({});
  });
  await signInAsE2EUser(page, testId);
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  return chatBodies;
}

const helpItem = (page: Page) => page.locator(".user-menu-dropdown").getByRole("button", { name: "Help" });

test("Help in the user menu sends Help in the chat and closes the menu", async ({ page }) => {
  const chatBodies = await openStudio(page, "help-menu");
  await page.locator(".user-menu-button").click();
  await helpItem(page).click();
  await expect(page.locator(".user-menu-dropdown")).toHaveCount(0);
  await expect(page.getByTestId("chat-stream")).toContainText(REPLY);
  expect(chatBodies).toHaveLength(1);
  expect(chatBodies[0].message).toBe("Help");
});

test("Help is off while a reply is in progress", async ({ page }) => {
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const chatBodies = await openStudio(page, "help-menu-busy", () => held);
  await page.locator(".user-menu-button").click();
  await helpItem(page).click();
  await page.locator(".user-menu-button").click();
  await expect(helpItem(page)).toBeDisabled();
  release();
  await expect(page.getByTestId("chat-stream")).toContainText(REPLY);
  await expect(helpItem(page)).toBeEnabled();
  expect(chatBodies).toHaveLength(1);
});
