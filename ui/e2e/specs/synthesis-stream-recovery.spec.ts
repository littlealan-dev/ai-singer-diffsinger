import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";


test("stream-error performs bounded recovery and unlocks chat", async ({ page }) => {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let progressRequests = 0;

  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (route.request().method() === "OPTIONS") {
      return route.fulfill({ status: 204 });
    }
    if (url.pathname === "/credits") {
      return json({
        balance: 100,
        reserved: 0,
        available: 100,
        is_expired: false,
        overdrafted: false,
      });
    }
    if (url.pathname === "/readyz") {
      return json({ ready: true, status: "ready" });
    }
    if (url.pathname === "/maintenance/status") {
      return json({ enabled: false, allowed: true });
    }
    if (url.pathname === "/api/voicebanks") {
      return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    }
    if (url.pathname === "/sessions") {
      return json({ session_id: "stream-recovery" });
    }
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return json({
        session_id: "stream-recovery",
        score_id: "score-stream-recovery",
        parsed: true,
      });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/chat")) {
      const progressUrl =
        "/sessions/stream-recovery/progress?job_id=job-stream-recovery";
      const body = [
        "event: accepted",
        `data: ${JSON.stringify({
          type: "chat_progress",
          message: "Preparing take",
          job_id: "job-stream-recovery",
          progress_url: progressUrl,
        })}`,
        "",
        "event: stream-error",
        `data: ${JSON.stringify({
          status: "running",
          job_id: "job-stream-recovery",
          message: "We couldn’t confirm the final job status. Checking for updates…",
        })}`,
        "",
      ].join("\n");
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body,
      });
    }
    if (url.pathname.endsWith("/progress")) {
      progressRequests += 1;
      return json({
        status: "running",
        job_id: "job-stream-recovery",
        score_id: "score-stream-recovery",
        step: "render",
        message: "Rendering.",
        progress: 0.5,
      });
    }
    return json({});
  });

  await signInAsE2EUser(page, "synthesis-stream-recovery");
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();

  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();

  await expect(page.getByRole("alert")).toContainText(
    "We couldn’t confirm the final job status"
  );
  await expect.poll(() => progressRequests).toBe(3);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
  await page.waitForTimeout(1500);
  expect(progressRequests).toBe(3);
});

const MIDI_SESSION_ID = "stream-midi";
const MIDI_SCORE_ID = "score-stream-midi";
const MIDI_JOB_ID = "job-stream-midi";
const MIDI_PROGRESS_URL = `/sessions/${MIDI_SESSION_ID}/progress?job_id=${MIDI_JOB_ID}`;

// The job publishes MIDI for the score's instrumental parts; the upload itself
// carries none, so a /instrumental-midi fetch can only follow a job update.
const PUBLISHED_PERFORMANCE_MIDI = {
  version: 1,
  instrumental_parts: [],
  has_instrumental_parts: true,
  original_midi_available: true,
  expanded_midi_available: true,
};

// Header plus one empty track: the smallest file a MIDI parser accepts.
const EMPTY_MIDI = Buffer.from([
  0x4d, 0x54, 0x68, 0x64, 0x00, 0x00, 0x00, 0x06, 0x00, 0x00, 0x00, 0x01, 0x01, 0xe0,
  0x4d, 0x54, 0x72, 0x6b, 0x00, 0x00, 0x00, 0x04, 0x00, 0xff, 0x2f, 0x00,
]);

type MockedJobBackend = {
  progressRequests: () => number;
  midiRequests: () => number;
  estimateRequests: () => number;
};

function sseBody(events: Array<[string, unknown]>): string {
  return events
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n`)
    .join("\n") + "\n";
}

function jobPayload(status: string, extra: Record<string, unknown> = {}) {
  return {
    status,
    job_id: MIDI_JOB_ID,
    score_id: MIDI_SCORE_ID,
    job_kind: "synthesis",
    step: status === "done" ? "done" : "render",
    message: status === "done" ? "Your take is ready." : "Rendering.",
    progress: status === "done" ? 1 : 0.5,
    ...extra,
  };
}

async function mockJobBackend(
  page: Page,
  chatEvents: Array<[string, unknown]>,
  progressPayload: () => Record<string, unknown>
): Promise<MockedJobBackend> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  let progressRequests = 0;
  let midiRequests = 0;
  let estimateRequests = 0;
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({ json: body });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204 });
    if (url.pathname === "/credits") {
      return json({ balance: 100, reserved: 0, available: 100, is_expired: false, overdrafted: false });
    }
    if (url.pathname === "/readyz") return json({ ready: true, status: "ready" });
    if (url.pathname === "/maintenance/status") return json({ enabled: false, allowed: true });
    if (url.pathname === "/api/voicebanks") {
      return json({ voicebanks: [{ id: "test", name: "Test Voice" }] });
    }
    if (url.pathname === "/sessions") return json({ session_id: MIDI_SESSION_ID });
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return json({
        session_id: MIDI_SESSION_ID,
        score_id: MIDI_SCORE_ID,
        parsed: true,
        score_summary: {
          duration_seconds: 8,
          parts: [{ part_id: "P1", part_index: 0, part_name: "Solo", has_lyrics: true }],
        },
      });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/synthesis-estimate")) {
      estimateRequests += 1;
      return json({
        synthesis_credit_estimate: {
          pricing_version: 1,
          vocal_part_id: "P1",
          expand_repeats: true,
          vocal_duration_seconds: 8,
          vocal_part: { pricing_unit_seconds: 30, estimated_credits: 1 },
          instrumentals: {
            has_instrumental_parts: false,
            charge_required: false,
            pricing_unit_seconds: 120,
            estimated_credits: 0,
            charged_once_for_all_tracks: true,
          },
          billing_components: ["vocal"],
          total_estimated_credits: 1,
        },
      });
    }
    if (url.pathname.endsWith("/instrumental-midi")) {
      midiRequests += 1;
      return route.fulfill({ contentType: "audio/midi", body: EMPTY_MIDI });
    }
    if (url.pathname.endsWith("/chat")) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBody(chatEvents),
      });
    }
    if (url.pathname.endsWith("/progress")) {
      progressRequests += 1;
      return json(progressPayload());
    }
    return json({});
  });
  return {
    progressRequests: () => progressRequests,
    midiRequests: () => midiRequests,
    estimateRequests: () => estimateRequests,
  };
}

async function uploadAndSing(page: Page, testId: string): Promise<void> {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  await signInAsE2EUser(page, testId);
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  // The cookie banner covers the composer, including the Send button.
  const declineCookies = page.getByRole("button", { name: "Decline" });
  await declineCookies.waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  if (await declineCookies.isVisible()) await declineCookies.click();
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();
}

const ACCEPTED_EVENT: [string, unknown] = [
  "accepted",
  {
    type: "chat_progress",
    message: "Preparing take",
    job_id: MIDI_JOB_ID,
    progress_url: MIDI_PROGRESS_URL,
  },
];

test("stream completion loads the MIDI the job published", async ({ page }) => {
  const backend = await mockJobBackend(
    page,
    [
      ACCEPTED_EVENT,
      ["progress", jobPayload("running")],
      [
        "completed",
        jobPayload("done", {
          performance_midi: PUBLISHED_PERFORMANCE_MIDI,
          performance_midi_published: true,
        }),
      ],
    ],
    () => jobPayload("running")
  );

  await uploadAndSing(page, "synthesis-stream-midi");

  await expect.poll(backend.midiRequests).toBe(1);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await page.waitForTimeout(1500);
  // The stream alone delivered the result: no polling fallback ran.
  expect(backend.progressRequests()).toBe(0);
  expect(backend.midiRequests()).toBe(1);
});

test("polling fallback loads the MIDI after the stream drops", async ({ page }) => {
  // The stream ends after acceptance with the job still running, which is
  // what a dropped connection looks like to the client.
  const backend = await mockJobBackend(
    page,
    [ACCEPTED_EVENT, ["progress", jobPayload("running")]],
    () =>
      jobPayload("done", {
        performance_midi: PUBLISHED_PERFORMANCE_MIDI,
        performance_midi_published: true,
      })
  );

  await uploadAndSing(page, "synthesis-stream-midi-fallback");

  await expect.poll(backend.progressRequests).toBeGreaterThan(0);
  await expect.poll(backend.midiRequests).toBe(1);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await page.waitForTimeout(1500);
  expect(backend.midiRequests()).toBe(1);
});

test("unconfirmed job status refreshes the estimate but leaves MIDI alone", async ({ page }) => {
  const backend = await mockJobBackend(
    page,
    [
      ACCEPTED_EVENT,
      [
        "stream-error",
        {
          status: "running",
          job_id: MIDI_JOB_ID,
          message: "We couldn’t confirm the final job status. Checking for updates…",
        },
      ],
    ],
    () => jobPayload("running")
  );

  await uploadAndSing(page, "synthesis-stream-unconfirmed");
  await expect.poll(backend.estimateRequests).toBeGreaterThan(0);
  const estimatesBeforeRecovery = backend.estimateRequests();

  await expect.poll(backend.progressRequests).toBe(3);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect.poll(backend.estimateRequests).toBe(estimatesBeforeRecovery + 1);
  expect(backend.midiRequests()).toBe(0);
});
