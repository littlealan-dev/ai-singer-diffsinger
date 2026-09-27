import { expect, test, type Page, type Route } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { signInAsE2EUser } from "../auth";


type StreamScenario = {
  sessionId: string;
  scoreId: string;
  streamEvents: Array<{ event: string; data: Record<string, unknown> }>;
  onProgress: (route: Route) => Promise<void>;
};

const streamBody = (
  events: Array<{ event: string; data: Record<string, unknown> }>
): string =>
  events
    .flatMap(({ event, data }) => [
      `event: ${event}`,
      `data: ${JSON.stringify(data)}`,
      "",
    ])
    .join("\n");

async function installScenarioRoutes(
  page: Page,
  xml: Buffer,
  scenario: StreamScenario
): Promise<void> {
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
      return json({ session_id: scenario.sessionId });
    }
    if (url.pathname.endsWith("/solfege-settings")) {
      return json({ system: "movable_do", mode: "major", revision: 1 });
    }
    if (url.pathname.endsWith("/upload")) {
      return json({
        session_id: scenario.sessionId,
        score_id: scenario.scoreId,
        parsed: true,
      });
    }
    if (url.pathname.endsWith("/score")) {
      return route.fulfill({ contentType: "application/xml", body: xml });
    }
    if (url.pathname.endsWith("/chat")) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: streamBody(scenario.streamEvents),
      });
    }
    if (url.pathname.endsWith("/progress")) {
      return scenario.onProgress(route);
    }
    return json({});
  });
}

async function openUploadedScore(
  page: Page,
  xml: Buffer,
  userId: string
): Promise<void> {
  await signInAsE2EUser(page, userId);
  await page.addStyleTag({ content: ".announcement-overlay { display: none !important; }" });
  await page.getByTestId("score-upload-input").setInputFiles({
    name: "score.xml",
    mimeType: "application/xml",
    buffer: xml,
  });
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
}

async function sendSynthesisRequest(page: Page): Promise<void> {
  await page.getByTestId("chat-input").fill("Sing the solo line");
  await page.getByTestId("send-message").click();
}

async function disableProgressInterval(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const originalSetInterval = window.setInterval.bind(window);
    window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      if (timeout === 1200) {
        return originalSetInterval(() => undefined, 60_000);
      }
      return originalSetInterval(handler, timeout, ...args);
    }) as typeof window.setInterval;
  });
}

const acceptedAndRunningEvents = (
  sessionId: string,
  jobId: string
): StreamScenario["streamEvents"] => {
  const progressUrl = `/sessions/${sessionId}/progress?job_id=${jobId}`;
  return [
    {
      event: "accepted",
      data: {
        type: "chat_progress",
        message: "Preparing take",
        job_id: jobId,
        progress_url: progressUrl,
      },
    },
    {
      event: "progress",
      data: {
        status: "running",
        job_id: jobId,
        step: "render",
        message: "Rendering.",
        progress: 0.5,
      },
    },
  ];
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("sightsinger.multitrack-tutorial-dismissed", "true");
  });
});

test("stream-error performs bounded recovery and unlocks chat", async ({ page }) => {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const sessionId = "stream-recovery";
  const scoreId = "score-stream-recovery";
  const jobId = "job-stream-recovery";
  let progressRequests = 0;

  await installScenarioRoutes(page, xml, {
    sessionId,
    scoreId,
    streamEvents: [
      acceptedAndRunningEvents(sessionId, jobId)[0],
      {
        event: "stream-error",
        data: {
          status: "running",
          job_id: jobId,
          message: "We couldn’t confirm the final job status. Checking for updates…",
        },
      },
    ],
    onProgress: async (route) => {
      progressRequests += 1;
      await route.fulfill({
        json: {
          status: "running",
          job_id: jobId,
          score_id: scoreId,
          step: "render",
          message: "Rendering.",
          progress: 0.5,
        },
      });
    },
  });

  await openUploadedScore(page, xml, "synthesis-stream-recovery");
  await sendSynthesisRequest(page);

  await expect(page.getByRole("alert")).toContainText(
    "We couldn’t confirm the final job status"
  );
  await expect.poll(() => progressRequests).toBe(3);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
  await page.waitForTimeout(1500);
  expect(progressRequests).toBe(3);
});

test("online retries after a Safari-style transient polling failure", async ({ page }) => {
  await disableProgressInterval(page);
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    let progressAttempts = 0;
    (window as typeof window & { __progressAttempts?: number }).__progressAttempts = 0;
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/progress")) {
        progressAttempts += 1;
        (window as typeof window & { __progressAttempts?: number }).__progressAttempts =
          progressAttempts;
        if (progressAttempts === 1) {
          throw new TypeError("Load failed");
        }
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });

  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const sessionId = "stream-safari-recovery";
  const scoreId = "score-stream-safari-recovery";
  const jobId = "job-stream-safari-recovery";
  let progressRequests = 0;

  await installScenarioRoutes(page, xml, {
    sessionId,
    scoreId,
    streamEvents: acceptedAndRunningEvents(sessionId, jobId),
    onProgress: async (route) => {
      progressRequests += 1;
      await route.fulfill({
        json: {
          status: "done",
          job_id: jobId,
          score_id: scoreId,
          step: "done",
          message: "Your take is ready.",
          progress: 1,
        },
      });
    },
  });

  await openUploadedScore(page, xml, "synthesis-stream-safari-recovery");
  await sendSynthesisRequest(page);

  await expect.poll(() =>
    page.evaluate(
      () => (window as typeof window & { __progressAttempts?: number }).__progressAttempts
    )
  ).toBe(1);
  await expect(page.getByRole("alert")).toContainText(
    "Connection lost. Reconnecting to check your take"
  );
  expect(progressRequests).toBe(0);

  await page.evaluate(() => window.dispatchEvent(new Event("online")));

  await expect.poll(() => progressRequests).toBe(1);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("online aborts a stalled progress poll before retrying", async ({ page }) => {
  await disableProgressInterval(page);
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    let progressAttempts = 0;
    (window as typeof window & { __progressAttempts?: number }).__progressAttempts = 0;
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/progress")) {
        progressAttempts += 1;
        (window as typeof window & { __progressAttempts?: number }).__progressAttempts =
          progressAttempts;
        if (progressAttempts === 1) {
          return new Promise<Response>((_resolve, reject) => {
            const rejectAbort = () => reject(new DOMException("Aborted", "AbortError"));
            if (init?.signal?.aborted) {
              rejectAbort();
              return;
            }
            init?.signal?.addEventListener("abort", rejectAbort, { once: true });
          });
        }
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });

  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const sessionId = "stream-stalled-recovery";
  const scoreId = "score-stream-stalled-recovery";
  const jobId = "job-stream-stalled-recovery";
  let progressRequests = 0;

  await installScenarioRoutes(page, xml, {
    sessionId,
    scoreId,
    streamEvents: acceptedAndRunningEvents(sessionId, jobId),
    onProgress: async (route) => {
      progressRequests += 1;
      await route.fulfill({
        json: {
          status: "done",
          job_id: jobId,
          score_id: scoreId,
          step: "done",
          message: "Your take is ready.",
          progress: 1,
        },
      });
    },
  });

  await openUploadedScore(page, xml, "synthesis-stream-stalled-recovery");
  await sendSynthesisRequest(page);

  await expect.poll(() =>
    page.evaluate(
      () => (window as typeof window & { __progressAttempts?: number }).__progressAttempts
    )
  ).toBe(1);
  expect(progressRequests).toBe(0);

  await page.evaluate(() => window.dispatchEvent(new Event("online")));

  await expect.poll(() => progressRequests).toBe(1);
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("transient recovery stops after the bounded recovery window", async ({ page }) => {
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/progress")) {
        throw new TypeError("Load failed");
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });

  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const sessionId = "stream-bounded-transient-recovery";
  const scoreId = "score-stream-bounded-transient-recovery";
  const jobId = "job-stream-bounded-transient-recovery";

  await installScenarioRoutes(page, xml, {
    sessionId,
    scoreId,
    streamEvents: acceptedAndRunningEvents(sessionId, jobId),
    onProgress: async (route) => {
      await route.fulfill({
        json: {
          status: "running",
          job_id: jobId,
          score_id: scoreId,
          step: "render",
          message: "Rendering.",
          progress: 0.5,
        },
      });
    },
  });

  await openUploadedScore(page, xml, "synthesis-stream-bounded-transient-recovery");
  await page.clock.install();
  await sendSynthesisRequest(page);

  await expect(page.getByRole("alert")).toContainText(
    "Connection lost. Reconnecting to check your take"
  );
  await page.evaluate(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
  });
  await page.clock.fastForward(3 * 60_000);

  await expect(page.getByRole("alert")).toContainText(
    "We couldn’t confirm the final job status"
  );
  await page.getByTestId("chat-input").fill("Check this job later");
  await expect(page.getByTestId("send-message")).toBeEnabled();
  await expect(page.getByLabel("Processing")).toHaveCount(0);
});

test("healthy progress polling continues beyond the transient recovery window", async ({
  page,
}) => {
  const xml = await readFile(path.resolve("e2e/fixtures/basic-one-part.xml"));
  const sessionId = "stream-healthy-long-recovery";
  const scoreId = "score-stream-healthy-long-recovery";
  const jobId = "job-stream-healthy-long-recovery";
  let progressRequests = 0;

  await installScenarioRoutes(page, xml, {
    sessionId,
    scoreId,
    streamEvents: acceptedAndRunningEvents(sessionId, jobId),
    onProgress: async (route) => {
      progressRequests += 1;
      await route.fulfill({
        json: {
          status: "running",
          job_id: jobId,
          score_id: scoreId,
          step: "render",
          message: "Rendering.",
          progress: 0.5,
        },
      });
    },
  });

  await openUploadedScore(page, xml, "synthesis-stream-healthy-long-recovery");
  await page.clock.install();
  await sendSynthesisRequest(page);

  await expect.poll(() => progressRequests).toBeGreaterThan(0);
  await page.clock.fastForward(3 * 60_000 + 1_000);

  await page.getByTestId("chat-input").fill("Do not send while this job is running");
  await expect(page.getByTestId("send-message")).toBeDisabled();
  await expect(page.getByLabel("Processing")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
