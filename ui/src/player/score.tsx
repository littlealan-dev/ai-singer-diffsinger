// The score preview: the horizontal renderer, the page view's zoom helpers and the
// cursor-follow scrolling, shared by the app and the public share page.

import { useEffect, useRef, type RefObject } from "react";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";

export type ScorePreviewLayout = "page" | "horizontal";

export const scrollScoreMeasureIntoView = (
  container: HTMLElement,
  target: HTMLElement,
  layout: ScorePreviewLayout,
) => {
  const containerRect = container.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const margin = 24;
  let nextLeft = container.scrollLeft;
  let nextTop = container.scrollTop;

  if (targetRect.left < containerRect.left + margin) {
    nextLeft += targetRect.left - containerRect.left - margin;
  } else if (targetRect.right > containerRect.right - margin) {
    // Align the arriving measure to the reading origin instead of merely
    // exposing its right edge. This keeps following measures in view for both
    // page and horizontal reading, avoiding a scroll on nearly every measure.
    nextLeft += targetRect.left - containerRect.left - margin;
  }
  // Horizontal engraving is one left-to-right score. Following its cursor
  // vertically makes the preview bounce even though the playback position has
  // not moved into a new vertical reading region.
  if (layout === "page") {
    if (targetRect.top < containerRect.top + margin) {
      nextTop += targetRect.top - containerRect.top - margin;
    } else if (targetRect.bottom > containerRect.bottom - margin) {
      // As above, place the newly followed system at the top of the readable
      // viewport rather than only making its bottom edge barely visible.
      nextTop += targetRect.top - containerRect.top - margin;
    }
  }

  if (nextLeft !== container.scrollLeft || nextTop !== container.scrollTop) {
    container.scrollTo({ left: Math.max(0, nextLeft), top: Math.max(0, nextTop), behavior: "smooth" });
  }
};

export const applyPageLayoutZoom = (osmd: OpenSheetMusicDisplay, zoomLevel: number): void => {
  // Let OSMD own both engraving scale and system/page breaking. This is the
  // same native zoom API used by the horizontal renderer; keeping an A4 page
  // format gives it a fixed printable width in which to recompute measure
  // wrapping for every zoom level.
  osmd.setPageFormat("A4_P");
  osmd.zoom = Number.isFinite(zoomLevel) ? Math.min(2, Math.max(0.6, zoomLevel)) : 1;
};

/**
 * OSMD emits each page as a `div#osmdCanvasPageN` containing its SVG. Wrap
 * that complete page canvas with its native dimensions so the browser can
 * scale notation and cursor together without another engraving pass.
 */
export const preparePagePreviewWrappers = (container: HTMLElement): void => {
  const pages = Array.from(container.children).filter(
    (child): child is HTMLElement =>
      child instanceof HTMLElement &&
      child.id.startsWith("osmdCanvasPage") &&
      Boolean(child.querySelector(":scope > svg"))
  );

  for (const page of pages) {
    const bounds = page.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) continue;

    const wrapper = document.createElement("div");
    wrapper.className = "score-page-virtualizer";
    wrapper.dataset.nativeWidth = String(bounds.width);
    wrapper.dataset.nativeHeight = String(bounds.height);
    wrapper.style.width = `${Math.ceil(bounds.width)}px`;
    wrapper.style.height = `${Math.ceil(bounds.height)}px`;
    page.replaceWith(wrapper);
    wrapper.append(page);
  }
};

/**
 * A preview-pane resize must not reload and re-engrave the MusicXML. The
 * wrapper retains the scaled scroll geometry, while an inline transform
 * scales the whole OSMD SVG (notation and cursor included) together.
 */
export const fitPagePreviewToContainer = (container: HTMLElement): void => {
  let wrappers = Array.from(
    container.querySelectorAll<HTMLElement>(":scope > .score-page-virtualizer")
  );
  // During local hot reload an already engraved score may still have OSMD's
  // native canvases in the container. Adopt them on the next resize instead
  // of waiting for the user to re-upload or change layouts.
  if (wrappers.length === 0) {
    preparePagePreviewWrappers(container);
    wrappers = Array.from(
      container.querySelectorAll<HTMLElement>(":scope > .score-page-virtualizer")
    );
  }
  const firstPage = wrappers[0];
  if (!firstPage) return;

  const nativeWidth = Number(firstPage.dataset.nativeWidth);
  if (!Number.isFinite(nativeWidth) || nativeWidth <= 0) return;

  const styles = window.getComputedStyle(container);
  const horizontalPadding =
    Number.parseFloat(styles.paddingLeft) + Number.parseFloat(styles.paddingRight);
  const availableWidth = Math.max(0, container.clientWidth - horizontalPadding);
  const scale = availableWidth > 0 ? availableWidth / nativeWidth : 1;
  if (!Number.isFinite(scale) || scale <= 0) return;

  for (const wrapper of wrappers) {
    const width = Number(wrapper.dataset.nativeWidth);
    const height = Number(wrapper.dataset.nativeHeight);
    if (!Number.isFinite(width) || !Number.isFinite(height)) continue;
    const scaledWidth = Math.ceil(width * scale);
    const scaledHeight = Math.ceil(height * scale);
    wrapper.style.width = `${scaledWidth}px`;
    wrapper.style.height = `${scaledHeight}px`;

    // OSMD's SVG does not consistently expose a viewBox, so changing the SVG
    // CSS viewport can resize only the sheet and clip its fixed-coordinate
    // notation. Transform the page canvas instead: this scales every SVG
    // child in the same coordinate system, including the active-measure
    // cursor.
    const page = wrapper.querySelector<HTMLElement>(":scope > [id^='osmdCanvasPage']");
    if (page) {
      page.style.width = `${Math.ceil(width)}px`;
      page.style.height = `${Math.ceil(height)}px`;
      page.style.transform = `scale(${scale})`;
      page.style.transformOrigin = "top left";
    }
  }
};

// Keep each native incremental-rendering batch within a bounded engraving
// budget. A two-staff piano part counts as two simultaneous staves, so dense
// scores naturally render fewer measures ahead than solo scores.
export const HORIZONTAL_SCORE_STAFF_MEASURE_BUDGET = 16;

export const HORIZONTAL_SCORE_MAX_MEASURES_PER_BATCH = 16;

export const countParallelScoreStaves = (scoreData: string, fallbackPartCount: number): number => {
  try {
    const document = new DOMParser().parseFromString(scoreData, "application/xml");
    const parts = Array.from(document.querySelectorAll("score-partwise > part"));
    if (!parts.length) return Math.max(1, fallbackPartCount);
    return Math.max(
      1,
      parts.reduce((total, part) => {
        const stavesText = part.querySelector("measure > attributes > staves")?.textContent;
        const staves = Number.parseInt(stavesText ?? "", 10);
        return total + (Number.isFinite(staves) && staves > 0 ? staves : 1);
      }, 0)
    );
  } catch {
    return Math.max(1, fallbackPartCount);
  }
};

export const getHorizontalScoreIncrementalBatchSize = (parallelStaffCount: number): number =>
  Math.max(
    1,
    Math.min(
      HORIZONTAL_SCORE_MAX_MEASURES_PER_BATCH,
      Math.floor(HORIZONTAL_SCORE_STAFF_MEASURE_BUDGET / Math.max(1, parallelStaffCount))
    )
  );

export type HorizontalScoreRendererHandle = {
  osmd: OpenSheetMusicDisplay;
  ensureSourceMeasureRendered: (sourceMeasureIndex: number) => void;
};

export type HorizontalScoreRendererProps = {
  scoreData: string;
  zoomLevel: number;
  measuresPerBatch: number;
  scrollContainerRef: RefObject<HTMLElement>;
  onRendererChange: (renderer: HorizontalScoreRendererHandle | null) => void;
  onReady: () => void;
  onRenderError: () => void;
};

export const HorizontalScoreRenderer = ({
  scoreData,
  zoomLevel,
  measuresPerBatch,
  scrollContainerRef,
  onRendererChange,
  onReady,
  onRenderError,
}: HorizontalScoreRendererProps) => {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let cancelled = false;
    let osmd: OpenSheetMusicDisplay | null = null;
    container.replaceChildren();

    void (async () => {
      try {
        osmd = new OpenSheetMusicDisplay(container, {
          autoResize: false,
          drawTitle: true,
          followCursor: false,
          cursorsOptions: [{ type: 3, color: "#8b5cf6", alpha: 0.24, follow: false }],
          pageFormat: "Endless",
          renderSingleHorizontalStaffline: true,
        });
        await osmd.load(scoreData);
        if (cancelled) return;
        osmd.zoom = zoomLevel;

        // OSMD 2.x keeps one continuous notation model and appends to the
        // same SVG. Unlike the former range-per-chunk implementation, this
        // preserves the actual score start (one clef/key/time signature) and
        // has no fabricated system boundaries between batches.
        osmd.renderNext({ measures: measuresPerBatch });
        osmd.cursor.hide();
        const ensureSourceMeasureRendered = (sourceMeasureIndex: number) => {
          let guard = 0;
          while (
            !osmd!.IncrementalRenderingComplete &&
            osmd!.IncrementalRenderProgress.renderedMeasures <= sourceMeasureIndex &&
            guard++ < 256
          ) {
            osmd!.renderNext({ measures: measuresPerBatch });
          }
        };
        onRendererChange({ osmd, ensureSourceMeasureRendered });
        onReady();
        osmd.enableIncrementalRenderingOnScroll({
          measures: measuresPerBatch,
          scrollElement: scrollContainerRef.current ?? undefined,
        });
      } catch {
        if (!cancelled) onRenderError();
      }
    })();

    return () => {
      cancelled = true;
      onRendererChange(null);
      try {
        osmd?.disableIncrementalRenderingOnScroll();
        osmd?.clear();
      } catch {
        // Ignore cleanup from a partially initialized renderer.
      }
    };
  }, [measuresPerBatch, onReady, onRenderError, onRendererChange, scoreData, scrollContainerRef, zoomLevel]);

  return (
    <div
      className="score-surface horizontal-incremental-score-surface"
      data-testid="score-preview-surface"
      data-measures-per-batch={measuresPerBatch}
    >
      <div
        ref={containerRef}
        className="horizontal-score-incremental-renderer"
        data-testid="horizontal-score-incremental-renderer"
      />
    </div>
  );
};
