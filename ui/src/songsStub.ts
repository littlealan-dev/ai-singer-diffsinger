/**
 * TEMPORARY stand-in for the Songs backend (SIG-194), so the Songs UI can be
 * built and tried before that backend exists. Delete this file, and the
 * `SONGS_API_STUBBED` branches in api.ts, once the backend is deployed.
 *
 * It answers the Songs endpoints from this browser's localStorage, per signed-in
 * user. Uploads, chat, takes and Stop still go to the real backend.
 *
 * Where it differs from the real backend:
 * - Songs live in this browser only, and are lost when site data is cleared.
 * - The server records the chat and each take by itself. Here the page reports
 *   them through `recordState`, so only what a page saw is remembered. A take
 *   that finished while the page was away is picked up when the Song is next
 *   opened or listed, by asking the real backend for that job's progress.
 * - Replace stops the running work before the new file is checked, so a bad
 *   file can stop a job. The real backend checks the file first.
 * - Chat messages are saved as shown, not from the server's history.
 * - A vocal volume up to 1.2 is accepted, because the player's vocal sliders
 *   go that far. The Songs spec says 0..1 for every volume.
 */
import type {
  PlayerMixRequest,
  ProgressResponse,
  ScoreSummary,
  PerformanceMidi,
  SolfegeSettings,
  SongInfo,
  SongRow,
  SongWorkspace,
  StopWorkResponse,
  UploadResponse,
  UploadScoreOptions,
  WorkspaceBus,
  WorkspaceInstrument,
  WorkspaceJob,
  WorkspaceMessage,
  WorkspaceVocal,
} from "./api";

export type SongsStubDeps = {
  userId: () => string | null;
  uploadScore: (
    sessionId: string,
    file: File,
    options?: UploadScoreOptions
  ) => Promise<UploadResponse>;
  fetchProgress: (progressUrl: string) => Promise<ProgressResponse>;
  stopSessionWork: (sessionId: string) => Promise<StopWorkResponse>;
  makeError: (message: string, status: number, code: string) => Error;
  errorCode: (error: unknown) => string | undefined;
};

/** What the page reports, standing in for what the server records by itself. */
export type SongsStubRecordedState = {
  messages: Array<{ id: string; role: "user" | "assistant"; content: string }>;
  jobs: Array<{
    job_id: string;
    job_kind?: string | null;
    progress_url: string;
    originating_turn_id: string | null;
  }>;
  score_id: string | null;
  score_summary: ScoreSummary | null;
  performance_midi: PerformanceMidi | null;
  // The page knows the choice, not the server's revision number.
  solfege_settings: Pick<SolfegeSettings, "system" | "mode"> | null;
};

export type SongsStub = {
  uploadScore: (
    sessionId: string,
    file: File,
    options?: UploadScoreOptions
  ) => Promise<UploadResponse>;
  fetchWorkspace: (songId: string) => Promise<SongWorkspace>;
  listSongs: (limit: number) => Promise<SongRow[]>;
  renameSong: (songId: string, title: string) => Promise<SongRow>;
  deleteSong: (songId: string) => Promise<void>;
  savePlayerMix: (songId: string, mix: PlayerMixRequest) => Promise<{ revision: number }>;
  recordState: (songId: string, state: SongsStubRecordedState) => void;
};

type StoredVocal = Omit<WorkspaceVocal, "audio_url" | "progress_url" | "muted" | "solo" | "volume">;

type StoredJob = {
  job_id: string;
  job_kind: string | null;
  progress_url: string;
  originating_turn_id: string | null;
  created_at: string;
  /** Set once the job has ended; an ended job's take is recorded only once. */
  ended?: "done" | "failed" | "dropped";
};

type StoredSong = {
  song: SongInfo & { last_active_at: string };
  deleted: boolean;
  score_id: string | null;
  messages: WorkspaceMessage[];
  jobs: StoredJob[];
  player: {
    score_id: string | null;
    revision: number;
    repeats: boolean;
    vocals: StoredVocal[];
    /** Mixer values by track key; kept apart so a new take of a part keeps them. */
    vocal_mix: Record<string, { muted: boolean; solo: boolean; volume: number }>;
    instruments: WorkspaceInstrument[];
    bus: WorkspaceBus;
  };
};

/** The large part of a Song, kept under its own key so chat updates stay small. */
type StoredScore = {
  score_id: string | null;
  current_score?: unknown;
  score_summary: ScoreSummary | null;
  solfege_settings?: SolfegeSettings;
};

const STORAGE_PREFIX = "sightsinger.songs-stub.v1";
const DEFAULT_BUS: WorkspaceBus = { muted: false, solo: false, level_db: 0 };
const TITLE_MAX_LENGTH = 120;
const REPLACE_UPLOAD_RETRIES = 5;
const REPLACE_UPLOAD_RETRY_MS = 400;

const wait = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

function emptyPlayer(scoreId: string | null): StoredSong["player"] {
  return {
    score_id: scoreId,
    revision: 0,
    repeats: false,
    vocals: [],
    vocal_mix: {},
    instruments: [],
    bus: { ...DEFAULT_BUS },
  };
}

function songTitleFrom(summary: ScoreSummary | null | undefined, fileName: string): string {
  const fromFile = fileName.replace(/\.[^./\\]+$/, "");
  const title = [summary?.title, fromFile, "Untitled song"]
    .map((candidate) => (typeof candidate === "string" ? candidate.trim() : ""))
    .find((candidate) => candidate.length > 0);
  return (title ?? "Untitled song").slice(0, TITLE_MAX_LENGTH);
}

export function createSongsStub(deps: SongsStubDeps): SongsStub {
  const songKey = (songId: string) => `${STORAGE_PREFIX}:${deps.userId() ?? "anon"}:song:${songId}`;
  const scoreKey = (songId: string) => `${STORAGE_PREFIX}:${deps.userId() ?? "anon"}:score:${songId}`;

  function read<T>(key: string): T | null {
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : null;
    } catch {
      return null;
    }
  }

  function write(key: string, value: unknown): void {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      // Most likely the storage quota. The stub then simply forgets the change.
      console.warn("Songs stub could not save", key, error);
    }
  }

  function loadSong(songId: string): StoredSong | null {
    const stored = read<StoredSong>(songKey(songId));
    return stored && !stored.deleted ? stored : null;
  }

  function requireSong(songId: string): StoredSong {
    const stored = loadSong(songId);
    if (!stored) throw deps.makeError("This song isn't available.", 404, "song_not_found");
    return stored;
  }

  function allSongs(): StoredSong[] {
    const prefix = `${STORAGE_PREFIX}:${deps.userId() ?? "anon"}:song:`;
    const songs: StoredSong[] = [];
    try {
      for (let index = 0; index < window.localStorage.length; index += 1) {
        const key = window.localStorage.key(index);
        if (!key?.startsWith(prefix)) continue;
        const stored = read<StoredSong>(key);
        if (stored && !stored.deleted) songs.push(stored);
      }
    } catch {
      // Storage unavailable: no Songs.
    }
    return songs;
  }

  /** The stub's `record_take`: the player holds the latest take of each part. */
  function recordTake(stored: StoredSong, job: StoredJob, payload: ProgressResponse): void {
    const track = payload.audio_track;
    if (!track || !payload.audio_url) return;
    const player = stored.player;
    if (payload.score_id && player.score_id && payload.score_id !== player.score_id) return;
    const partId = typeof track.part_id === "string" ? track.part_id.trim() : "";
    const partIndex = typeof track.part_index === "number" ? track.part_index : null;
    const key =
      (typeof track.key === "string" && track.key.trim()) ||
      (partId ? `id:${partId}` : partIndex !== null ? `index:${partIndex}` : "");
    if (!key) return;
    const repeats = Boolean(payload.expand_repeats);
    if (repeats !== player.repeats) {
      player.vocals = [];
      player.repeats = repeats;
    }
    const entry: StoredVocal = {
      key,
      part_id: partId || null,
      part_index: partIndex,
      label: (typeof track.label === "string" && track.label.trim()) || (partId ? `Part ${partId}` : "Vocal"),
      verse_number: track.verse_number ?? null,
      take_signature: track.take_signature ?? null,
      source_job_id: job.job_id,
      expand_repeats: repeats,
      duration_seconds: payload.actual_duration_seconds ?? null,
      voice_name: null,
    };
    const existingIndex = player.vocals.findIndex((vocal) => vocal.key === key);
    if (existingIndex >= 0) player.vocals[existingIndex] = entry;
    else player.vocals.push(entry);
    player.revision += 1;
  }

  /**
   * Asks the real backend how each job stands and applies the workspace's job
   * rule: running jobs of any kind, and ended takes that belong to a chat turn.
   * `refreshEnded` also re-reads ended takes, for fresh audio links.
   */
  async function resolveJobs(
    stored: StoredSong,
    refreshEnded: boolean
  ): Promise<{ jobs: WorkspaceJob[]; changed: boolean }> {
    let changed = false;
    const resolved = await Promise.all(
      stored.jobs.map(async (job) => {
        if (job.ended === "dropped" || (job.ended && !refreshEnded)) return null;
        try {
          return await deps.fetchProgress(job.progress_url);
        } catch {
          return null;
        }
      })
    );
    const jobs: WorkspaceJob[] = [];
    stored.jobs.forEach((job, index) => {
      const payload = resolved[index];
      if (!payload || (payload.job_id && payload.job_id !== job.job_id)) return;
      const kind = payload.job_kind ?? job.job_kind ?? "synthesis";
      const workspaceJob: WorkspaceJob = {
        ...payload,
        job_id: job.job_id,
        job_kind: kind,
        originating_turn_id: job.originating_turn_id,
        created_at: job.created_at,
        progress_url: job.progress_url,
      };
      if (payload.status === "queued" || payload.status === "running") {
        jobs.push(workspaceJob);
        return;
      }
      const isTake = kind === "synthesis" && Boolean(job.originating_turn_id);
      if (payload.status === "done" && isTake) {
        if (!job.ended) {
          recordTake(stored, job, payload);
          job.ended = "done";
          changed = true;
        }
        jobs.push(workspaceJob);
      } else if (payload.status === "error" && isTake) {
        if (!job.ended) {
          job.ended = "failed";
          changed = true;
        }
        jobs.push(workspaceJob);
      } else if (!job.ended && payload.status !== "idle") {
        // An ended split, a stopped take, an ended export mix: the chat text
        // already says what happened.
        job.ended = "dropped";
        changed = true;
      }
    });
    return { jobs, changed };
  }

  function usableVocals(
    stored: StoredSong,
    score: StoredScore | null,
    jobs: WorkspaceJob[]
  ): WorkspaceVocal[] {
    if (stored.player.score_id !== stored.score_id) return [];
    const currentSignatures = new Map(
      (score?.score_summary?.parts ?? []).flatMap((part) =>
        part.part_id && part.take_signature ? [[part.part_id, part.take_signature] as const] : []
      )
    );
    return stored.player.vocals.flatMap((vocal) => {
      const job = jobs.find((candidate) => candidate.job_id === vocal.source_job_id);
      if (!job?.audio_url) return [];
      const current = vocal.part_id ? currentSignatures.get(vocal.part_id) : undefined;
      if (vocal.take_signature && current && vocal.take_signature !== current) return [];
      const mix = stored.player.vocal_mix[vocal.key] ?? { muted: false, solo: false, volume: 1 };
      return [{ ...vocal, ...mix, audio_url: job.audio_url, progress_url: job.progress_url }];
    });
  }

  function toRow(stored: StoredSong, working: boolean, trackCount: number): SongRow {
    return {
      id: stored.song.id,
      title: stored.song.title,
      created_at: stored.song.created_at,
      last_active_at: stored.song.last_active_at,
      status: working ? "working" : "idle",
      track_count: trackCount,
    };
  }

  async function rowFor(stored: StoredSong): Promise<SongRow> {
    const { jobs, changed } = await resolveJobs(stored, false);
    if (changed) write(songKey(stored.song.id), stored);
    const working = jobs.some((job) => job.status === "queued" || job.status === "running");
    const trackCount = stored.player.score_id === stored.score_id ? stored.player.vocals.length : 0;
    return toRow(stored, working, trackCount);
  }

  return {
    async uploadScore(sessionId, file, options = {}) {
      if (options.replace) {
        const stop = await deps.stopSessionWork(sessionId);
        if (stop.result === "too_late" || stop.result === "not_stoppable") {
          throw deps.makeError(
            "The current job is finishing and can't be stopped.",
            409,
            "stop_too_late"
          );
        }
      }
      let response: UploadResponse | null = null;
      for (let attempt = 0; response === null; attempt += 1) {
        try {
          // Today's backend has no `replace` field; the stub has stopped the work itself.
          response = await deps.uploadScore(sessionId, file);
        } catch (error) {
          const stillStopping =
            options.replace &&
            deps.errorCode(error) === "work_running" &&
            attempt < REPLACE_UPLOAD_RETRIES;
          if (!stillStopping) throw error;
          await wait(REPLACE_UPLOAD_RETRY_MS);
        }
      }
      const now = new Date().toISOString();
      const scoreId = response.score_id ?? null;
      const summary = response.score_summary ?? null;
      const previous = loadSong(sessionId);
      const title =
        previous?.song.title_edited ? previous.song.title : songTitleFrom(summary, file.name);
      const song: StoredSong["song"] = {
        id: sessionId,
        title,
        title_edited: previous?.song.title_edited ?? false,
        created_at: previous?.song.created_at ?? now,
        last_active_at: now,
      };
      write(songKey(sessionId), {
        song,
        deleted: false,
        score_id: scoreId,
        messages: previous?.messages ?? [],
        // Jobs of the replaced score were stopped, or ended before the upload was let through.
        jobs: [],
        player: emptyPlayer(scoreId),
      } satisfies StoredSong);
      const performanceMidi = response.performance_midi ?? summary?.performance_midi;
      write(scoreKey(sessionId), {
        score_id: scoreId,
        current_score: response.current_score,
        score_summary: summary && performanceMidi ? { ...summary, performance_midi: performanceMidi } : summary,
        solfege_settings: response.solfege_settings,
      } satisfies StoredScore);
      return {
        ...response,
        song: {
          id: song.id,
          title: song.title,
          title_edited: song.title_edited,
          created_at: song.created_at,
        },
      };
    },

    async fetchWorkspace(songId) {
      const stored = requireSong(songId);
      const score = read<StoredScore>(scoreKey(songId));
      const { jobs, changed } = await resolveJobs(stored, true);
      if (changed) write(songKey(songId), stored);
      return {
        session_id: songId,
        song: { ...stored.song },
        score: {
          score_id: stored.score_id,
          current_score: score?.current_score,
          score_summary: score?.score_summary ?? null,
          solfege_settings: score?.solfege_settings,
        },
        messages: stored.messages,
        jobs,
        player: {
          score_id: stored.player.score_id,
          revision: stored.player.revision,
          repeats: stored.player.repeats,
          vocals: usableVocals(stored, score, jobs),
          instruments: stored.player.instruments,
          bus: stored.player.bus,
        },
      };
    },

    async listSongs(limit) {
      const songs = allSongs()
        .sort((a, b) => b.song.last_active_at.localeCompare(a.song.last_active_at))
        .slice(0, Math.max(1, Math.min(limit, 200)));
      return Promise.all(songs.map(rowFor));
    },

    async renameSong(songId, rawTitle) {
      const stored = requireSong(songId);
      const title = rawTitle.trim();
      if (title.length < 1 || title.length > TITLE_MAX_LENGTH) {
        throw deps.makeError("A title needs 1 to 120 characters.", 400, "invalid_title");
      }
      stored.song.title = title;
      stored.song.title_edited = true;
      write(songKey(songId), stored);
      return rowFor(stored);
    },

    async deleteSong(songId) {
      const stored = requireSong(songId);
      const row = await rowFor(stored);
      if (row.status === "working") {
        throw deps.makeError("Stop the running work before deleting this song.", 409, "work_running");
      }
      stored.deleted = true;
      write(songKey(songId), stored);
      try {
        window.localStorage.removeItem(scoreKey(songId));
      } catch {
        // Nothing to free.
      }
    },

    async savePlayerMix(songId, mix) {
      const stored = requireSong(songId);
      if (mix.score_id !== stored.score_id) {
        throw deps.makeError("The score changed since this mix was made.", 409, "score_changed");
      }
      const volumeOk = (volume: number, max: number) =>
        Number.isFinite(volume) && volume >= 0 && volume <= max;
      const valid =
        mix.vocals.length <= 64 &&
        mix.instruments.length <= 64 &&
        mix.vocals.every((vocal) => volumeOk(vocal.volume, 1.2)) &&
        mix.instruments.every(
          (row) =>
            volumeOk(row.volume, 1) &&
            (row.gm_program === null || (row.gm_program >= 0 && row.gm_program <= 127)) &&
            (row.soundfont_bank === null || (row.soundfont_bank >= 0 && row.soundfont_bank <= 16383))
        ) &&
        mix.bus.level_db >= -48 &&
        mix.bus.level_db <= 6;
      if (!valid) throw deps.makeError("That mix can't be saved.", 400, "invalid_mix");
      const player =
        stored.player.score_id === stored.score_id ? stored.player : emptyPlayer(stored.score_id);
      // The server ignores a key it has no take for. The stub may not have seen
      // the take end yet, so it keeps the values until it does.
      for (const vocal of mix.vocals) {
        player.vocal_mix[vocal.key] = { muted: vocal.muted, solo: vocal.solo, volume: vocal.volume };
      }
      player.instruments = mix.instruments;
      player.bus = mix.bus;
      player.revision += 1;
      stored.player = player;
      write(songKey(songId), stored);
      return { revision: player.revision };
    },

    recordState(songId, state) {
      const stored = loadSong(songId);
      if (!stored || state.score_id !== stored.score_id) return;
      const now = new Date().toISOString();
      const before = JSON.stringify([stored.messages, stored.jobs]);
      const timestamps = new Map(stored.messages.map((message) => [message.id, message.timestamp]));
      stored.messages = state.messages.map((message) => ({
        ...message,
        timestamp: timestamps.get(message.id) ?? now,
      }));
      const known = new Map(stored.jobs.map((job) => [job.job_id, job]));
      for (const job of state.jobs) {
        const existing = known.get(job.job_id);
        if (existing) {
          existing.job_kind = job.job_kind ?? existing.job_kind;
          continue;
        }
        stored.jobs.push({
          job_id: job.job_id,
          job_kind: job.job_kind ?? null,
          progress_url: job.progress_url,
          originating_turn_id: job.originating_turn_id,
          created_at: now,
        });
      }
      if (JSON.stringify([stored.messages, stored.jobs]) !== before) {
        stored.song.last_active_at = now;
        write(songKey(songId), stored);
      }
      const score = read<StoredScore>(scoreKey(songId));
      const summary =
        state.score_summary && state.performance_midi
          ? { ...state.score_summary, performance_midi: state.performance_midi }
          : state.score_summary;
      const nextScore: StoredScore = {
        score_id: state.score_id,
        current_score: score?.current_score,
        score_summary: summary,
        solfege_settings: state.solfege_settings
          ? { revision: score?.solfege_settings?.revision ?? 0, ...state.solfege_settings }
          : score?.solfege_settings,
      };
      if (JSON.stringify(nextScore) !== JSON.stringify(score)) write(scoreKey(songId), nextScore);
    },
  };
}
