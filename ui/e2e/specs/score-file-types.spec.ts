import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";

// A score is MusicXML in one of its three file extensions: .musicxml and .xml
// are plain XML (MuseScore and Finale name uncompressed exports .musicxml),
// .mxl is zipped. The file picker offers all three. A drop is not filtered by
// the picker, so the backend names any other file and says what to upload.

const fixture = (name: string) => path.resolve("e2e/fixtures", name);

async function dismissOverlays(page: Page): Promise<void> {
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click({ force: true });
  const releaseNotesDismiss = page.getByRole("button", { name: "Awesome, let's go!" });
  await releaseNotesDismiss.waitFor({ state: "visible", timeout: 2_000 }).catch(() => undefined);
  if (await releaseNotesDismiss.isVisible()) await releaseNotesDismiss.click({ force: true });
  const openUserMenu = page.locator(".user-menu-button[aria-expanded='true']");
  if (await openUserMenu.isVisible()) await openUserMenu.click({ force: true });
}

/** Drops a file on the score panel, as a user dragging it from their desktop. */
async function dropFile(page: Page, name: string, mimeType: string, content: Buffer): Promise<void> {
  const dataTransfer = await page.evaluateHandle(
    ({ fileName, type, bytes }) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(bytes)], fileName, { type }));
      return transfer;
    },
    { fileName: name, type: mimeType, bytes: [...content] },
  );
  await page.getByTestId("score-preview").dispatchEvent("drop", { dataTransfer });
}

function uploadResponse(page: Page) {
  return page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith("/upload") && response.request().method() === "POST",
  );
}

test("a .musicxml score uploads, and a dropped PDF is named with what to upload instead", async ({ page }) => {
  test.setTimeout(180_000);
  await signInAsE2EUser(page, "score-file-types");
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await dismissOverlays(page);

  const input = page.getByTestId("score-upload-input");
  await expect(input).toHaveAttribute("accept", ".musicxml,.xml,.mxl");

  // The picker's .musicxml file is read as plain MusicXML.
  const accepted = uploadResponse(page);
  await input.setInputFiles({
    name: "My Song.musicxml",
    mimeType: "application/vnd.recordare.musicxml+xml",
    buffer: await readFile(fixture("basic-one-part.xml")),
  });
  expect((await accepted).status()).toBe(200);
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();

  // A dropped PDF is refused with its extension and the formats that work.
  const refused = uploadResponse(page);
  await dropFile(page, "My Song.pdf", "application/pdf", Buffer.from("%PDF-1.4\n"));
  expect((await refused).status()).toBe(400);
  const alert = page.locator(".message-box.error");
  await expect(alert).toContainText("SightSinger can't read .pdf files.");
  await expect(alert).toContainText("Upload a MusicXML score (.musicxml, .xml or .mxl).");
  // The refused file leaves the current score in place.
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
});
