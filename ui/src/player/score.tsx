// The score preview: the horizontal and page renderers, the page view's zoom helpers,
// the layout toggle's icons and the cursor-follow scrolling, shared by the app and
// the public share page.

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

export type PageScoreRendererProps = {
  scoreData: string;
  zoomLevel: number;
  onOsmdChange: (osmd: OpenSheetMusicDisplay | null) => void;
  onReady: () => void;
  onRenderError: () => void;
};

/** The page view (A4 pages fitted to the container's width), for the share page. */
export const PageScoreRenderer = ({ scoreData, zoomLevel, onOsmdChange, onReady, onRenderError }: PageScoreRendererProps) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const callbacksRef = useRef({ onOsmdChange, onReady, onRenderError });
  callbacksRef.current = { onOsmdChange, onReady, onRenderError };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let cancelled = false;
    container.replaceChildren();
    // The same options as the app's page view; fitting to the width happens after engraving.
    const osmd = new OpenSheetMusicDisplay(container, {
      autoResize: false,
      drawTitle: true,
      followCursor: false,
      cursorsOptions: [{ type: 3, color: "#8b5cf6", alpha: 0.24, follow: false }],
      pageFormat: "A4_P",
      renderSingleHorizontalStaffline: false,
    });
    void (async () => {
      try {
        await osmd.load(scoreData);
        if (cancelled) return;
        applyPageLayoutZoom(osmd, zoomLevel);
        osmd.render();
        preparePagePreviewWrappers(container);
        fitPagePreviewToContainer(container);
        osmd.cursor.hide();
        callbacksRef.current.onOsmdChange(osmd);
        callbacksRef.current.onReady();
      } catch {
        if (!cancelled) callbacksRef.current.onRenderError();
      }
    })();
    let frame = 0;
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => fitPagePreviewToContainer(container));
    });
    observer.observe(container);
    return () => {
      cancelled = true;
      observer.disconnect();
      window.cancelAnimationFrame(frame);
      callbacksRef.current.onOsmdChange(null);
      try {
        osmd.clear();
      } catch {
        // A half-rendered OSMD instance may fail to clear.
      }
    };
  }, [scoreData, zoomLevel]);

  return <div ref={containerRef} className="score-surface page-layout" data-testid="score-preview-surface" />;
};

export const PageViewIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" clipRule="evenodd" d="M19.4291 10.9231H4.57087C4.25599 10.9231 4 11.1938 4 11.5384C4 11.8831 4.25599 12.1538 4.57087 12.1538H19.4291C19.744 12.1538 20 11.8831 20 11.5384C20 11.1938 19.744 10.9231 19.4291 10.9231Z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M19.4291 7.49449H4.57087C4.25599 7.49449 4 7.76526 4 8.10987C4 8.45449 4.25599 8.72526 4.57087 8.72526H19.4291C19.744 8.72526 20 8.45449 20 8.10987C20 7.76526 19.744 7.49449 19.4291 7.49449Z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M19.4291 14.3516H4.57087C4.25599 14.3516 4 14.6224 4 14.967C4 15.3116 4.25599 15.5824 4.57087 15.5824H19.4291C19.744 15.5824 20 15.3116 20 14.967C20 14.6224 19.744 14.3516 19.4291 14.3516Z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M19.4291 4.06592H4.57087C4.25599 4.06592 4 4.33669 4 4.6813C4 5.02592 4.25599 5.29669 4.57087 5.29669H19.4291C19.744 5.29669 20 5.02592 20 4.6813C20 4.33669 19.744 4.06592 19.4291 4.06592Z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M19.4291 17.7802H4.57087C4.25599 17.7802 4 18.051 4 18.3956C4 18.7402 4.25599 19.011 4.57087 19.011H19.4291C19.744 19.011 20 18.7402 20 18.3956C20 18.051 19.744 17.7802 19.4291 17.7802Z" />
  </svg>
);

export const HorizontalViewIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path
      fillRule="evenodd"
      clipRule="evenodd"
      d="M18.7692 4.94205V19.0598C18.7692 19.5784 19.04 20 19.3846 20C19.7292 20 20 19.5784 20 19.0598V4.94205C20 4.42346 19.7292 4 19.3846 4C19.04 4 18.7692 4.42346 18.7692 4.94205ZM13.8462 4.57087L13.8462 19.4291C13.8462 19.744 14.1169 20 14.4615 20C14.8062 20 15.0769 19.744 15.0769 19.4291L15.0769 4.57087C15.0769 4.25598 14.8062 4 14.4615 4C14.1169 4 13.8462 4.25598 13.8462 4.57087ZM8.92308 19.2008C8.92308 19.6416 9.19385 20 9.53846 20C9.88308 20 10.1538 19.6416 10.1538 19.2008V4.7992C10.1538 4.35837 9.88308 4 9.53846 4C9.19385 4 8.92308 4.35837 8.92308 4.7992ZM5.23077 19.4291L5.23077 4.57087C5.23077 4.25598 4.96 4 4.61538 4C4.27077 4 4 4.25598 4 4.57087L4 19.4291C4 19.744 4.27077 20 4.61538 20C4.96 20 5.23077 19.744 5.23077 19.4291Z"
    />
  </svg>
);
