import type { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import type { PerformanceMeasureMapEntry } from "./api";

/** Follow the existing played-order map without rescanning its prefix each frame. */
export const createPerformanceMeasureLookup = () => {
  let previousEntries: readonly PerformanceMeasureMapEntry[] | undefined;
  let currentIndex = -1;

  return (
    entries: readonly PerformanceMeasureMapEntry[] | undefined,
    seconds: number,
  ): PerformanceMeasureMapEntry | null => {
    if (entries !== previousEntries) {
      previousEntries = entries;
      currentIndex = -1;
    }
    if (!entries?.length || !Number.isFinite(seconds)) return null;

    const containsTime = (entry: PerformanceMeasureMapEntry | undefined) =>
      entry && seconds >= entry.start_seconds && seconds < entry.end_seconds;
    if (containsTime(entries[currentIndex])) return entries[currentIndex];
    if (containsTime(entries[currentIndex + 1])) return entries[++currentIndex];

    // Seeking, a changed map, or a frame that skipped several measures. End
    // times are ordered; find the first interval that has not ended yet.
    let low = 0;
    let high = entries.length;
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2);
      if (entries[middle].end_seconds <= seconds) low = middle + 1;
      else high = middle;
    }
    currentIndex = low;
    if (containsTime(entries[currentIndex])) return entries[currentIndex];
    // Keep the final bar highlighted after natural completion. A gap or time
    // before the score starts must not incorrectly highlight the final bar.
    if (low === entries.length) {
      currentIndex = entries.length - 1;
      return entries[currentIndex];
    }
    return null;
  };
};

/** Move OSMD's native measure-area cursor, drawing only the destination. */
export const positionNativeScoreCursor = (
  osmd: OpenSheetMusicDisplay,
  sourceMeasureIndex: number,
): boolean => {
  const measure = osmd.Sheet.SourceMeasures[sourceMeasureIndex];
  if (!measure) return false;
  // The backend's played-order map decides repeats and jumps. OSMD follows
  // original notation positions and must not independently repeat/stop them.
  osmd.EngravingRules.CursorIgnoreRepetitions = true;
  const cursor = osmd.cursor;
  cursor.hide();
  const currentIndex = cursor.Iterator.CurrentMeasureIndex;
  if (sourceMeasureIndex === currentIndex + 1 && !cursor.Iterator.EndReached) {
    cursor.nextMeasure();
  }
  if (cursor.Iterator.CurrentMeasureIndex !== sourceMeasureIndex || cursor.Iterator.EndReached) {
    // Native iterator construction may traverse source entries internally,
    // but only on discontinuities, with no intermediate cursor/DOM updates.
    // Use the source timestamp, never a printed measure number or pass count.
    cursor.iterator = osmd.Sheet.MusicPartManager.getIterator(measure.AbsoluteTimestamp);
    cursor.iterator.SkipInvisibleNotes = cursor.SkipInvisibleNotes;
  }
  if (cursor.Iterator.CurrentMeasureIndex !== sourceMeasureIndex) return false;
  cursor.show();
  return true;
};
