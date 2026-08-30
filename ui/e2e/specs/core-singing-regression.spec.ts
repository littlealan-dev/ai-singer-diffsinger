import { expect, test, type Page, type Response, type TestInfo } from "@playwright/test";
import { copyFile } from "node:fs/promises";
import path from "node:path";
import { signInAsE2EUser } from "../auth";
import { clearLocalArtifacts, getE2EState, type E2EState } from "../support";

const fixtures = path.resolve(import.meta.dirname, "..", "fixtures");
let sessionId = "";

test.describe.configure({ mode: "serial" });

test.describe("core singing regression", () => {
  test.beforeEach(async ({ page }) => {
    sessionId = "";
    if (test.info().title.includes("plays deferred repeat-expanded piano MIDI")) {
      await page.addInitScript(() => {
        const playback = { audioBufferStarts: 0, oscillatorStarts: 0 };
        Object.defineProperty(window, "__e2ePlayback", { value: playback, configurable: true });
        const audioStart = AudioBufferSourceNode.prototype.start;
        AudioBufferSourceNode.prototype.start = function (...args) {
          playback.audioBufferStarts += 1;
          return audioStart.apply(this, args);
        };
        const oscillatorStart = OscillatorNode.prototype.start;
        OscillatorNode.prototype.start = function (...args) {
          playback.oscillatorStarts += 1;
          return oscillatorStart.apply(this, args);
        };
      });
    }
    await signInAsE2EUser(page, test.info().title.replaceAll(" ", "-"));
    await expect(page.getByTestId("chat-input")).toBeEnabled();
  });

  test("initializes a new account's credits without showing the paywall", async ({ page }) => {
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByText("1000000 Credits", { exact: true })).toBeVisible();
  });

  test("uploads and synthesizes one voice part, one verse", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "basic-one-part.xml");
    await requestScenario(page, "basic");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.part_index).toBe(0);
    expect(state.synthesis?.lyric_selection?.number).toBe("1");
    expect(state.synthesis?.lyric_selection?.name).toBe("");
  });

  test("selects verse 1 from the UI selector and synthesizes it", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "two-verses-one-part.xml");
    await requestScenario(page, "two-verses");

    await expect(page.getByTestId("part-selection").locator("option:checked")).toHaveText("Solo");
    await expect(page.getByTestId("verse-selection").locator("option")).toHaveText(["Verse 1", "Verse 2"]);
    await page.getByTestId("verse-selection").selectOption("1");
    const renderResponse = await clickUseSelection(page);
    expect(renderResponse.type, JSON.stringify(renderResponse)).toBe("chat_progress");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.part_index).toBe(0);
    expect(state.synthesis?.lyric_selection?.number).toBe("1");
    expect(state.synthesis?.lyric_selection?.name).toBe("");
  });

  test("synthesizes verse 1 when the request is typed in chat", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "two-verses-one-part.xml");
    await requestScenario(page, "two-verses");

    await expect(page.getByTestId("verse-selection").locator("option")).toHaveText(["Verse 1", "Verse 2"]);
    const renderResponse = await sendMessage(page, "Please sing the Solo part, verse 1.");
    expect(renderResponse.type, JSON.stringify(renderResponse)).toBe("chat_progress");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.part_index).toBe(0);
    expect(state.synthesis?.lyric_selection?.number).toBe("1");
    expect(state.synthesis?.lyric_selection?.name).toBe("");
  });

  test("plays deferred repeat-expanded piano MIDI alongside synthesized vocals", async ({ page, request }, testInfo) => {
    const updateDepthWarnings: string[] = [];
    page.on("console", (message) => {
      if (/Maximum update depth exceeded/.test(message.text())) {
        updateDepthWarnings.push(message.text());
      }
    });
    await uploadFixture(page, "repeat-piano-accompaniment.xml");
    // Register before the synthesis response is handled by React: the app starts
    // fetching deferred MIDI as soon as it receives the newly persisted metadata.
    const midiResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/instrumental-midi")
    );
    await requestScenario(page, "repeat-piano");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.score_summary?.performance_midi).toMatchObject({
      has_instrumental_parts: true,
      expanded_midi_available: true,
    });
    expect((await midiResponse).ok()).toBeTruthy();

    // Verify per-track controls panel rendering and order
    const tracksPanel = page.getByTestId("score-tracks-panel");
    await expect(tracksPanel).toBeVisible();
    const trackRows = tracksPanel.locator(".score-track-row");
    await expect(trackRows).toHaveCount(2); // 1 vocal track + 1 accompaniment piano track
    await expect(trackRows.nth(0)).toHaveClass(/vocal-track/);
    await expect(trackRows.nth(1)).toHaveClass(/instrument-track/);
    await expect(trackRows.nth(1).locator(".score-track-instrument-select")).toBeVisible();

    // Verify mute / solo toggles and volume control
    const instSoloBtn = trackRows.nth(1).locator(".score-track-solo-btn");
    await expect(instSoloBtn).toHaveText("S");
    await instSoloBtn.click();
    await expect(instSoloBtn).toHaveClass(/active/);
    await instSoloBtn.click();
    await expect(instSoloBtn).not.toHaveClass(/active/);

    const play = page.getByRole("button", { name: "Play score player" });
    await expect(play).toBeEnabled();
    await play.click();

    await expect.poll(async () => page.evaluate(() => (window as any).__e2ePlayback), {
      timeout: 15_000,
    }).toMatchObject({
      audioBufferStarts: expect.any(Number),
      oscillatorStarts: expect.any(Number),
    });
    await expect.poll(async () => page.evaluate(() => (window as any).__e2ePlayback.audioBufferStarts), {
      timeout: 15_000,
    }).toBeGreaterThan(0);
    // The expanded written order is 1-2-1-2-3, so the piano must schedule
    // multiple notes. A one-note MIDI failure cannot satisfy this assertion.
    await expect.poll(async () => page.evaluate(() => (window as any).__e2ePlayback.oscillatorStarts), {
      timeout: 15_000,
    }).toBeGreaterThanOrEqual(5);
    expect(updateDepthWarnings).toEqual([]);
  });

  test("highlights and follows written-order measures in page and horizontal score views", async ({ page, request }, testInfo) => {
    // At the default 1280px test viewport, the responsive header controls
    // overlap the Horizontal button with the score-download control. Exercise
    // the desktop layout that exposes both controls as a user would.
    await page.setViewportSize({ width: 1920, height: 480 });
    await uploadFixture(page, "active-measure-written-order.xml");
    await requestScenario(page, "active-measure-written");
    await waitForAudio(page, request, testInfo);

    // The onboarding menu can receive focus after a synthesis completes in the
    // test shell. Close it before clicking the real score-player transport.
    await page.keyboard.press("Escape");
    await installActiveMeasureObserver(page);
    const canvas = page.locator(".score-canvas");
    await canvas.evaluate((element) => element.scrollTo({ top: 0, left: 0 }));
    await page.getByRole("button", { name: "Play score player" }).click();

    await expect.poll(
      async () => (await getActiveMeasureHistory(page)).some((entry) => entry.sourceMeasureIndex >= 10),
      { timeout: 15_000 },
    ).toBe(true);
    await expect.poll(() => canvas.evaluate((element) => element.scrollTop), { timeout: 15_000 }).toBeGreaterThan(0);

    await page.getByRole("button", { name: "Stop score player" }).click();
    // Make the full staffline wider than the constrained horizontal viewport.
    // This proves the follow behavior rather than merely the cursor mapping.
    for (let index = 0; index < 5; index += 1) {
      await page.getByRole("button", { name: "Zoom in" }).click();
    }
    await page.getByRole("button", { name: "Horizontal" }).click();
    await expect(page.getByRole("button", { name: "Horizontal" })).toHaveAttribute("aria-pressed", "true");
    const horizontalRenderer = page.getByTestId("horizontal-score-incremental-renderer");
    await expect(horizontalRenderer).toBeVisible();
    // Horizontal rendering is one continuous OSMD score. It must not use
    // independent range excerpts, which would redraw clefs at every boundary.
    await expect(horizontalRenderer.locator("svg")).toHaveCount(1);
    // Keep the header at desktop width for the layout switch, then constrain
    // the preview to prove that a later measure is followed horizontally.
    await page.setViewportSize({ width: 600, height: 480 });
    await canvas.evaluate((element) => {
      element.style.flex = "0 0 220px";
      element.style.width = "220px";
    });
    await canvas.evaluate((element) => element.scrollTo({ top: 0, left: 0 }));
    await clearActiveMeasureHistory(page);
    await page.getByRole("button", { name: "Play score player" }).click();

    await expect.poll(
      async () => (await getActiveMeasureHistory(page)).some((entry) => entry.sourceMeasureIndex >= 10),
      { timeout: 15_000 },
    ).toBe(true);
    await expect.poll(() => canvas.evaluate((element) => element.scrollLeft), { timeout: 15_000 }).toBeGreaterThan(0);
  });

  test("returns the active highlight to repeated source measures", async ({ page, request }, testInfo) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await uploadFixture(page, "active-measure-repeat.xml");
    await requestScenario(page, "active-measure-repeat");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.score_summary?.performance_measure_map?.expanded.map((entry) => entry.source_measure_index))
      .toEqual([0, 1, 2, 3, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);

    await page.keyboard.press("Escape");
    await installActiveMeasureObserver(page);
    await page.getByRole("button", { name: "Horizontal" }).click();
    const horizontalRenderer = page.getByTestId("horizontal-score-incremental-renderer");
    await expect(horizontalRenderer).toBeVisible();
    await expect(horizontalRenderer.locator("svg")).toHaveCount(1);
    await page.getByRole("button", { name: "Play score player" }).click();

    await expect.poll(
      async () => {
        const history = await getActiveMeasureHistory(page);
        return ["0:0", "1:1", "2:2", "3:3", "4:0"].every((expected) =>
          history.some((entry) => `${entry.playedMeasureIndex}:${entry.sourceMeasureIndex}` === expected),
        );
      },
      { timeout: 15_000 },
    ).toBe(true);
    await expect.poll(
      async () => (await getActiveMeasureHistory(page)).some((entry) => entry.sourceMeasureIndex >= 8),
      { timeout: 15_000 },
    ).toBe(true);
  });

  test("adds solfege, rehydrates the active artifact, then synthesizes it", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "solfege-source.xml");
    await requestScenario(page, "solfege");
    await waitForDerivedScore(page, request);

    const transformed = await getE2EState(page, request, sessionId);
    expect(transformed.score_version).toBeGreaterThan(1);
    expect(String(transformed.files?.active_musicxml_storage_path ?? "")).not.toBe("");

    await clearLocalArtifacts(page, request, sessionId);
    const renderResponse = await sendMessage(page, "[e2e:render-solfege]");
    expect(renderResponse.type, JSON.stringify(renderResponse)).toBe("chat_progress");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.lyric_selection?.name).toBe("SightSinger Solfege");
    expect(state.score_version).toBe(transformed.score_version);
    expect(state.files?.active_musicxml_storage_path).toBe(transformed.files?.active_musicxml_storage_path);
    expect(String(state.synthesis?.source_musicxml_path ?? "")).toContain("sessions/");
  });

  test("splits two voices on one staff and synthesizes a derived part", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "two-voices-one-staff.xml");
    await requestScenario(page, "split-staff");
    await waitForDerivedScore(page, request, { requirePreprocessJob: true });
    const renderResponse = await sendMessage(page, "[e2e:render-derived]");
    expect(renderResponse.type, JSON.stringify(renderResponse)).toBe("chat_progress");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.part_index).toBe(1);
    expect(state.synthesis?.score_sha256).toMatch(/^[a-f0-9]{64}$/);
  });

  test("splits chord lanes from one part and synthesizes a derived part", async ({ page, request }, testInfo) => {
    await uploadFixture(page, "chord-one-part.xml");
    await requestScenario(page, "split-chords");
    await waitForDerivedScore(page, request, { requirePreprocessJob: true });
    const renderResponse = await sendMessage(page, "[e2e:render-derived]");
    expect(renderResponse.type, JSON.stringify(renderResponse)).toBe("chat_progress");
    const state = await waitForAudio(page, request, testInfo);

    expect(state.synthesis?.part_index).toBe(1);
    expect(state.synthesis?.score_sha256).toMatch(/^[a-f0-9]{64}$/);
  });

  test("re-upload replaces score-specific state in the same authenticated session", async ({ page, request }) => {
    await uploadFixture(page, "basic-one-part.xml");
    await sendMessage(page, "Remember that I will upload a different song next.");
    await expect(page.getByTestId("chat-stream")).toContainText("Remember that I will upload a different song next.");
    const beforeReplacement = await getE2EState(page, request, sessionId);

    await uploadFixture(page, "two-voices-one-staff.xml");
    const replacement = await getE2EState(page, request, sessionId);

    expect(replacement.history_length).toBe(beforeReplacement.history_length);
    expect(beforeReplacement.score_summary?.parts).toHaveLength(1);
    expect(beforeReplacement.score_summary?.parts?.[0]?.part_name).toBe("Solo");
    expect(replacement.score_summary?.parts?.[0]?.part_name).toBe("Soprano Alto");
    await expect(page.getByTestId("chat-stream")).toContainText("Remember that I will upload a different song next.");
    await expect(page.getByTestId("synthesis-audio")).toHaveCount(0);
    await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  });
});

async function uploadFixture(page: Page, filename: string): Promise<void> {
  const uploadResponse = page.waitForResponse((response) => {
    const request = response.request();
    return new URL(response.url()).pathname.endsWith("/upload") && request.method() === "POST";
  });
  await page.getByTestId("score-upload-input").setInputFiles(path.join(fixtures, filename));
  const response = await uploadResponse;
  expect(response.ok()).toBeTruthy();
  setSessionIdFromResponse(response, "upload");
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

async function requestScenario(page: Page, scenario: string): Promise<void> {
  await sendMessage(page, `[e2e:${scenario}] prepare this fixture`);
}

type ActiveMeasureHistoryEntry = {
  playedMeasureIndex: number;
  sourceMeasureIndex: number;
};

async function installActiveMeasureObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    const targetWindow = window as Window & {
      __e2eActiveMeasureHistory?: ActiveMeasureHistoryEntry[];
      __e2eActiveMeasureObserver?: MutationObserver;
    };
    targetWindow.__e2eActiveMeasureObserver?.disconnect();
    targetWindow.__e2eActiveMeasureHistory = [];
    targetWindow.__e2eActiveMeasureObserver = new MutationObserver((records) => {
      for (const record of records) {
        const target = record.target as HTMLElement;
        const playedMeasureIndex = Number(target.dataset.playedMeasureIndex);
        const sourceMeasureIndex = Number(target.dataset.sourceMeasureIndex);
        if (!Number.isFinite(playedMeasureIndex) || !Number.isFinite(sourceMeasureIndex)) continue;
        const history = targetWindow.__e2eActiveMeasureHistory!;
        const previous = history[history.length - 1];
        if (
          previous?.playedMeasureIndex !== playedMeasureIndex ||
          previous.sourceMeasureIndex !== sourceMeasureIndex
        ) {
          history.push({ playedMeasureIndex, sourceMeasureIndex });
        }
      }
    });
    targetWindow.__e2eActiveMeasureObserver.observe(document.body, {
      attributes: true,
      subtree: true,
      attributeFilter: ["data-played-measure-index"],
    });
  });
}

async function clearActiveMeasureHistory(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as Window & { __e2eActiveMeasureHistory?: ActiveMeasureHistoryEntry[] })
      .__e2eActiveMeasureHistory = [];
  });
}

async function getActiveMeasureHistory(page: Page): Promise<ActiveMeasureHistoryEntry[]> {
  return page.evaluate(
    () =>
      (window as Window & { __e2eActiveMeasureHistory?: ActiveMeasureHistoryEntry[] })
        .__e2eActiveMeasureHistory ?? [],
  );
}

async function sendMessage(page: Page, message: string): Promise<Record<string, unknown>> {
  const chatResponse = page.waitForResponse((response) => {
    const request = response.request();
    return new URL(response.url()).pathname.endsWith("/chat") && request.method() === "POST";
  });
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("send-message").click();
  const response = await chatResponse;
  expect(response.ok()).toBeTruthy();
  setSessionIdFromResponse(response, "chat");
  return response.json() as Promise<Record<string, unknown>>;
}

async function clickUseSelection(page: Page): Promise<Record<string, unknown>> {
  const chatResponse = page.waitForResponse((response) => {
    const request = response.request();
    return new URL(response.url()).pathname.endsWith("/chat") && request.method() === "POST";
  });
  await page.getByTestId("use-selection").click();
  const response = await chatResponse;
  expect(response.ok()).toBeTruthy();
  setSessionIdFromResponse(response, "chat");
  return response.json() as Promise<Record<string, unknown>>;
}

function setSessionIdFromResponse(response: Response, operation: "upload" | "chat"): void {
  const match = new URL(response.url()).pathname.match(
    new RegExp(`^/sessions/([^/]+)/${operation}$`),
  );
  expect(match?.[1], `Unable to derive session ID from ${operation} response URL: ${response.url()}`).toBeTruthy();
  sessionId = match![1];
}

async function waitForDerivedScore(
  page: Page,
  request: Parameters<typeof getE2EState>[1],
  options: { requirePreprocessJob?: boolean } = {},
): Promise<void> {
  await expect.poll(async () => {
    const state = await getE2EState(page, request, sessionId);
    return state.score_version ?? 0;
  }, { timeout: 120_000 }).toBeGreaterThan(1);
  if (options.requirePreprocessJob) {
    await expect.poll(async () => {
      const state = await getE2EState(page, request, sessionId);
      return state.job?.status ?? "pending";
    }, { timeout: 120_000 }).toBe("completed");
  }
  await expect(page.getByTestId("score-preview-surface").locator("svg")).toBeVisible();
}

async function waitForAudio(
  page: Page,
  request: Parameters<typeof getE2EState>[1],
  testInfo: TestInfo,
): Promise<E2EState> {
  const audio = page.getByTestId("synthesis-audio");
  await expect.poll(async () => {
    const state = await getE2EState(page, request, sessionId);
    return state.job?.status ?? "pending";
  }, { timeout: 540_000 }).toMatch(/^(completed|failed)$/);

  const terminalState = await getE2EState(page, request, sessionId);
  if (terminalState.job?.status !== "completed") {
    throw new Error(`Real synthesis failed: ${terminalState.job?.error ?? "unknown error"}`);
  }
  await expect(audio).toBeVisible({ timeout: 30_000 });
  await expect(audio).toHaveAttribute("src", /\/sessions\/.*\/audio/);
  const state = await expect.poll(async () => getE2EState(page, request, sessionId), {
    timeout: 30_000,
  }).toMatchObject({ synthesis: { duration_seconds: expect.any(Number) } });

  const result = await getE2EState(page, request, sessionId);
  expect(result.synthesis?.duration_seconds).toBeGreaterThan(0);
  if (result.synthesis?.output_path) {
    const source = path.resolve(import.meta.dirname, "..", "..", "..", result.synthesis.output_path);
    await copyFile(source, testInfo.outputPath(`${testInfo.title}.wav`));
  }
  return result;
}
