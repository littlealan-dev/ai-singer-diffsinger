import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import path from "node:path";
import { firebaseIdToken, signInAsE2EUser } from "../auth";

// Sing All against the real backend and workers, with the regression LLM.
// Run through e2e/run.mjs; the mocked card tests are in sing-all.spec.ts.

const API_BASE = "http://127.0.0.1:8000";
const FIRESTORE = "http://127.0.0.1:8080/v1/projects/demo-sightsinger-e2e/databases/(default)/documents";
const fixtures = path.resolve(import.meta.dirname, "..", "fixtures");

// A retry would reuse the user, whose Sing All may still be running.
test.describe.configure({ mode: "serial", retries: 0 });

const card = (page: Page) => page.getByTestId("sing-all-card");

async function signInOnAPaidPlan(page: Page, testId: string): Promise<string> {
  await signInAsE2EUser(page, testId);
  // Every GET /credits re-seeds the e2e user on the free plan, which Sing All refuses.
  // The page asks once per load, so move the user to a paid plan after that call.
  const credits = page.waitForResponse((response) => new URL(response.url()).pathname === "/credits");
  await page.reload();
  await credits;
  // The auth emulator gives the user its own uid; read it from the ID token.
  const token = await firebaseIdToken(page);
  const uid: string = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString()).user_id;
  const response = await fetch(
    `${FIRESTORE}/users/${uid}?updateMask.fieldPaths=billing.activePlanKey`,
    {
      method: "PATCH",
      headers: { Authorization: "Bearer owner", "Content-Type": "application/json" },
      body: JSON.stringify({
        fields: { billing: { mapValue: { fields: { activePlanKey: { stringValue: "solo_monthly" } } } } },
      }),
    },
  );
  expect(response.ok, await response.text()).toBeTruthy();
  await expect(page.getByRole("heading", { name: "Studio Chat" })).toBeVisible();
  const declineCookies = page.getByRole("button", { name: "Decline" });
  if (await declineCookies.isVisible()) await declineCookies.click({ force: true });
  const releaseNotes = page.getByRole("button", { name: "Awesome, let's go!" });
  await releaseNotes.waitFor({ state: "visible", timeout: 2_000 }).catch(() => undefined);
  if (await releaseNotes.isVisible()) await releaseNotes.click({ force: true });
  return uid;
}

async function uploadFixture(page: Page, filename: string): Promise<string> {
  const upload = page.waitForResponse(
    (response) => new URL(response.url()).pathname.endsWith("/upload") && response.request().method() === "POST",
  );
  await page.getByTestId("score-upload-input").setInputFiles(path.join(fixtures, filename));
  const response = await upload;
  expect(response.ok()).toBeTruthy();
  const match = new URL(response.url()).pathname.match(/^\/sessions\/([^/]+)\/upload$/);
  expect(match?.[1]).toBeTruthy();
  await expect(page.getByTestId("score-preview-surface").locator("svg").first()).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  return match![1];
}

async function api(page: Page, request: APIRequestContext, pathname: string) {
  const token = await firebaseIdToken(page);
  const response = await request.get(`${API_BASE}${pathname}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.ok(), `${pathname}: ${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json();
}

test("Sing All runs from the chat button to its receipt, with every response in the contract's shape", async (
  { page, request },
) => {
  await signInOnAPaidPlan(page, "sing-all-backend-run");
  const songId = await uploadFixture(page, "sing-all-two-voices-piano.xml");

  // The button's chat message: the regression LLM answers with open_sing_all.
  const chat = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/chat"));
  const firstQuote = page.waitForResponse((response) => new URL(response.url()).pathname === "/sing-all/quote");
  await page.getByTestId("sing-all-button").click();
  const chatBody = await (await chat).json();
  expect(chatBody).toMatchObject({ type: "chat_text", singAll: { language: "en", solfegeOnly: false } });

  const quoteResponse = await firstQuote;
  expect(quoteResponse.status(), await quoteResponse.text()).toBe(200);
  const quote = await quoteResponse.json();
  expect(quote).toMatchObject({ blocked: null, options: { language: "en", mode: "lyrics" } });
  for (const field of ["quoteId", "rows", "voices", "lines", "total", "balance", "notes", "style", "choicesHash"]) {
    expect(quote, field).toHaveProperty(field);
  }
  for (const field of ["languages", "solfegeOnly", "verse", "verses", "repeats", "repeatsAvailable"]) {
    expect(quote.options, field).toHaveProperty(field);
  }
  expect(quote.rows.map((row: { name: string }) => row.name)).toEqual(["Soprano", "Alto"]);
  expect(quote.rows.every((row: { include: boolean }) => row.include)).toBe(true);
  expect(quote.lines.map((line: { kind: string }) => line.kind)).toContain("total");
  expect(quote.voices.length).toBeGreaterThan(0);

  await expect(card(page)).toContainText("2 vocal parts · English");
  const singIt = page.getByTestId("sing-all-sing-it");
  await expect(singIt).toHaveText(`Sing it · ${quote.total} cr`);

  // Customise → a style note → Apply makes the style call and a new quote.
  await card(page).getByRole("button", { name: /Customise/ }).click();
  await card(page).getByRole("textbox", { name: "Style notes" }).fill("Warm and soft");
  const styledQuote = page.waitForResponse((response) => new URL(response.url()).pathname === "/sing-all/quote");
  await card(page).getByRole("button", { name: "Apply" }).click();
  const styled = await (await styledQuote).json();
  expect(styled.style.note).toBe("Warm and soft");
  expect(Object.keys(styled.style.controls).sort()).toEqual(
    styled.rows.map((row: { rowId: string }) => row.rowId).sort(),
  );
  await expect(card(page)).toContainText("Style: “Warm and soft”");

  // Sing it answers the job's Progress.
  const started = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/sing-all/jobs" && response.request().method() === "POST",
  );
  await singIt.click();
  const startedResponse = await started;
  expect(startedResponse.status(), await startedResponse.text()).toBe(200);
  const progress = await startedResponse.json();
  expect(progress).toMatchObject({ songId, status: "running", cancelRequested: false });
  for (const field of ["jobId", "title", "stage", "endReason", "choices", "counts", "rows", "credits",
    "receipt", "notes", "elapsedS", "remainingS", "render", "createdAt", "endedAt"]) {
    expect(progress, field).toHaveProperty(field);
  }
  const jobId: string = progress.jobId;
  await expect(card(page)).toContainText("You can close this page.");
  await expect(page.getByTestId("sing-all-pill")).toContainText("Sing All");

  const active = await api(page, request, "/sing-all/active");
  expect(active.job === null || active.job.jobId === jobId).toBe(true);

  // Two real takes on the synthesis worker.
  await expect(card(page)).toContainText("Done: every part is sung", { timeout: 540_000 });
  await expect(page.getByTestId("sing-all-pill")).toHaveCount(0);
  await expect(card(page).locator(".sing-all-receipt")).toBeVisible();

  const ended = await api(page, request, `/sing-all/jobs/${jobId}`);
  expect(ended).toMatchObject({
    jobId, songId, status: "completed", stage: "done", endReason: null,
    counts: { takes: { done: 2, failed: 0, total: 2 } },
    credits: { reserved: 0, charged: styled.total, refunded: 0 },
    remainingS: null,
  });
  expect(ended.receipt.length).toBeGreaterThan(0);
  expect(ended.rows.map((row: { takes: { status: string }[] }) => row.takes.map((take) => take.status)))
    .toEqual([["done"], ["done"]]);
  expect((await api(page, request, "/sing-all/active")).job).toBeNull();

  // The Song: two lyrics tracks, the history ids, and no Sing All child among its jobs.
  const workspace = await api(page, request, `/songs/${songId}/workspace`);
  const vocals = workspace.player?.vocals ?? [];
  expect(vocals).toHaveLength(2);
  expect(vocals.every((vocal: { mode?: string }) => vocal.mode === "lyrics")).toBe(true);
  expect((workspace.messages ?? []).filter((message: { singAllJobId?: string }) => message.singAllJobId === jobId))
    .not.toHaveLength(0);
  expect(JSON.stringify(workspace.jobs ?? [])).not.toContain(jobId);

  // A reload shows the ended card again from the Song's history.
  await page.reload();
  await expect(card(page)).toContainText("Done: every part is sung");
});
