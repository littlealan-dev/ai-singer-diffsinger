// The player's track state: vocal takes, instrument rows and the instrument bus,
// shared by the app's player and the public share page.

import { getContext as getToneContext } from "tone";
import type { InstrumentalPart, PerformanceMidi, WorkspaceInstrument } from "../api";

export type VocalMode = "lyrics" | "solfege";

export type MultiTrackAudioTrack = {
  key: string;
  label: string;
  audioUrl: string;
  partId?: string | null;
  partIndex?: number | null;
  verseNumber?: string | number | null;
  durationSeconds?: number | null;
  jobId?: string;
  // The completed job whose asset is currently decoded by the player. This is
  // distinct from jobId so a historical UI state cannot pair a newer label/job
  // with an older rendition's audio source. Required: it keys the decoded-buffer
  // cache, so a track without it cannot be played or cached correctly.
  sourceJobId: string;
  // Whether the take was rendered with repeats; unknown for a take from before
  // jobs recorded it. The player keeps only takes of one repeat setting, and
  // plays the instrumental MIDI in that order.
  expandRepeats?: boolean;
  // The part's take_signature when the take was rendered; null for a take from
  // before jobs recorded it. A take whose signature no longer matches its part
  // in the current score summary was sung from music that has since changed.
  takeSignature?: string | null;
  // Lyrics or solfège; a take from before Sing All is lyrics.
  mode?: VocalMode;
  muted: boolean;
  solo: boolean;
  volume: number;
};

export type InstrumentalTrackState = {
  key: string;
  partId: string;
  rawPartId: string;
  partIndex: number;
  label: string;
  muted: boolean;
  unassigned: boolean;
  solo: boolean;
  volume: number;
  gmProgram: number;
  soundfontBank: number;
  presetKind: "melodic" | "percussion_kit";
  percussion?: boolean;
};

/**
 * The row key of an instrumental part. A part written on several staves, such
 * as a piano, arrives as one entry and one MIDI track per staff, all sharing
 * its raw part ID, so all of them get one row.
 */
export const instrumentalTrackKey = (part: InstrumentalPart): string =>
  `inst-${part.raw_part_id || part.part_index}`;

/**
 * The row that controls MIDI track `index`. The MIDI has one track per
 * eligible entry, in order, and `midiTrackKeys` holds each track's row key.
 */
export const instrumentalTrackForMidiTrack = (
  index: number,
  midiTrackKeys: readonly string[],
  instrumentalTracks: InstrumentalTrackState[]
): InstrumentalTrackState | null => {
  const key = midiTrackKeys[index];
  if (key !== undefined) return instrumentalTracks.find((track) => track.key === key) ?? null;
  return instrumentalTracks.length === 1 ? instrumentalTracks[0] : null;
};

export type InstrumentalBusState = {
  muted: boolean;
  solo: boolean;
  levelDb: number;
};

export const DEFAULT_INSTRUMENTAL_BUS: InstrumentalBusState = {
  muted: false,
  solo: false,
  levelDb: 0,
};

export const INSTRUMENTAL_BUS_MIN_DB = -48;

export const INSTRUMENTAL_BUS_MAX_DB = 6;

export const dbToGain = (db: number) => Math.pow(10, db / 20);

export const DEFAULT_INSTRUMENT_TRACK_VOLUME = 0.65;

/** Download and decode one take, in the playback context the player uses. */
export const decodeVocalAudio = async (audioUrl: string): Promise<AudioBuffer> => {
  // The URL carries its own playback token, as for the player's loader.
  const response = await fetch(audioUrl);
  if (!response.ok) {
    // Keep the status code: an expired token (401) is refreshed on Play.
    throw new Error(`Failed to fetch ${audioUrl}: ${response.status} ${response.statusText}`);
  }
  return getToneContext().rawContext.decodeAudioData(await response.arrayBuffer());
};

export const shouldMuteMultiTrackForPlayback = (
  track: MultiTrackAudioTrack,
  tracks: MultiTrackAudioTrack[]
): boolean => {
  const hasSolo = tracks.some((candidate) => candidate.solo);
  return hasSolo ? !track.solo : track.muted;
};

export const hasAudibleScorePlayerMix = (
  vocalTracks: MultiTrackAudioTrack[],
  instrumentalTracks: InstrumentalTrackState[],
  instrumentalBus: InstrumentalBusState
) => {
  const hasIndividualInstrumentSolo = instrumentalTracks.some((track) => track.solo);
  const tracks = [
    ...instrumentalTracks.map((track) => ({
      muted: instrumentalBus.muted || track.muted,
      solo: hasIndividualInstrumentSolo ? track.solo : instrumentalBus.solo,
    })),
    ...vocalTracks.map((track) => ({ muted: track.muted, solo: track.solo })),
  ];
  if (!tracks.length) return false;
  const hasSolo = tracks.some((track) => track.solo);
  return hasSolo
    ? tracks.some((track) => track.solo && !track.muted)
    : tracks.some((track) => !track.muted);
};

// One row per part: a part's later staves share its first staff's row. A row keeps a reopened
// Song's saved choice, else its current values, else the part's own defaults.
export const instrumentRowsFromMidi = (
  performanceMidi: PerformanceMidi,
  savedChoices: readonly WorkspaceInstrument[] | null,
  current: readonly InstrumentalTrackState[]
): InstrumentalTrackState[] => {
  const eligible = performanceMidi.instrumental_parts.filter((p) => p.eligible);
  const rowParts = eligible.filter(
    (part, index) =>
      eligible.findIndex((other) => instrumentalTrackKey(other) === instrumentalTrackKey(part)) === index
  );
  return rowParts.map((part) => {
    const saved = part.raw_part_id
      ? savedChoices?.find((choice) => choice.raw_part_id === part.raw_part_id)
      : undefined;
    const existing = saved
      ? {
          unassigned: saved.unassigned,
          muted: saved.muted,
          solo: saved.solo,
          volume: saved.volume,
          gmProgram: saved.gm_program ?? part.playback_preset?.program ?? part.midi_program ?? 0,
          soundfontBank:
            saved.soundfont_bank ??
            part.playback_preset?.bank ??
            part.soundfont_bank ??
            (part.percussion ? 128 : 0),
          presetKind:
            saved.preset_kind ??
            part.playback_preset?.kind ??
            (part.percussion ? ("percussion_kit" as const) : ("melodic" as const)),
        }
      : part.raw_part_id
        ? current.find((track) => track.rawPartId === part.raw_part_id)
        : current.find((track) => track.partId === part.part_id);
    return {
      key: instrumentalTrackKey(part),
      partId: part.part_id,
      rawPartId: part.raw_part_id,
      partIndex: part.part_index,
      label: part.label || `Part ${part.part_index + 1}`,
      unassigned: part.program_source === "user_assignable" ? (existing ? existing.unassigned : true) : false,
      muted: existing ? existing.muted : part.program_source === "user_assignable",
      solo: existing ? existing.solo : false,
      volume: existing ? existing.volume : DEFAULT_INSTRUMENT_TRACK_VOLUME,
      gmProgram: existing ? existing.gmProgram : (part.playback_preset?.program ?? part.midi_program ?? 0),
      // A part with no sound chosen is a drum kit or an instrument as the part itself is.
      soundfontBank: part.program_source === "user_assignable" ? (part.percussion ? 128 : 0) : existing ? existing.soundfontBank : (part.playback_preset?.bank ?? part.soundfont_bank ?? (part.percussion ? 128 : 0)),
      presetKind: part.program_source === "user_assignable" ? (part.percussion ? "percussion_kit" : "melodic") : existing ? existing.presetKind : (part.playback_preset?.kind ?? (part.percussion ? "percussion_kit" : "melodic")),
      percussion: part.percussion,
    };
  });
};

// With takes in both modes the player shows one mode's takes at a time; with one mode, all of them.
export const vocalsInBothModes = (tracks: readonly MultiTrackAudioTrack[]): boolean =>
  new Set(tracks.map((track) => track.mode ?? "lyrics")).size > 1;

export const shownVocals = (tracks: MultiTrackAudioTrack[], mode: VocalMode): MultiTrackAudioTrack[] =>
  vocalsInBothModes(tracks) ? tracks.filter((track) => (track.mode ?? "lyrics") === mode) : tracks;
