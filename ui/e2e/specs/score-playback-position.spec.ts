import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import type { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import type * as playback from "../../src/scorePlayback";

declare global {
  interface Window {
    scorePlaybackTest: typeof playback & {
      OpenSheetMusicDisplay: typeof OpenSheetMusicDisplay;
    };
  }
}

test.beforeEach(async ({ page }) => {
  await page.goto("/e2e/fixtures/score-playback-harness.html");
  await page.waitForFunction(() => Boolean(window.scorePlaybackTest));
});

test("follows timing intervals, repeat occurrences and seeks without scanning the prefix", async ({ page }) => {
  const result = await page.evaluate(() => {
    const lookup = window.scorePlaybackTest.createPerformanceMeasureLookup();
    const visits = [0, 1, 0, 1, 2];
    const map = visits.map((source, index) => ({
      played_measure_index: index, source_measure_index: source,
      source_measure_number: String(source + 1), pass_index: index === 2 || index === 3 ? 1 : 0,
      start_seconds: index * 3, end_seconds: (index + 1) * 3,
    }));
    const positions = [0, 2.99, 3, 6, 10.2, 10.22, 12.01, 1, 15].map(
      (time) => lookup(map, time)?.played_measure_index,
    );
    const written = map.slice(0, 3).map((entry, index) => ({ ...entry, source_measure_index: index }));
    const switched = lookup(written, 6.5)?.source_measure_index;
    const beforeStart = lookup(map, -1);
    const invalid = lookup(map, Number.NaN);
    const gap = lookup([{ ...map[0], end_seconds: 1 }, { ...map[1], start_seconds: 2 }], 1.5);

    let reads = 0;
    const longMap = Array.from({ length: 1000 }, (_, index) => ({
      ...map[0], played_measure_index: index, source_measure_index: index,
      get start_seconds() { reads++; return index * 3; },
      get end_seconds() { reads++; return (index + 1) * 3; },
    }));
    lookup(longMap, 2700.2); // Seek directly to a late measure.
    const seekReads = reads;
    reads = 0;
    for (let frame = 0; frame < 100; frame++) lookup(longMap, 2700.2 + frame / 60);
    const frameReads = reads;
    reads = 0;
    const next = lookup(longMap, 2703.01)?.played_measure_index;
    return { positions, switched, beforeStart, invalid, gap, seekReads, frameReads, next, transitionReads: reads };
  });
  expect(result.positions).toEqual([0, 0, 1, 2, 3, 3, 4, 0, 4]);
  expect(result.switched).toBe(2);
  expect(result.beforeStart).toBeNull();
  expect(result.invalid).toBeNull();
  expect(result.gap).toBeNull();
  expect(result.seekReads).toBeLessThan(20);
  expect(result.frameReads).toBeLessThanOrEqual(200);
  expect(result.next).toBe(901);
  expect(result.transitionReads).toBeLessThanOrEqual(4);
});

const navigationCases: [string, number[]][] = [
  ["forward_repeat.xml", [1, 2, 1, 2, 3, 4, 5]],
  ["volta_endings.xml", [1, 2, 1, 3, 4, 5]],
  ["da_capo.xml", [1, 2, 3, 4, 1, 2, 3, 4, 5]],
  ["da_capo_al_fine.xml", [1, 2, 3, 4, 1, 2]],
  ["da_capo_al_coda.xml", [1, 2, 3, 4, 1, 2, 3, 5, 6]],
  ["dal_segno.xml", [1, 2, 3, 4, 2, 3, 4, 5]],
  ["dal_segno_al_fine.xml", [1, 2, 3, 4, 2, 3]],
  ["dal_segno_al_coda.xml", [1, 2, 3, 4, 5, 3, 4, 6]],
];

for (const layout of ["page", "horizontal"] as const) {
  test(`native ${layout} cursor follows all navigation fixtures and written order`, async ({ page }) => {
    for (const [filename, route] of navigationCases) {
      const xml = await readFile(path.resolve(import.meta.dirname, "../../../tests/fixtures/repeat_navigation", filename), "utf8");
      const result = await page.evaluate(async ({ xml, layout, route }) => {
        const { OpenSheetMusicDisplay, positionNativeScoreCursor } = window.scorePlaybackTest;
        const container = document.getElementById("score")!;
        container.replaceChildren();
        const osmd = new OpenSheetMusicDisplay(container, {
          autoResize: false, followCursor: false,
          cursorsOptions: [{ type: 3, color: "#8b5cf6", alpha: 0.24, follow: false }],
          pageFormat: layout === "page" ? "A4_P" : "Endless",
          renderSingleHorizontalStaffline: layout === "horizontal",
        });
        await osmd.load(xml);
        osmd.zoom = 0.8;
        if (layout === "horizontal") osmd.renderNext({ measures: 2 });
        else osmd.render();
        osmd.cursor.hide();
        const written = osmd.Sheet.SourceMeasures.map((_, index) => index + 1);
        // Include backward/forward seeks as well as both repeat-toggle routes.
        const targets = [...route, ...written, written.length, 1, written.length, 2];
        const positions = targets.map((number) => {
          const target = number - 1;
          while (layout === "horizontal" && !osmd.IncrementalRenderingComplete && !osmd.Sheet.SourceMeasures[target].WasRendered) {
            osmd.renderNext({ measures: 2 });
          }
          const positioned = positionNativeScoreCursor(osmd, target);
          const cursor = osmd.cursor;
          const graphicalMeasure = osmd.GraphicSheet.MeasureList[target][0];
          return {
            target, actual: cursor.Iterator.CurrentMeasureIndex, positioned,
            timestamp: osmd.Sheet.SourceMeasures[target].AbsoluteTimestamp.RealValue,
            end: osmd.Sheet.SelectionEnd?.RealValue,
            ended: cursor.Iterator.EndReached,
            visible: !cursor.Hidden && cursor.cursorElement.isConnected,
            leftError: Math.abs(parseFloat(cursor.cursorElement.style.left) - graphicalMeasure.PositionAndShape.AbsolutePosition.x * 10 * osmd.zoom),
          };
        });
        osmd.clear();
        return positions;
      }, { xml, layout, route });
      for (const position of result) {
        expect(position, `${filename}: ${JSON.stringify(position)}`).toMatchObject({ actual: position.target, positioned: true, visible: true });
        expect(position.leftError).toBeLessThan(0.1);
      }
    }
  });

  test(`late ${layout} measures require one native advance and one visible update`, async ({ page }, testInfo) => {
    // Multiple voices and note positions expose repeated visual updates hidden
    // by short, one-note fixtures. Construct the long score only in the test.
    const result = await page.evaluate(async (layout) => {
      const { OpenSheetMusicDisplay, positionNativeScoreCursor } = window.scorePlaybackTest;
      const attributes = '<attributes><divisions>2</divisions><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>';
      const notes = Array.from({ length: 8 }, (_, index) => `<note><pitch><step>${index % 2 ? "D" : "C"}</step><octave>4</octave></pitch><duration>1</duration><type>eighth</type></note>`).join("");
      const measures = Array.from({ length: 128 }, (_, index) => `<measure number="${index + 1}">${index === 0 ? attributes : ""}${notes}</measure>`).join("");
      const xml = `<?xml version="1.0"?><score-partwise version="3.1"><part-list><score-part id="P1"><part-name>Piano</part-name></score-part><score-part id="P2"><part-name>Voice</part-name></score-part></part-list><part id="P1">${measures}</part><part id="P2">${measures}</part></score-partwise>`;
      const osmd = new OpenSheetMusicDisplay(document.getElementById("score")!, {
        autoResize: false, followCursor: false, cursorsOptions: [{ type: 3, color: "#8b5cf6", alpha: 0.24, follow: false }],
        pageFormat: layout === "page" ? "A4_P" : "Endless", renderSingleHorizontalStaffline: layout === "horizontal",
      });
      await osmd.load(xml);
      osmd.render();
      osmd.cursor.hide();
      const cursor = osmd.cursor;
      let advances = 0;
      let visualUpdates = 0;
      const nextMeasure = cursor.nextMeasure.bind(cursor);
      cursor.nextMeasure = () => { advances++; nextMeasure(); };
      const update = cursor.update.bind(cursor);
      cursor.update = () => { if (!cursor.Hidden) visualUpdates++; update(); };
      const timings: number[] = [];
      const counts: { advances: number; visualUpdates: number; actual: number }[] = [];
      for (let target = 0; target < 128; target++) {
        advances = 0;
        visualUpdates = 0;
        const start = performance.now();
        positionNativeScoreCursor(osmd, target);
        timings.push(performance.now() - start);
        counts.push({ advances, visualUpdates, actual: cursor.Iterator.CurrentMeasureIndex });
      }
      // Compare the previous implementation's work for the same final bar.
      advances = 0;
      visualUpdates = 0;
      const baselineStart = performance.now();
      cursor.reset();
      for (let index = 0; index < 127; index++) cursor.nextMeasure();
      cursor.show();
      const baseline = { ms: performance.now() - baselineStart, advances, visualUpdates };
      osmd.clear();
      return { counts, timings, baseline };
    }, layout);
    expect(result.counts[0]).toEqual({ advances: 0, visualUpdates: 1, actual: 0 });
    result.counts.slice(1).forEach((counts, index) => {
      expect(counts).toEqual({ advances: 1, visualUpdates: 1, actual: index + 1 });
    });
    expect(result.baseline.advances).toBe(127);
    expect(result.baseline.visualUpdates).toBeGreaterThan(1000);
    await testInfo.attach(`${layout}-cursor-cost`, { body: JSON.stringify(result, null, 2), contentType: "application/json" });
  });
}
