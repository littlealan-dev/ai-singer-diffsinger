// The share page's rows, as the owner's saved mix leaves them. A viewer can only
// turn rows on and off; levels are fixed, and nothing the viewer does is saved.

import type { PerformanceMidi, SharePlayer, ShareVocal } from "../api";
import { dbToGain, instrumentRowsFromMidi, type InstrumentalTrackState } from "../player/tracks";

export type ShareVocalRow = ShareVocal & { level: number; on: boolean };
export type ShareInstrumentRow = InstrumentalTrackState & { level: number; on: boolean };
export type ShareRows = {
  vocals: ShareVocalRow[];
  instruments: ShareInstrumentRow[];
  /** Parts with no sound chosen: not rows, but their MIDI tracks must stay silent. */
  silent: InstrumentalTrackState[];
};

/**
 * Each row's starting level and on/off. Instruments are the app's own rows with
 * the owner's choices applied; a part with no sound chosen is not a row. The
 * bus folds into each instrument as the app's player applies it, then a solo
 * anywhere means only the soloed, unmuted rows start on.
 */
export const startRows = (
  player: SharePlayer,
  performanceMidi: Partial<PerformanceMidi> | null | undefined
): ShareRows => {
  const parts = performanceMidi?.instrumental_parts;
  const built = parts
    ? instrumentRowsFromMidi({ ...performanceMidi, instrumental_parts: parts } as PerformanceMidi, player.instruments, [])
    : [];
  const assigned = built.filter((row) => !row.unassigned);
  const anyInstrumentSolo = assigned.some((row) => row.solo);
  const busGain = dbToGain(player.bus.level_db);
  const instruments = assigned.map((row) => ({
    ...row,
    muted: player.bus.muted || row.muted,
    solo: anyInstrumentSolo ? row.solo : player.bus.solo,
    level: row.volume * busGain,
    on: false,
  }));
  const vocals = player.vocals.map((vocal) => ({ ...vocal, level: vocal.volume, on: false }));
  const anySolo = [...vocals, ...instruments].some((row) => row.solo);
  const isOn = (row: { muted: boolean; solo: boolean }) => (anySolo ? row.solo && !row.muted : !row.muted);
  return {
    vocals: vocals.map((row) => ({ ...row, on: isOn(row) })),
    instruments: instruments.map((row) => ({ ...row, on: isOn(row) })),
    silent: built.filter((row) => row.unassigned),
  };
};
