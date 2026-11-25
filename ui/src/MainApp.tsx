import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { createPerformanceMeasureLookup, positionNativeScoreCursor } from "./scorePlayback";
import WaveSurfer from "wavesurfer.js";
import type { ToneAdapter } from "@waveform-playlist/playout";
import {
  MediaRecorder as ExtendableMediaRecorder,
  register as registerExtendableMediaRecorderEncoder,
} from "extendable-media-recorder";
import { connect as connectExtendableWavEncoder } from "extendable-media-recorder-wav-encoder";
import { Upload, Send, Sparkles, Minus, Plus, Download, Printer, ChevronsUpDown, ListCollapse, PanelLeftClose, PanelLeftOpen, Check, X, Music2, Play, Pause, Repeat, Square, Mic, Volume2, VolumeX, GripVertical, Sliders, ListMusic, FilePlus, Pencil, Share2, Info } from "lucide-react";
import { createPortal } from "react-dom";
import { Link, useLocation, useMatch, useNavigate } from "react-router-dom";
import clsx from "clsx";
import {
  ApiError,
  chat,
  createSession,
  exportMix,
  fetchActiveSingAll,
  fetchInstrumentalMidi,
  fetchScoreXml,
  fetchSingAllJob,
  fetchProgress,
  fetchSynthesisEstimate,
  fetchSolfegeSettings,
  fetchVoicebanks,
  fetchWorkspace,
  markFeedbackPrompted,
  renameSong,
  savePlayerMix,
  SYNTHESIS_JOB_DETAILS_TIMEOUT_BODY,
  SYNTHESIS_JOB_DETAILS_TIMEOUT_MESSAGE,
  SYNTHESIS_JOB_DETAILS_TIMEOUT_TITLE,
  submitAudioFeedback,
  stopSessionWork,
  uploadScore,
  updateSolfegeSettings,
  type AudioTrackMetadata,
  type ChatStream,
  type FeedbackPromptState,
  type FeedbackRatingsRequest,
  type PerformanceMidi,
  type PerformanceMeasureMapEntry,
  type PlayerMixRequest,
  type ProgressResponse,
  type ScoreSummary,
  type SingAllOpen,
  type SingAllProgress,
  type SolfegeMode,
  type SolfegeSystem,
  type SongInfo,
  type SongWorkspace,
  type SynthesisCreditEstimate,
  type VoicebankOption,
  type WorkspaceInstrument,
  type WorkspaceJob,
  type WorkspaceMessage,
  type WorkspaceVocal,
} from "./api";
import {
  DEFAULT_INSTRUMENTAL_BUS,
  INSTRUMENTAL_BUS_MAX_DB,
  INSTRUMENTAL_BUS_MIN_DB,
  decodeVocalAudio,
  hasAudibleScorePlayerMix,
  instrumentRowsFromMidi,
  instrumentalTrackKey,
  shouldMuteMultiTrackForPlayback,
  shownVocals,
  vocalsInBothModes as hasVocalsInBothModes,
  type InstrumentalBusState,
  type InstrumentalTrackState,
  type MultiTrackAudioTrack,
  type VocalMode,
} from "./player/tracks";
import {
  HorizontalScoreRenderer,
  HorizontalViewIcon,
  PageViewIcon,
  applyPageLayoutZoom,
  countParallelScoreStaves,
  fitPagePreviewToContainer,
  getHorizontalScoreIncrementalBatchSize,
  preparePagePreviewWrappers,
  scrollScoreMeasureIntoView,
  type HorizontalScoreRendererHandle,
  type ScorePreviewLayout,
} from "./player/score";
import { ScorePlayerEngine, withoutKeysOutside, type ScorePlayerPlaybackControls } from "./player/ScorePlayerEngine";
import CreditsHeader from "./components/CreditsHeader";
import SingAllCard, { SingAllDownloadButton } from "./components/SingAllCard";
import { noticeInBackground } from "./tabNotice";
import { shareDialogOpen, singAllStop, singAllTabReturn, songInfoOpened } from "./analytics";
import { UserMenu } from "./components/UserMenu";
import SongsDrawer from "./components/SongsDrawer";
import NewSongOrReplaceModal from "./components/NewSongOrReplaceModal";
import ShareSongModal, { shareBlockedReason, type ShareState } from "./share/ShareSongModal";
import SongInfoPanel from "./components/SongInfoPanel";
import { logAnalyticsEvent } from "./firebase";
import { useCredits } from "./hooks/useCredits";
import { useBillingState } from "./hooks/useBillingState";
import { useAuth } from "./hooks/useAuth";
import { useAnnouncements } from "./hooks/useAnnouncements";
import { WaitlistModal } from "./components/WaitlistModal";
import AnnouncementModal from "./components/AnnouncementModal";
import FeatureTour from "./components/FeatureTour";
import type { WaitlistSource } from "./components/WaitingListForm";
import {
  BillingPaywallModal,
  type PaywallTrigger,
} from "./components/billing/BillingPaywallModal";
import {
  clearPendingBillingPortalSync,
  hasPendingBillingPortalSync,
  startCheckout,
  startBillingPortal,
  syncBillingSubscription,
  syncCheckoutSession,
} from "./billing/api";
import {
  clearPendingCheckoutPlan,
  getStoredPendingCheckoutPlan,
  isPaidPlanKey,
  type BillingPlanKey,
} from "./billing/plans";

const SCORE_PREVIEW_RENDER_ERROR =
  "This score was uploaded, but its notation data looks malformed and cannot be rendered in the preview.";
const BROWSER_MIX_BOUNCE_MAX_DURATION_SECONDS = 5 * 60;
const BROWSER_MIX_RECORDING_MIME_TYPE = "audio/wav";
const STOP_TOO_LATE_NOTICE =
  "The current job is just finishing and can't be stopped. You can choose again when it ends.";
const REPEATS_UNAVAILABLE_MESSAGE =
  "Repeats: unavailable. This score's repeat marks can't be followed, so it sings in written order";

const traceBrowserMixBounce = (stage: string, details: Record<string, unknown> = {}) => {
  if (import.meta.env.DEV) {
    console.info("[browser-mix-bounce]", stage, details);
  }
};

let extendableWavRegistration: Promise<void> | null = null;

const ensureExtendableWavRecorderRegistered = (): Promise<void> => {
  extendableWavRegistration ??= (async () => {
    await registerExtendableMediaRecorderEncoder(await connectExtendableWavEncoder());
  })();
  return extendableWavRegistration;
};

// The Sing All hint's chat message (UI prototype: the chat reply that opens the card is faked).
const SING_ALL_PROMPT = "Sing All";
const STARTING_CONVERSATIONS = [
  "sing the vocal part, verse 1",
  "sing the soprano part",
  "sing the alto part in solfege / solfa",
] as const;

type DemoSong = {
  id: string;
  title: string;
  detail: string;
  /** Served from ui/public, so it loads through the same upload path as a user's file. */
  file: string;
  /** Starter prompts that name this song's real parts and verses. */
  prompts: readonly string[];
};

const DEMO_SONGS: readonly DemoSong[] = [
  {
    id: "amazing-grace",
    title: "Amazing Grace",
    detail: "SATB choir + piano · 2 verses",
    file: "/demo-scores/amazing-grace.xml",
    prompts: [
      "sing the soprano part, verse 1",
      "sing the alto part in solfege",
      "sing the bass part, verse 2",
    ],
  },
  {
    id: "happy-birthday",
    title: "Happy Birthday",
    detail: "Alto & men + piano",
    file: "/demo-scores/happy-birthday.xml",
    prompts: [
      "sing happy birthday to Sarah",
      "sing the men's part",
      "add solfege to all parts",
    ],
  },
];

const PLAYBACK_TOKEN_REFRESH_MARGIN_MS = 5_000;
// The billable server-side export mix is retired: it mixed vocal tracks only,
// while the real-time browser mix also includes the instrumental tracks. The
// backend endpoint is disabled by default too; the code stays for re-enabling.
const BILLABLE_SERVER_EXPORT_MIX_ENABLED = false;
const SYNTHESIS_STREAM_LIVENESS_TIMEOUT_MS = 35_000;
const SYNTHESIS_STREAM_ERROR_POLL_ATTEMPTS = 3;
const PROGRESS_POLL_INTERVAL_MS = 1200;
// A job that is still running is polled less and less often, up to this.
const PROGRESS_POLL_MAX_INTERVAL_MS = 5_000;
// How long failed progress polls are retried before polling gives up. The
// instance running a job can be replaced mid-job; the replacement takes about
// 25 s to start, and the job record it serves then holds the outcome.
const PROGRESS_POLL_FAILURE_RETRY_MS = 60_000;
const PROGRESS_POLL_MAX_BACKOFF_MS = 10_000;

// Polls of a running job back off: 1.2 s, 2.4 s, 4.8 s, then every 5 s. A
// failed poll restarts that and retries on its own schedule, so every client
// polling a lost instance does not keep hitting a service that is short of
// capacity: 1.2 s, 2.4 s, 4.8 s, 9.6 s, then 10 s.
function progressPollDelayMs(successfulPolls: number, consecutiveFailures: number): number {
  if (consecutiveFailures === 0) {
    return Math.min(
      PROGRESS_POLL_INTERVAL_MS * 2 ** Math.max(0, successfulPolls - 1),
      PROGRESS_POLL_MAX_INTERVAL_MS
    );
  }
  return Math.min(
    PROGRESS_POLL_INTERVAL_MS * 2 ** (consecutiveFailures - 1),
    PROGRESS_POLL_MAX_BACKOFF_MS
  );
}
const SYNTHESIS_STREAM_RECONNECTING_MESSAGE =
  "Connection lost. Reconnecting to check your take";
const SYNTHESIS_STATUS_UNCONFIRMED_MESSAGE =
  "We couldn’t confirm the final job status. Checking for updates…";

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
  audioTrack?: AudioTrackMetadata;
  details?: unknown;
  attemptMessages?: AttemptMessage[];
  progressUrl?: string;
  isProgress?: boolean;
  progressValue?: number;
  jobId?: string;
  feedback?: FeedbackPromptState;
  // The chat model opened Sing All in this reply: the bubble shows the quote card.
  singAll?: SingAllOpen;
  // The Sing All job this bubble's card follows: set at "Sing it", or from the history.
  singAllJobId?: string;
};

type AttemptMessage = {
  attempt_number: number;
  message?: string;
  thought_summary?: string;
};

type ScorePayload = {
  name: string;
  data: string;
};

const playbackTokenExpiresAt = (audioUrl: string): number | null => {
  try {
    const token = new URL(audioUrl, window.location.origin).searchParams.get("playback_token");
    const payloadPart = token?.split(".", 1)[0];
    if (!payloadPart) return null;
    const base64 = payloadPart.replace(/-/g, "+").replace(/_/g, "/");
    const padded = `${base64}${"=".repeat((4 - (base64.length % 4)) % 4)}`;
    const payload = JSON.parse(window.atob(padded)) as { exp?: unknown };
    return typeof payload.exp === "number" && Number.isFinite(payload.exp)
      ? payload.exp * 1_000
      : null;
  } catch {
    return null;
  }
};

const isPlaybackTokenStale = (audioUrl: string): boolean => {
  const expiresAt = playbackTokenExpiresAt(audioUrl);
  return expiresAt !== null && expiresAt <= Date.now() + PLAYBACK_TOKEN_REFRESH_MARGIN_MS;
};

const formatDb = (db: number) => `${Number.isInteger(db) ? db : db.toFixed(1)} dB`;

const GM_INSTRUMENTS = [
  { program: 0, label: "Acoustic Grand Piano" },
  { program: 1, label: "Bright Acoustic Piano" },
  { program: 2, label: "Electric Grand Piano" },
  { program: 3, label: "Honky-tonk Piano" },
  { program: 4, label: "Electric Piano (Rhodes)" },
  { program: 5, label: "Electric Piano (DX7)" },
  { program: 6, label: "Harpsichord" },
  { program: 7, label: "Clavinet" },
  { program: 8, label: "Celesta" },
  { program: 9, label: "Glockenspiel" },
  { program: 10, label: "Music Box" },
  { program: 11, label: "Vibraphone" },
  { program: 12, label: "Marimba" },
  { program: 13, label: "Xylophone" },
  { program: 14, label: "Tubular Bells" },
  { program: 15, label: "Dulcimer" },
  { program: 16, label: "Drawbar Organ" },
  { program: 17, label: "Percussive Organ" },
  { program: 18, label: "Rock Organ" },
  { program: 19, label: "Church Organ" },
  { program: 20, label: "Reed Organ" },
  { program: 21, label: "Accordion" },
  { program: 22, label: "Harmonica" },
  { program: 23, label: "Tango Accordion" },
  { program: 24, label: "Acoustic Guitar (Nylon)" },
  { program: 25, label: "Acoustic Guitar (Steel)" },
  { program: 26, label: "Electric Guitar (Jazz)" },
  { program: 27, label: "Electric Guitar (Clean)" },
  { program: 28, label: "Electric Guitar (Muted)" },
  { program: 29, label: "Overdriven Guitar" },
  { program: 30, label: "Distortion Guitar" },
  { program: 31, label: "Guitar Harmonics" },
  { program: 32, label: "Acoustic Bass" },
  { program: 33, label: "Electric Bass (Finger)" },
  { program: 34, label: "Electric Bass (Pick)" },
  { program: 35, label: "Fretless Bass" },
  { program: 36, label: "Slap Bass 1" },
  { program: 37, label: "Slap Bass 2" },
  { program: 38, label: "Synth Bass 1" },
  { program: 39, label: "Synth Bass 2" },
  { program: 40, label: "Violin" },
  { program: 41, label: "Viola" },
  { program: 42, label: "Cello" },
  { program: 43, label: "Contrabass" },
  { program: 44, label: "Tremolo Strings" },
  { program: 45, label: "Pizzicato Strings" },
  { program: 46, label: "Orchestral Harp" },
  { program: 47, label: "Timpani" },
  { program: 48, label: "String Ensemble 1" },
  { program: 49, label: "String Ensemble 2" },
  { program: 50, label: "Synth Strings 1" },
  { program: 51, label: "Synth Strings 2" },
  { program: 52, label: "Choir Aahs" },
  { program: 53, label: "Voice Oohs" },
  { program: 54, label: "Synth Voice" },
  { program: 55, label: "Orchestra Hit" },
  { program: 56, label: "Trumpet" },
  { program: 57, label: "Trombone" },
  { program: 58, label: "Tuba" },
  { program: 59, label: "Muted Trumpet" },
  { program: 60, label: "French Horn" },
  { program: 61, label: "Brass Section" },
  { program: 62, label: "Synth Brass 1" },
  { program: 63, label: "Synth Brass 2" },
  { program: 64, label: "Soprano Sax" },
  { program: 65, label: "Alto Sax" },
  { program: 66, label: "Tenor Sax" },
  { program: 67, label: "Baritone Sax" },
  { program: 68, label: "Oboe" },
  { program: 69, label: "English Horn" },
  { program: 70, label: "Bassoon" },
  { program: 71, label: "Clarinet" },
  { program: 72, label: "Piccolo" },
  { program: 73, label: "Flute" },
  { program: 74, label: "Recorder" },
  { program: 75, label: "Pan Flute" },
  { program: 76, label: "Blown Bottle" },
  { program: 77, label: "Shakuhachi" },
  { program: 78, label: "Whistle" },
  { program: 79, label: "Ocarina" },
] as const;

type GmInstrumentOption = {
  program: number;
  label: string;
};

type GmInstrumentGroup = {
  id: string;
  label: string;
  instruments: readonly GmInstrumentOption[];
};

const GM_INSTRUMENT_GROUPS: readonly GmInstrumentGroup[] = [
  // These are the official General MIDI Level 1 program families. The UI
  // currently exposes the first 80 programs, through the Pipe family.
  { id: "piano", label: "Piano", instruments: GM_INSTRUMENTS.slice(0, 8) },
  { id: "chromatic-percussion", label: "Chromatic Percussion", instruments: GM_INSTRUMENTS.slice(8, 16) },
  { id: "organ", label: "Organ", instruments: GM_INSTRUMENTS.slice(16, 24) },
  { id: "guitar", label: "Guitar", instruments: GM_INSTRUMENTS.slice(24, 32) },
  { id: "bass", label: "Bass", instruments: GM_INSTRUMENTS.slice(32, 40) },
  { id: "strings", label: "Strings", instruments: GM_INSTRUMENTS.slice(40, 48) },
  { id: "ensemble", label: "Ensemble", instruments: GM_INSTRUMENTS.slice(48, 56) },
  { id: "brass", label: "Brass", instruments: GM_INSTRUMENTS.slice(56, 64) },
  { id: "reed", label: "Reed", instruments: GM_INSTRUMENTS.slice(64, 72) },
  { id: "pipe", label: "Pipe", instruments: GM_INSTRUMENTS.slice(72, 80) },
];

// GM Level 1 specifies a single note-based percussion map on channel 10;
// alternate kits require a non-GM1 extension (for example GM2, GS, or XG).
const GM_PERCUSSION_GROUPS: readonly GmInstrumentGroup[] = [
  {
    id: "percussion",
    label: "Percussion",
    instruments: [{ program: 0, label: "Standard GM Percussion" }],
  },
];

const FLUIDR3_PERCUSSION_GROUPS: readonly GmInstrumentGroup[] = [
  {
    id: "fluidr3-percussion",
    label: "FluidR3 Drum Kits",
    instruments: [
      { program: 0, label: "Standard Kit" },
      { program: 8, label: "Room Kit" },
      { program: 16, label: "Power Kit" },
      { program: 24, label: "Electronic Kit" },
      { program: 25, label: "TR-808 Kit" },
      { program: 32, label: "Jazz Kit" },
      { program: 40, label: "Brush Kit" },
      { program: 48, label: "Orchestra Kit" },
    ],
  },
];

type GroupedInstrumentPickerProps = {
  label: string;
  gmProgram: number;
  percussion?: boolean;
  soundfontBank?: number;
  presetKind?: "melodic" | "percussion_kit";
  unassigned?: boolean;
  onChange: (gmProgram: number) => void;
};

const GroupedInstrumentPicker = ({
  label,
  gmProgram,
  percussion = false,
  soundfontBank,
  presetKind,
  unassigned = false,
  onChange,
}: GroupedInstrumentPickerProps) => {
  const groups = percussion
    // The player uses FluidR3's bank 128 for every percussion route.  The
    // provider patch in patches/@waveform-playlist+playout+*.patch makes its
    // SoundFont track honour this program number on MIDI channel 10.
    ? soundfontBank === 128 && presetKind === "percussion_kit"
      ? FLUIDR3_PERCUSSION_GROUPS
      : GM_PERCUSSION_GROUPS
    : GM_INSTRUMENT_GROUPS;
  const selectedGroup =
    groups.find((group) => group.instruments.some((instrument) => instrument.program === gmProgram)) ??
    groups[0];
  const selectedInstrument =
    selectedGroup.instruments.find((instrument) => instrument.program === gmProgram) ??
    selectedGroup.instruments[0];
  const [open, setOpen] = useState(false);
  const [openGroupId, setOpenGroupId] = useState<string | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPosition, setMenuPosition] = useState<CSSProperties | null>(null);
  const openGroup = groups.find((group) => group.id === openGroupId) ?? null;

  useEffect(() => {
    if (!open) {
      setOpenGroupId(null);
    }
  }, [open]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        pickerRef.current &&
        !pickerRef.current.contains(target) &&
        !menuRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  useEffect(() => {
    if (!open || !triggerRef.current) {
      setMenuPosition(null);
      return;
    }

    const updateMenuPosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const viewportPadding = 12;
      const gap = 6;
      const menuWidth = Math.min(250, window.innerWidth - viewportPadding * 2);
      const spaceAbove = rect.top - viewportPadding;
      const spaceBelow = window.innerHeight - rect.bottom - viewportPadding;
      const openUpward = spaceAbove >= spaceBelow;
      const availableHeight = Math.max(
        120,
        Math.min(260, (openUpward ? spaceAbove : spaceBelow) - gap)
      );
      const left = Math.max(
        viewportPadding,
        Math.min(rect.left, window.innerWidth - menuWidth - viewportPadding)
      );
      setMenuPosition(
        openUpward
          ? { left, bottom: window.innerHeight - rect.top + gap, width: menuWidth, maxHeight: availableHeight }
          : { left, top: rect.bottom + gap, width: menuWidth, maxHeight: availableHeight }
      );
    };

    updateMenuPosition();
    window.addEventListener("resize", updateMenuPosition);
    document.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateMenuPosition);
      document.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [open]);

  const pickerMenu = open && menuPosition ? (
    <div
      className="score-track-instrument-menu"
      ref={menuRef}
      style={menuPosition}
      role="menu"
      aria-label={`${label} sound selection`}
    >
      {openGroup ? (
        <>
          <button
            type="button"
            className="score-track-instrument-menu-back"
            onClick={() => setOpenGroupId(null)}
            role="menuitem"
          >
            <span aria-hidden="true">‹</span> {openGroup.label}
          </button>
          <div className="score-track-instrument-menu-options">
            {openGroup.instruments.map((instrument) => (
              <button
                key={instrument.program}
                type="button"
                className={clsx("score-track-instrument-menu-option", {
                  selected: instrument.program === gmProgram,
                })}
                onClick={() => {
                  onChange(instrument.program);
                  setOpen(false);
                }}
                role="menuitemradio"
                aria-checked={instrument.program === gmProgram}
              >
                <span>{instrument.label}</span>
                {instrument.program === gmProgram && <Check size={13} aria-hidden="true" />}
              </button>
            ))}
          </div>
        </>
      ) : (
        <div className="score-track-instrument-menu-options">
          {groups.map((group) => (
            <button
              key={group.id}
              type="button"
              className="score-track-instrument-menu-option score-track-instrument-menu-group"
              onClick={() => setOpenGroupId(group.id)}
              role="menuitem"
            >
              <span>{group.label}</span>
              <span className="score-track-instrument-menu-arrow" aria-hidden="true">›</span>
            </button>
          ))}
        </div>
      )}
    </div>
  ) : null;

  return (
    <div className="score-track-instrument-picker" ref={pickerRef}>
      <button
        ref={triggerRef}
        type="button"
        className="score-track-instrument-select score-track-instrument-trigger"
        onClick={() => setOpen((current) => !current)}
        aria-label={`${label} ${percussion ? "percussion kit" : "instrument sound"}`}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="score-track-instrument-label">{unassigned ? (percussion ? "Choose a kit" : "Choose an instrument") : selectedInstrument.label}</span>
        <ChevronsUpDown size={13} aria-hidden="true" />
      </button>
      {typeof document !== "undefined" && pickerMenu && createPortal(pickerMenu, document.body)}
    </div>
  );
};

type BrowserMixBouncePhase = "idle" | "preparing" | "recording" | "finalizing";

type BrowserMixRecorder = {
  state: "inactive" | "recording" | "paused";
  start: () => void;
  stop: () => void;
  addEventListener: (type: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions) => void;
  removeEventListener: (type: string, listener: EventListenerOrEventListenerObject) => void;
};

type BrowserMixBounceOperation = {
  phase: Exclude<BrowserMixBouncePhase, "idle">;
  adapter: ToneAdapter;
  destination: MediaStreamAudioDestinationNode;
  recorder: BrowserMixRecorder;
  recordingMimeType: string;
  chunks: Blob[];
  discarded: boolean;
};

const selectAudibleMultiTrackTracks = (
  tracks: MultiTrackAudioTrack[]
): MultiTrackAudioTrack[] => {
  const hasSolo = tracks.some((track) => track.solo);
  return hasSolo ? tracks.filter((track) => track.solo) : tracks.filter((track) => !track.muted);
};

const isNoOpSingleTrackExport = (tracks: MultiTrackAudioTrack[]): boolean => {
  return tracks.length === 1;
};

const multiTrackAnalyticsParams = (tracks: MultiTrackAudioTrack[]) => {
  const soloTrackCount = tracks.filter((track) => track.solo).length;
  const mutedTrackCount = tracks.filter((track) => track.muted).length;
  const audibleTrackCount = soloTrackCount
    ? soloTrackCount
    : tracks.filter((track) => !track.muted).length;
  return {
    feature_area: "multitrack_player",
    track_count: tracks.length,
    audible_track_count: audibleTrackCount,
    solo_track_count: soloTrackCount,
    muted_track_count: mutedTrackCount,
  };
};

const synthesisAudioAnalyticsParams = (message: Message) => ({
  feature_area: "individual_synthesis_audio",
  has_audio_track_metadata: Boolean(message.audioTrack),
  has_part_id: Boolean(message.audioTrack?.part_id),
  has_verse_number: Boolean(message.audioTrack?.verse_number),
  has_feedback_prompt_candidate: Boolean(message.feedback?.promptCandidate),
});

const estimateExportMixCredits = (durationSeconds: number | null | undefined): number | null => {
  if (typeof durationSeconds !== "number" || !Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    return null;
  }
  return Math.max(1, Math.ceil(durationSeconds / 60));
};

const wait = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

class AudioDownloadError extends Error {
  constructor(
    message: string,
    readonly status?: number
  ) {
    super(message);
  }
}

const isExpiredAudioDownloadError = (error: unknown): boolean =>
  error instanceof AudioDownloadError && (error.status === 401 || error.status === 403);

const downloadAudioUrl = async (audioUrl: string, fileName: string) => {
  const url = new URL(audioUrl, window.location.origin);
  url.searchParams.set("download", "1");
  const response = await fetch(url.toString());
  if (!response.ok) {
    throw new AudioDownloadError(`Download failed (${response.status}).`, response.status);
  }
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 0);
};

type MultiTrackWaveformLaneProps = {
  track: MultiTrackAudioTrack;
  index: number;
  onWaveSurferMount: (trackKey: string, instance: WaveSurfer) => void;
  onWaveSurferUnmount: (trackKey: string) => void;
  onTrackFinished: () => void;
  onTrackSeek: (trackKey: string, time: number) => void;
  onMuteChange: (trackKey: string, muted: boolean) => void;
  onSoloChange: (trackKey: string, solo: boolean) => void;
  onVolumeChange: (trackKey: string, volume: number) => void;
  onDownloadTrack: (track: MultiTrackAudioTrack) => void;
  onDurationChange: (trackKey: string, durationSeconds: number) => void;
};

const MultiTrackWaveformLane = ({
  track,
  index,
  onWaveSurferMount,
  onWaveSurferUnmount,
  onTrackFinished,
  onTrackSeek,
  onMuteChange,
  onSoloChange,
  onVolumeChange,
  onDownloadTrack,
  onDurationChange,
}: MultiTrackWaveformLaneProps) => {
  const waveformRef = useRef<HTMLDivElement | null>(null);
  const waveSurferRef = useRef<WaveSurfer | null>(null);

  useEffect(() => {
    if (!waveformRef.current) return;
    const waveSurfer = WaveSurfer.create({
      container: waveformRef.current,
      url: track.audioUrl,
      height: 36,
      waveColor: "rgba(125, 211, 252, 0.42)",
      progressColor: "rgba(183, 125, 255, 0.95)",
      cursorColor: "rgba(255, 255, 255, 0.95)",
      cursorWidth: 2,
      barWidth: 2,
      barGap: 3,
      barRadius: 2,
      normalize: true,
      dragToSeek: true,
    });
    waveSurferRef.current = waveSurfer;
    waveSurfer.setVolume(track.volume);
    waveSurfer.setMuted(track.muted);
    onWaveSurferMount(track.key, waveSurfer);

    const unsubscribeFinish = waveSurfer.on("finish", onTrackFinished);
    const unsubscribeInteraction = waveSurfer.on("interaction", (time) => {
      onTrackSeek(track.key, time);
    });
    const unsubscribeReady = waveSurfer.on("ready", () => {
      const duration = waveSurfer.getDuration();
      if (Number.isFinite(duration) && duration > 0) {
        onDurationChange(track.key, duration);
      }
    });

    return () => {
      unsubscribeReady();
      unsubscribeInteraction();
      unsubscribeFinish();
      onWaveSurferUnmount(track.key);
      waveSurfer.destroy();
      waveSurferRef.current = null;
    };
  }, [
    onTrackFinished,
    onTrackSeek,
    onDurationChange,
    onWaveSurferMount,
    onWaveSurferUnmount,
    track.audioUrl,
    track.key,
  ]);

  useEffect(() => {
    waveSurferRef.current?.setMuted(track.muted);
  }, [track.muted]);

  useEffect(() => {
    waveSurferRef.current?.setVolume(track.volume);
  }, [track.volume]);

  return (
    <div className="multitrack-lane">
      <div className="multitrack-lane-header">
        <span className="multitrack-lane-number">{index + 1}</span>
        <div className="multitrack-lane-title">
          <strong>{track.label}</strong>
          <span>
            {track.verseNumber ? `Verse ${track.verseNumber}` : "Generated vocal"}
            {track.jobId ? ` · ${track.jobId.slice(0, 8)}` : ""}
          </span>
        </div>
      </div>
      <div className="multitrack-clip">
        <div ref={waveformRef} className="multitrack-waveform" />
      </div>
      <div className="multitrack-lane-controls">
        <button
          type="button"
          className={clsx("multitrack-toggle", { active: track.muted })}
          onClick={() => onMuteChange(track.key, !track.muted)}
          aria-label={track.muted ? `Unmute ${track.label}` : `Mute ${track.label}`}
          title={track.muted ? "Unmute" : "Mute"}
        >
          M
        </button>
        <button
          type="button"
          className={clsx("multitrack-solo", { active: track.solo })}
          onClick={() => onSoloChange(track.key, !track.solo)}
          aria-label={track.solo ? `Disable solo for ${track.label}` : `Solo ${track.label}`}
          title={track.solo ? "Disable solo" : "Solo"}
        >
          S
        </button>
        <input
          className="multitrack-volume"
          type="range"
          min="0"
          max="1.2"
          step="0.01"
          value={track.volume}
          onChange={(event) => onVolumeChange(track.key, Number(event.target.value))}
          aria-label={`${track.label} volume`}
        />
        <button
          type="button"
          className="audio-download-button multitrack-track-download"
          onClick={() => onDownloadTrack(track)}
          aria-label={`Download ${track.label}`}
          title="Download"
        >
          <Download size={15} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
};

type ScoreTrackControlsPanelProps = {
  vocalTracks: MultiTrackAudioTrack[];
  instrumentalTracks: InstrumentalTrackState[];
  instrumentalBus: InstrumentalBusState;
  instrumentalTracksExpanded: boolean;
  disabled?: boolean;
  // Set when the Song has takes in both modes: the mode whose takes are listed.
  vocalModeShown?: VocalMode | null;
  onShowVocalMode?: (mode: VocalMode) => void;
  onUpdateVocalMute: (key: string, muted: boolean) => void;
  onUpdateVocalSolo: (key: string, solo: boolean) => void;
  onUpdateVocalVolume: (key: string, volume: number) => void;
  onDownloadVocalTrack: (track: MultiTrackAudioTrack) => void;
  onUpdateInstrumentalMute: (key: string, muted: boolean) => void;
  onUpdateInstrumentalSolo: (key: string, solo: boolean) => void;
  onUpdateInstrumentalVolume: (key: string, volume: number) => void;
  onUpdateInstrumentalGmProgram: (key: string, gmProgram: number) => void;
  onUpdateInstrumentalBusMute: (muted: boolean) => void;
  onUpdateInstrumentalBusSolo: (solo: boolean) => void;
  onUpdateInstrumentalBusLevel: (levelDb: number) => void;
  onToggleInstrumentalTracksExpanded: () => void;
  onCollapse: () => void;
};

const ScoreTrackControlsPanel = ({
  vocalTracks,
  instrumentalTracks,
  instrumentalBus,
  instrumentalTracksExpanded,
  disabled = false,
  vocalModeShown = null,
  onShowVocalMode,
  onUpdateVocalMute,
  onUpdateVocalSolo,
  onUpdateVocalVolume,
  onDownloadVocalTrack,
  onUpdateInstrumentalMute,
  onUpdateInstrumentalSolo,
  onUpdateInstrumentalVolume,
  onUpdateInstrumentalGmProgram,
  onUpdateInstrumentalBusMute,
  onUpdateInstrumentalBusSolo,
  onUpdateInstrumentalBusLevel,
  onToggleInstrumentalTracksExpanded,
  onCollapse,
}: ScoreTrackControlsPanelProps) => {
  const totalTracks = vocalTracks.length + instrumentalTracks.length;
  if (totalTracks === 0) return null;

  return (
    <div className="score-tracks-panel" aria-label="Score multitrack controls" data-testid="score-tracks-panel">
      <div className="score-tracks-header">
        <div className="score-tracks-header-title">
          <Sliders size={14} className="score-tracks-header-icon" />
          <span>Tracks & Stems</span>
        </div>
        <div className="score-tracks-header-actions">
          <span className="score-tracks-count-badge">
            {totalTracks} {totalTracks === 1 ? "track" : "tracks"}
          </span>
          <button
            type="button"
            className="panel-collapse-toggle score-tracks-collapse-toggle"
            onClick={onCollapse}
            aria-label="Collapse Tracks & Stems"
            title="Collapse Tracks & Stems"
          >
            <PanelLeftClose size={15} aria-hidden="true" />
          </button>
        </div>
      </div>

      <fieldset className="score-tracks-list score-tracks-list-fieldset" disabled={disabled}>
        {vocalModeShown && (
          <div className="solfege-segmented-control vocal-mode-switch" role="group" aria-label="Sung in">
            {([
              ["lyrics", "Lyrics"],
              ["solfege", "Solfège"],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={clsx({ selected: vocalModeShown === value })}
                aria-pressed={vocalModeShown === value}
                onClick={() => onShowVocalMode?.(value)}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {/* Audio / Vocal Tracks - TOP FIRST */}
        {vocalTracks.map((track) => (
          <div
            key={track.key}
            className={clsx("score-track-card score-track-row vocal-track", {
              "is-muted": track.muted,
              "is-soloed": track.solo,
            })}
            data-testid={`score-vocal-track-${track.key}`}
          >
            <div className="score-track-card-header">
              <div className="score-track-info">
                <span className="score-track-grip" aria-hidden="true" title="Audio stem">
                  <GripVertical size={13} />
                </span>
                <div className="score-track-icon-wrapper vocal" title="Vocal Audio Track">
                  <Mic size={14} />
                </div>
                <div className="score-track-details">
                  <span className="score-track-title" title={track.label}>
                    {track.label}
                  </span>
                  <div className="score-track-meta">
                    <span className="score-track-type-tag vocal">Vocal</span>
                    {track.verseNumber && (
                      <span className="score-track-tag">V{track.verseNumber}</span>
                    )}
                  </div>
                </div>
              </div>

              <div className="score-track-transport-btns">
                <button
                  type="button"
                  className={clsx("score-track-solo-btn", { active: track.solo })}
                  onClick={() => onUpdateVocalSolo(track.key, !track.solo)}
                  aria-pressed={track.solo}
                  title={track.solo ? "Unsolo track" : "Solo track"}
                >
                  S
                </button>
                <button
                  type="button"
                  className={clsx("score-track-mute-btn", { muted: track.muted })}
                  onClick={() => onUpdateVocalMute(track.key, !track.muted)}
                  aria-pressed={track.muted}
                  title={track.muted ? "Unmute track" : "Mute track"}
                >
                  {track.muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
                </button>
                <button
                  type="button"
                  className="score-track-action-btn"
                  onClick={() => onDownloadVocalTrack(track)}
                  title="Download vocal audio stem"
                  aria-label={`Download ${track.label} audio`}
                >
                  <Download size={13} />
                </button>
              </div>
            </div>

            <div className="score-track-card-footer">
              <div className="score-track-volume-wrapper" title={`Volume: ${Math.round(track.volume * 100)}%`}>
                <span className="score-track-volume-icon">
                  <Volume2 size={12} />
                </span>
                <input
                  type="range"
                  className="score-track-volume-slider"
                  min="0"
                  max="1.2"
                  step="0.01"
                  value={track.volume}
                  onChange={(e) => onUpdateVocalVolume(track.key, parseFloat(e.target.value))}
                  aria-label={`${track.label} volume`}
                />
                <span className="score-track-volume-label">{Math.round(track.volume * 100)}%</span>
              </div>
            </div>
          </div>
        ))}

        {/* Instrumental Bus and individual accompaniment tracks */}
        {instrumentalTracks.length > 0 && (
          <div
            className={clsx("score-track-card score-track-row instrumental-bus-track", {
              "is-muted": instrumentalBus.muted,
              "is-soloed": instrumentalBus.solo,
            })}
            data-testid="score-instrumental-bus"
          >
            <div className="score-track-card-header">
              <div className="score-track-info">
                <span className="score-track-icon-wrapper instrument" title="Instrumental Bus">
                  <Sliders size={14} />
                </span>
                <div className="score-track-details">
                  <span className="score-track-title">All Instruments</span>
                  <div className="score-track-meta">
                    <span className="score-track-type-tag instrument">Instrumental Bus</span>
                  </div>
                </div>
              </div>

              <div className="score-track-transport-btns">
                <button
                  type="button"
                  className={clsx("score-track-solo-btn", { active: instrumentalBus.solo })}
                  onClick={() => onUpdateInstrumentalBusSolo(!instrumentalBus.solo)}
                  aria-pressed={instrumentalBus.solo}
                  title={instrumentalBus.solo ? "Unsolo all instruments" : "Solo all instruments"}
                >
                  S
                </button>
                <button
                  type="button"
                  className={clsx("score-track-mute-btn", { muted: instrumentalBus.muted })}
                  onClick={() => onUpdateInstrumentalBusMute(!instrumentalBus.muted)}
                  aria-pressed={instrumentalBus.muted}
                  title={instrumentalBus.muted ? "Unmute all instruments" : "Mute all instruments"}
                >
                  {instrumentalBus.muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
                </button>
                <button
                  type="button"
                  className="score-track-action-btn score-track-expand-btn"
                  onClick={onToggleInstrumentalTracksExpanded}
                  aria-expanded={instrumentalTracksExpanded}
                  aria-controls="score-individual-instrument-tracks"
                  title={instrumentalTracksExpanded ? "Hide individual instrument controls" : "Show individual instrument controls"}
                >
                  <ListCollapse size={16} aria-hidden="true" />
                  <span>{instrumentalTracksExpanded ? "Hide" : "Details"}</span>
                </button>
              </div>
            </div>

            <div className="score-track-card-footer">
              <span className="score-track-bus-level-label">Instrumental Level</span>
              <div className="score-track-volume-wrapper" title={`Instrumental level: ${formatDb(instrumentalBus.levelDb)}`}>
                <span className="score-track-volume-icon">
                  <Volume2 size={12} />
                </span>
                <input
                  type="range"
                  className="score-track-volume-slider score-track-bus-level-slider"
                  min={INSTRUMENTAL_BUS_MIN_DB}
                  max={INSTRUMENTAL_BUS_MAX_DB}
                  step="0.5"
                  value={instrumentalBus.levelDb}
                  onChange={(event) => onUpdateInstrumentalBusLevel(Number(event.target.value))}
                  aria-label="Instrumental Level"
                />
                <span className="score-track-volume-label score-track-bus-level-value">
                  {formatDb(instrumentalBus.levelDb)}
                </span>
              </div>
            </div>
          </div>
        )}

        {instrumentalTracks.length > 0 && instrumentalTracksExpanded && (
          <div id="score-individual-instrument-tracks" className="score-individual-instrument-tracks">
        {instrumentalTracks.map((inst) => (
          <div
            key={inst.key}
            className={clsx("score-track-card score-track-row instrument-track", {
              "is-muted": inst.muted,
              "is-soloed": inst.solo,
            })}
            data-testid={`score-inst-track-${inst.rawPartId || inst.partIndex}`}
          >
            <div className="score-track-card-header">
              <div className="score-track-info">
                <span className="score-track-grip" aria-hidden="true" title="Accompaniment track">
                  <GripVertical size={13} />
                </span>
                <div className="score-track-icon-wrapper instrument" title="Instrument Accompaniment">
                  <Music2 size={14} />
                </div>
                <div className="score-track-details">
                  <span className="score-track-title" title={inst.label}>
                    {inst.label}
                  </span>
                  <div className="score-track-meta">
                    <span className="score-track-type-tag instrument">Accompaniment</span>
                  </div>
                </div>
              </div>

              <div className="score-track-transport-btns">
                <button
                  type="button"
                  className={clsx("score-track-solo-btn", { active: inst.solo })}
                  onClick={() => onUpdateInstrumentalSolo(inst.key, !inst.solo)}
                  disabled={inst.unassigned}
                  aria-pressed={inst.solo}
                  title={inst.unassigned ? `${inst.percussion ? "Choose a kit" : "Choose an instrument"} to hear this part` : inst.solo ? "Unsolo track" : "Solo track"}
                >
                  S
                </button>
                <button
                  type="button"
                  className={clsx("score-track-mute-btn", { muted: inst.muted })}
                  onClick={() => onUpdateInstrumentalMute(inst.key, !inst.muted)}
                  disabled={inst.unassigned}
                  aria-pressed={inst.muted}
                  title={inst.unassigned ? `${inst.percussion ? "Choose a kit" : "Choose an instrument"} to hear this part` : inst.muted ? "Unmute track" : "Mute track"}
                >
                  {inst.muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
                </button>
              </div>
            </div>

            <div className="score-track-card-footer">
              <GroupedInstrumentPicker
                label={inst.label}
                gmProgram={inst.gmProgram}
                percussion={inst.percussion}
                soundfontBank={inst.soundfontBank}
                presetKind={inst.presetKind}
                unassigned={inst.unassigned}
                onChange={(gmProgram) => onUpdateInstrumentalGmProgram(inst.key, gmProgram)}
              />

              <div className="score-track-volume-wrapper" title={`Volume: ${Math.round(inst.volume * 100)}%`}>
                <span className="score-track-volume-icon">
                  <Volume2 size={12} />
                </span>
                <input
                  type="range"
                  className="score-track-volume-slider"
                  min="0"
                  max="1"
                  step="0.01"
                  value={inst.volume}
                  onChange={(e) => onUpdateInstrumentalVolume(inst.key, parseFloat(e.target.value))}
                  aria-label={`${inst.label} volume`}
                />
                <span className="score-track-volume-label">{Math.round(inst.volume * 100)}%</span>
              </div>
            </div>
          </div>
        ))}
          </div>
        )}
      </fieldset>
    </div>
  );
};

type PartOption = {
  key: string;
  label: string;
  part_id?: string;
  part_name?: string;
  part_index: number;
  has_lyrics?: boolean;
};

const FEEDBACK_ASPECTS = [
  "Pronunciation",
  "Timing/rhythm",
  "Part splitting accuracy",
  "Lyrics alignment",
  "Voice quality",
] as const;

type FeedbackAspect = (typeof FEEDBACK_ASPECTS)[number];
type FeedbackRatings = Record<FeedbackAspect, number>;

const DEFAULT_FEEDBACK_RATINGS: FeedbackRatings = {
  "Voice quality": 3,
  Pronunciation: 3,
  "Timing/rhythm": 3,
  "Lyrics alignment": 3,
  "Part splitting accuracy": 3,
};

const buildPartOptions = (summary: ScoreSummary | null): PartOption[] => {
  if (!summary?.parts?.length) return [];
  const options = summary.parts.map((part, index) => {
    const partId = (part.part_id ?? "").trim();
    const partIndex = (part.part_index !== undefined && part.part_index !== null) ? part.part_index : index;
    const key = partId ? `id:${partId}` : `index:${partIndex}`;
    const labelBase =
      (part.part_name ?? "").trim() || (partId ? `Part ${partId}` : `Part ${partIndex + 1}`);
    const label = part.has_lyrics === false ? `${labelBase} (no lyrics)` : labelBase;
    return {
      key,
      label,
      part_id: partId || undefined,
      part_name: (part.part_name ?? "").trim() || undefined,
      part_index: partIndex,
      has_lyrics: part.has_lyrics,
    };
  });
  const lyricOptions = options.filter((option) => option.has_lyrics !== false);
  return lyricOptions.length ? lyricOptions : options;
};

const downloadTextFile = (fileName: string, content: string) => {
  const blob = new Blob([content], { type: "application/vnd.recordare.musicxml+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
};

const scoreDownloadFileName = (fileName: string): string => {
  const trimmed = fileName.replace(/\.(mxl|musicxml|xml)$/i, "");
  return `${trimmed || "score"}-sightsinger.xml`;
};

const escapeHtml = (value: string): string =>
  value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const buildPrintableScoreMarkup = (scoreElement: HTMLElement): string => {
  const svgPages = Array.from(scoreElement.querySelectorAll("svg"));
  if (svgPages.length === 0) {
    return `<div class="score-page">${scoreElement.innerHTML}</div>`;
  }
  return svgPages
    .map((svg) => {
      const printableSvg = svg.cloneNode(true) as SVGSVGElement;
      printableSvg.removeAttribute("style");
      printableSvg.removeAttribute("x");
      printableSvg.removeAttribute("y");
      printableSvg.setAttribute("preserveAspectRatio", "xMinYMin meet");
      return `<section class="score-page">${printableSvg.outerHTML}</section>`;
    })
    .join("");
};

const renderScorePageLayoutForPrint = async (
  scoreData: string,
  zoomLevel: number
): Promise<string> => {
  // Printing uses the same fixed-A4, native-OSMD zoom path as Page preview so
  // its system and measure wrapping matches what the user sees.
  const container = document.createElement("div");
  container.style.position = "absolute";
  container.style.left = "-100000px";
  container.style.top = "0";
  container.style.width = "900px";
  container.style.opacity = "0";
  container.style.pointerEvents = "none";
  document.body.appendChild(container);

  let osmd: OpenSheetMusicDisplay | null = null;
  try {
    osmd = new OpenSheetMusicDisplay(container, {
      autoResize: false,
      drawTitle: true,
      followCursor: false,
      pageFormat: "A4_P",
      renderSingleHorizontalStaffline: false,
    });
    await osmd.load(scoreData);
    // Apply native OSMD zoom immediately before engraving.
    applyPageLayoutZoom(osmd, zoomLevel);
    osmd.render();
    return buildPrintableScoreMarkup(container);
  } finally {
    try {
      osmd?.clear();
    } catch {
      // Ignore cleanup errors from a temporary print renderer.
    }
    container.remove();
  }
};

const isInsufficientCreditError = (message: string): boolean =>
  /insufficient credits|requires ~?\d+ credits|out of credits/i.test(message);

const voiceInitials = (name: string): string => {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "AI";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return `${words[0][0] ?? ""}${words[1][0] ?? ""}`.toUpperCase();
};

const voiceImageUrl = (voice: VoicebankOption): string | null => {
  const filename = (voice.selector_image || voice.profile_image)?.trim();
  if (!filename || filename.includes("/") || filename.includes("\\")) return null;
  return `/voicebanks/${encodeURIComponent(filename)}`;
};

function RecoverableAudioPlayer({
  audioUrl,
  onRefresh,
  onPlayback,
  onDownload,
  onRecoveryError,
}: {
  audioUrl: string;
  onRefresh: () => Promise<string | null>;
  onPlayback: () => void;
  onDownload: () => void;
  onRecoveryError: (message: string) => void;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const refreshPromiseRef = useRef<Promise<string | null> | null>(null);
  const failedUrlRef = useRef<string | null>(null);
  const resumeTimeRef = useRef(0);
  const expiredWhilePlayingRef = useRef(false);
  const [replacementUrl, setReplacementUrl] = useState<string | null>(null);
  const [stale, setStale] = useState(() => isPlaybackTokenStale(audioUrl));
  const [recovering, setRecovering] = useState(false);
  const effectiveAudioUrl = replacementUrl ?? audioUrl;

  useEffect(() => {
    if (replacementUrl && replacementUrl === audioUrl) {
      setReplacementUrl(null);
    }
  }, [audioUrl, replacementUrl]);

  useEffect(() => {
    expiredWhilePlayingRef.current = false;
    setStale(isPlaybackTokenStale(effectiveAudioUrl));

    const expiresAt = playbackTokenExpiresAt(effectiveAudioUrl);
    if (expiresAt === null) return;

    const markStale = () => {
      if (!isPlaybackTokenStale(effectiveAudioUrl)) return;
      const audio = audioRef.current;
      if (audio && !audio.paused && !audio.ended) {
        expiredWhilePlayingRef.current = true;
        return;
      }
      if (audio && Number.isFinite(audio.currentTime)) {
        resumeTimeRef.current = audio.currentTime;
      }
      setStale(true);
    };
    const timeout = window.setTimeout(
      markStale,
      Math.max(0, expiresAt - Date.now() - PLAYBACK_TOKEN_REFRESH_MARGIN_MS)
    );
    window.addEventListener("focus", markStale);
    document.addEventListener("visibilitychange", markStale);
    return () => {
      window.clearTimeout(timeout);
      window.removeEventListener("focus", markStale);
      document.removeEventListener("visibilitychange", markStale);
    };
  }, [effectiveAudioUrl]);

  const recover = async (autoplay: boolean) => {
    if (refreshPromiseRef.current) return refreshPromiseRef.current;
    const audio = audioRef.current;
    if (audio) {
      resumeTimeRef.current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
      audio.pause();
    }
    setRecovering(true);
    setStale(true);
    const refreshPromise = onRefresh();
    refreshPromiseRef.current = refreshPromise;
    try {
      const nextAudioUrl = await refreshPromise;
      if (!nextAudioUrl || isPlaybackTokenStale(nextAudioUrl)) {
        onRecoveryError("Audio link expired. Please try again.");
        return null;
      }
      failedUrlRef.current = null;
      setReplacementUrl(nextAudioUrl);
      setStale(false);
      if (audio) {
        const resumeAt = resumeTimeRef.current;
        resumeTimeRef.current = 0;
        const startPlayback = () => {
          if (resumeAt > 0 && Number.isFinite(resumeAt)) {
            audio.currentTime = resumeAt;
          }
          if (!autoplay) return;
          void audio.play().catch((error) => {
            console.error("Recovered audio autoplay failed", error);
          });
        };
        audio.addEventListener("canplay", startPlayback, { once: true });
        audio.src = nextAudioUrl;
        audio.load();
        if (audio.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
          audio.removeEventListener("canplay", startPlayback);
          startPlayback();
        }
      }
      return nextAudioUrl;
    } catch (error) {
      onRecoveryError(
        error instanceof Error ? error.message : "Failed to refresh audio playback."
      );
      return null;
    } finally {
      refreshPromiseRef.current = null;
      setRecovering(false);
    }
  };

  const handlePlaybackError = () => {
    if (refreshPromiseRef.current) return;
    if (failedUrlRef.current === effectiveAudioUrl) {
      setStale(true);
      onRecoveryError("This audio is currently unavailable.");
      return;
    }
    failedUrlRef.current = effectiveAudioUrl;
    void recover(true);
  };

  return (
    <div className="audio-actions">
      <audio
        ref={audioRef}
        className="audio-player"
        data-testid="synthesis-audio"
        controls
        hidden={stale || recovering}
        src={stale || recovering ? undefined : effectiveAudioUrl}
        onPlay={(event) => {
          const playingUrl = event.currentTarget.currentSrc || event.currentTarget.src;
          if (isPlaybackTokenStale(playingUrl)) {
            event.currentTarget.pause();
            void recover(true);
            return;
          }
          onPlayback();
        }}
        onPause={(event) => {
          if (expiredWhilePlayingRef.current || isPlaybackTokenStale(effectiveAudioUrl)) {
            if (Number.isFinite(event.currentTarget.currentTime)) {
              resumeTimeRef.current = event.currentTarget.currentTime;
            }
            setStale(true);
          }
        }}
        onEnded={(event) => {
          event.currentTarget.currentTime = 0;
          resumeTimeRef.current = 0;
        }}
        onError={handlePlaybackError}
      />
      {stale || recovering ? (
        <button
          type="button"
          className="audio-recovery-button"
          data-testid="synthesis-audio-recover"
          onClick={() => void recover(true)}
          disabled={recovering}
        >
          <Play size={16} aria-hidden="true" />
          <span>{recovering ? "Loading audio..." : "Play audio"}</span>
        </button>
      ) : null}
      <button
        type="button"
        className="audio-download-button"
        aria-label="Download audio"
        title="Download audio"
        onClick={onDownload}
      >
        <Download size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

function FeedbackPrototypeBubble({
  minimized,
  onClose,
  onReopen,
  onSubmit,
}: {
  minimized: boolean;
  onClose: () => void;
  onReopen: () => void;
  onSubmit: (payload: { ratings: FeedbackRatings; feedback: string }) => void;
}) {
  const [ratings, setRatings] = useState<FeedbackRatings>(DEFAULT_FEEDBACK_RATINGS);
  const [feedback, setFeedback] = useState("");

  const chartCenter = { x: 180, y: 145 };
  const chartRadius = 88;
  const chartWidth = 360;
  const chartHeight = 285;
  const chartAspectCount = FEEDBACK_ASPECTS.length;
  const labelLines: Record<FeedbackAspect, string[]> = {
    "Voice quality": ["Voice", "quality"],
    Pronunciation: ["Pronunciation"],
    "Timing/rhythm": ["Timing", "/ rhythm"],
    "Lyrics alignment": ["Lyrics", "alignment"],
    "Part splitting accuracy": ["Part splitting", "accuracy"],
  };

  const aspectPoint = (index: number, radius: number) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / chartAspectCount;
    return {
      angle,
      x: chartCenter.x + Math.cos(angle) * radius,
      y: chartCenter.y + Math.sin(angle) * radius,
    };
  };

  const chartPoints = (values: number[]) =>
    values
      .map((value, index) => {
        const { x, y } = aspectPoint(index, chartRadius * (value / 5));
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");

  const guidePoints = (level: number) => chartPoints(FEEDBACK_ASPECTS.map(() => level));
  const ratingValues = FEEDBACK_ASPECTS.map((aspect) => ratings[aspect]);

  const handleChartClick = (event: MouseEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width) * chartWidth;
    const y = ((event.clientY - bounds.top) / bounds.height) * chartHeight;
    const dx = x - chartCenter.x;
    const dy = y - chartCenter.y;
    const distance = Math.sqrt(dx * dx + dy * dy);
    const rawAngle = Math.atan2(dy, dx);
    let closestIndex = 0;
    let closestDistance = Number.POSITIVE_INFINITY;

    FEEDBACK_ASPECTS.forEach((_, index) => {
      const { angle } = aspectPoint(index, chartRadius);
      const delta = Math.abs(Math.atan2(Math.sin(rawAngle - angle), Math.cos(rawAngle - angle)));
      if (delta < closestDistance) {
        closestDistance = delta;
        closestIndex = index;
      }
    });

    const value = Math.max(1, Math.min(5, Math.ceil((distance / chartRadius) * 5)));
    const aspect = FEEDBACK_ASPECTS[closestIndex];
    setRatings((prev) => ({ ...prev, [aspect]: value }));
  };

  if (minimized) {
    return (
      <div className="chat-bubble assistant feedback-minimized-bubble reveal">
        <span>Feedback form minimized</span>
        <button type="button" className="feedback-reopen-button" onClick={onReopen}>
          Rate this take
        </button>
      </div>
    );
  }

  return (
    <div className="chat-bubble assistant feedback-prototype-bubble reveal">
      <form
        className="feedback-prototype-form"
        aria-labelledby="feedback-prototype-title"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit({ ratings, feedback });
        }}
      >
        <button
          type="button"
          className="feedback-prototype-close"
          aria-label="Close feedback"
          onClick={onClose}
        >
          <X size={18} aria-hidden="true" />
        </button>
        <header className="feedback-prototype-header">
          <h3 id="feedback-prototype-title">How was the singing?</h3>
          <p>Quick feedback helps improve future audio generation.</p>
        </header>

        <div className="feedback-prototype-body">
          <div className="feedback-radar">
            <svg
              viewBox={`0 0 ${chartWidth} ${chartHeight}`}
              role="img"
              aria-label="Click the pentagon chart to rate each aspect from 1 to 5."
              onClick={handleChartClick}
            >
              {[1, 2, 3, 4, 5].map((level) => (
                <polygon
                  key={level}
                  points={guidePoints(level)}
                  className="feedback-radar-guide"
                />
              ))}
              {FEEDBACK_ASPECTS.map((aspect, index) => {
                const labelPoint = aspectPoint(index, 125);
                const axisPoint = aspectPoint(index, chartRadius);
                const lines = labelLines[aspect];
                return (
                  <g key={aspect}>
                    <line
                      x1={chartCenter.x}
                      y1={chartCenter.y}
                      x2={axisPoint.x.toFixed(1)}
                      y2={axisPoint.y.toFixed(1)}
                      className="feedback-radar-axis"
                    />
                    <text
                      x={labelPoint.x.toFixed(1)}
                      y={labelPoint.y.toFixed(1)}
                      textAnchor={
                        labelPoint.x < chartCenter.x - 8
                          ? "end"
                          : labelPoint.x > chartCenter.x + 8
                            ? "start"
                            : "middle"
                      }
                      dominantBaseline="middle"
                      className="feedback-radar-label"
                    >
                      {lines.map((line, lineIndex) => (
                        <tspan
                          key={line}
                          x={labelPoint.x.toFixed(1)}
                          dy={lineIndex === 0 ? `${-(lines.length - 1) * 0.55}em` : "1.1em"}
                        >
                          {line}
                        </tspan>
                      ))}
                      <tspan
                        x={labelPoint.x.toFixed(1)}
                        dy="1.25em"
                        className="feedback-radar-label-value"
                      >
                        {ratings[aspect]}
                      </tspan>
                    </text>
                  </g>
                );
              })}
              <polygon points={chartPoints(ratingValues)} className="feedback-radar-score" />
              {ratingValues.map((value, index) => {
                const { x, y } = aspectPoint(index, chartRadius * (value / 5));
                return (
                  <circle
                    key={`${FEEDBACK_ASPECTS[index]}:${value}`}
                    cx={x.toFixed(1)}
                    cy={y.toFixed(1)}
                    r="5"
                    className="feedback-radar-point"
                  />
                );
              })}
            </svg>
            <p className="feedback-radar-hint">Click closer to the center for 1, outer edge for 5.</p>
          </div>
        </div>

        <label className="feedback-text-field">
          <span>Any other suggestions?</span>
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={4}
            maxLength={4000}
          />
        </label>

        <div className="feedback-prototype-actions">
          <button type="button" className="feedback-secondary-action" onClick={onClose}>
            Close
          </button>
          <button type="submit" className="feedback-primary-action">
            Submit
          </button>
        </div>
      </form>
    </div>
  );
}

type SongLoadState = "idle" | "loading" | "unavailable" | "failed";

// A score the user picked while a Song is open, waiting for "New song or replace?".
type PendingUpload = {
  file: File;
  demoSongId: string | null;
  // Replace was refused because the job was already finishing; ask again when it ends.
  waitForWorkToEnd: boolean;
};

type MixSaveState = {
  epoch: number;
  songId: string | null;
  // Which tracks and rows exist. A change here alone is not a mix change.
  structure: string;
  saved: string;
  pending: { songId: string; epoch: number; signature: string; body: PlayerMixRequest } | null;
  timer: number | undefined;
  inFlight: boolean;
  due: boolean;
};

const MIX_SAVE_DELAY_MS = 1000;

type RebuiltChat = {
  messages: Message[];
  // The queued or running job to keep following, with the bubble that shows it.
  resume: { messageId: string; job: WorkspaceJob } | null;
  // A queued or running job with no chat turn: no bubble, but the Song is busy.
  detachedJob: WorkspaceJob | null;
};

// A player track as the workspace saved it.
const workspaceVocalTrack = (vocal: WorkspaceVocal): MultiTrackAudioTrack => ({
  key: vocal.key,
  label: vocal.label,
  audioUrl: vocal.audio_url,
  partId: vocal.part_id ?? null,
  partIndex: vocal.part_index ?? null,
  verseNumber: vocal.verse_number ?? null,
  durationSeconds: vocal.duration_seconds ?? undefined,
  jobId: vocal.source_job_id,
  sourceJobId: vocal.source_job_id,
  expandRepeats: vocal.expand_repeats ?? undefined,
  takeSignature: vocal.take_signature ?? null,
  mode: vocal.mode ?? "lyrics",
  muted: vocal.muted,
  solo: vocal.solo,
  volume: vocal.volume,
});

const SING_ALL_POLL_MS = 3000;

// The header pill's words for a running Sing All, e.g. "Singing 3/8".
const singAllPillStage = (job: SingAllProgress): string => {
  if (job.cancelRequested) return "Stopping";
  if (job.status === "needs_decision") return "Action needed";
  if (job.render.state === "waiting") return "Queued";
  switch (job.stage) {
    case "reading":
      return "Reading the score";
    case "splitting":
      return `Splitting ${job.counts.splits.done}/${job.counts.splits.total}`;
    case "solfege":
      return "Adding solfège";
    case "packaging":
      return job.package?.state === "mixing" && job.package.mixes.total > 0
        ? `Mixing ${job.package.mixes.done}/${job.package.mixes.total}`
        : "Packing the zip";
    default:
      return `Singing ${job.counts.takes.done}/${job.counts.takes.total}`;
  }
};

const isLiveJob = (job: WorkspaceJob) => job.status === "queued" || job.status === "running";

// Rebuilds a reopened Song's chat. A turn is a user message and the assistant
// messages after it, and its id is the user message's id. Each message gets a
// bubble. A turn's first job is shown in the turn's last assistant bubble, and
// any later job in a bubble of its own right after.
function rebuildChat(messages: WorkspaceMessage[], jobs: WorkspaceJob[]): RebuiltChat {
  const jobsByTurn = new Map<string, WorkspaceJob[]>();
  let detachedJob: WorkspaceJob | null = null;
  [...jobs]
    .sort((left, right) => left.created_at.localeCompare(right.created_at))
    .forEach((job) => {
      if (!job.originating_turn_id) {
        if (isLiveJob(job)) detachedJob = job;
        return;
      }
      const turnJobs = jobsByTurn.get(job.originating_turn_id) ?? [];
      turnJobs.push(job);
      jobsByTurn.set(job.originating_turn_id, turnJobs);
    });

  const rebuilt: Message[] = [];
  let resume: RebuiltChat["resume"] = null;
  const showJob = (bubble: Message, job: WorkspaceJob): Message => {
    const shown = applyProgressToMessage(
      { ...bubble, progressUrl: job.progress_url, jobId: job.job_id ?? bubble.jobId },
      job
    );
    if (isLiveJob(job)) resume = { messageId: shown.id, job };
    return shown;
  };
  const closeTurn = (turnId: string | null, firstBubbleIndex: number) => {
    const turnJobs = turnId ? jobsByTurn.get(turnId) : undefined;
    if (!turnJobs?.length) return;
    const [firstJob, ...laterJobs] = turnJobs;
    let lastAssistantIndex = -1;
    for (let index = rebuilt.length - 1; index >= firstBubbleIndex; index -= 1) {
      if (rebuilt[index].role === "assistant") {
        lastAssistantIndex = index;
        break;
      }
    }
    if (lastAssistantIndex < 0) {
      rebuilt.push(showJob({ id: `job-${firstJob.job_id}`, role: "assistant", content: "" }, firstJob));
    } else {
      rebuilt[lastAssistantIndex] = showJob(rebuilt[lastAssistantIndex], firstJob);
    }
    laterJobs.forEach((job) => {
      rebuilt.push(showJob({ id: `job-${job.job_id}`, role: "assistant", content: "" }, job));
    });
  };

  let turnId: string | null = null;
  let turnStart = 0;
  // A Sing All job's entries (started, paused, ended) are context for the chat model; the
  // chat shows the job's card in their place, once, at the first.
  const singAllShown = new Set<string>();
  messages.forEach((message) => {
    if (message.role === "user") {
      closeTurn(turnId, turnStart);
      turnId = message.id;
      turnStart = rebuilt.length;
    }
    if (message.singAllJobId) {
      if (singAllShown.has(message.singAllJobId)) return;
      singAllShown.add(message.singAllJobId);
      rebuilt.push({ id: message.id, role: message.role, content: "", singAllJobId: message.singAllJobId });
      return;
    }
    rebuilt.push({ id: message.id, role: message.role, content: message.content });
  });
  closeTurn(turnId, turnStart);
  return { messages: rebuilt, resume, detachedJob };
}

export default function MainApp() {
  const navigate = useNavigate();
  const { user, isAuthenticated } = useAuth();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatAutoScrollRequest, setChatAutoScrollRequest] = useState(0);
  const [input, setInput] = useState("");
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [score, setScore] = useState<ScorePayload | null>(null);
  const [scoreSummary, setScoreSummary] = useState<ScoreSummary | null>(null);
  // Read when a take lands, to drop takes that no longer match the score,
  // without re-creating the take callbacks on every summary change.
  const scoreSummaryRef = useRef(scoreSummary);
  scoreSummaryRef.current = scoreSummary;
  const [synthesisEstimate, setSynthesisEstimate] = useState<SynthesisCreditEstimate | null>(null);
  const [synthesisEstimateLoading, setSynthesisEstimateLoading] = useState(false);
  const [synthesisEstimateRevision, setSynthesisEstimateRevision] = useState(0);
  const [performanceMidi, setPerformanceMidi] = useState<PerformanceMidi | null>(null);
  const [instrumentalTracks, setInstrumentalTracks] = useState<InstrumentalTrackState[]>([]);
  const [instrumentalBus, setInstrumentalBus] = useState<InstrumentalBusState>(
    DEFAULT_INSTRUMENTAL_BUS
  );
  const [instrumentalTracksExpanded, setInstrumentalTracksExpanded] = useState(true);
  const [instrumentalMidiUrl, setInstrumentalMidiUrl] = useState<string | null>(null);
  const [scorePlayerControls, setScorePlayerControls] =
    useState<ScorePlayerPlaybackControls | null>(null);
  const [scorePlayerError, setScorePlayerError] = useState<string | null>(null);
  const [scorePlayerPlaybackRequestId, setScorePlayerPlaybackRequestId] = useState(0);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [multiTrackAudioTracks, setMultiTrackAudioTracks] = useState<MultiTrackAudioTrack[]>([]);
  // With takes in both modes the player shows, plays and mixes one mode's takes at a time.
  // The saved mix keeps every take.
  const [vocalModeShown, setVocalModeShown] = useState<VocalMode>("lyrics");
  const vocalsInBothModes = useMemo(() => hasVocalsInBothModes(multiTrackAudioTracks), [multiTrackAudioTracks]);
  const shownVocalTracks = useMemo(
    () => shownVocals(multiTrackAudioTracks, vocalModeShown),
    [multiTrackAudioTracks, vocalModeShown]
  );
  // Decoded audio of each take in the player, keyed by its source job. A take
  // is decoded once, when it enters the player; the player plays only these
  // buffers, so a take is never downloaded again. A take leaving the player
  // (replaced by a newer take of its part, or cleared by a new score) drops
  // its audio here too.
  const [decodedVocals, setDecodedVocals] = useState<ReadonlyMap<string, AudioBuffer>>(
    () => new Map()
  );
  // Takes whose decode failed, with the URL that failed. A new URL for the
  // take (a token refresh) or a Play click retries it.
  const [failedVocalDecodes, setFailedVocalDecodes] = useState<ReadonlyMap<string, string>>(
    () => new Map()
  );
  const vocalDecodesInFlightRef = useRef(new Set<string>());
  const liveVocalJobsRef = useRef(new Set<string>());
  const [multiTrackPlaying, setMultiTrackPlaying] = useState(false);
  const [multiTrackExportProgress, setMultiTrackExportProgress] = useState<number | null>(null);
  const [multiTrackExportError, setMultiTrackExportError] = useState<string | null>(null);
  const [browserMixBouncePhase, setBrowserMixBouncePhase] = useState<BrowserMixBouncePhase>("idle");
  const [browserMixBouncePreparation, setBrowserMixBouncePreparation] = useState<string | null>(null);
  const [browserMixBounceNotice, setBrowserMixBounceNotice] = useState<string | null>(null);
  const [browserMixBounceError, setBrowserMixBounceError] = useState<string | null>(null);
  const [scorePlayerTimelineDuration, setScorePlayerTimelineDuration] = useState(0);
  const [scorePlayerSeekBarHost, setScorePlayerSeekBarHost] = useState<HTMLDivElement | null>(null);
  const [scorePlayerAdapterReady, setScorePlayerAdapterReady] = useState(false);
  const [scorePlayerAssetsReady, setScorePlayerAssetsReady] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [splitPct, setSplitPct] = useState(40);
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [scoreTracksCollapsed, setScoreTracksCollapsed] = useState(false);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [scorePreviewLayout, setScorePreviewLayout] = useState<ScorePreviewLayout>("page");
  const [horizontalRendererRevision, setHorizontalRendererRevision] = useState(0);
  const [expandRepeats, setExpandRepeats] = useState(false);
  const [scoreReady, setScoreReady] = useState(false);
  const [scorePreviewError, setScorePreviewError] = useState<string | null>(null);
  const [voicebanks, setVoicebanks] = useState<VoicebankOption[]>([]);
  const [voicebanksLoading, setVoicebanksLoading] = useState(false);
  const [selectedVoicebankId, setSelectedVoicebankId] = useState<string | null>(null);
  const [failedVoiceImages, setFailedVoiceImages] = useState<Record<string, boolean>>({});
  const [voiceMenuOpen, setVoiceMenuOpen] = useState(false);
  const [solfegeMenuOpen, setSolfegeMenuOpen] = useState(false);
  const [solfegeSystem, setSolfegeSystem] = useState<SolfegeSystem>("movable_do");
  const [solfegeMode, setSolfegeMode] = useState<SolfegeMode>("major");
  const [draftSolfegeSystem, setDraftSolfegeSystem] = useState<SolfegeSystem>("movable_do");
  const [draftSolfegeMode, setDraftSolfegeMode] = useState<SolfegeMode>("major");
  const [solfegeSettingsSaving, setSolfegeSettingsSaving] = useState(false);
  const [showWaitlistModal, setShowWaitlistModal] = useState(false);
  const [waitlistSource, setWaitlistSource] = useState<WaitlistSource>("studio_menu");
  const [showCreditsModal, setShowCreditsModal] = useState(false);
  const [showTrialExpiredModal, setShowTrialExpiredModal] = useState(false);
  const [paywallTrigger, setPaywallTrigger] = useState<PaywallTrigger | null>(null);
  const [paywallDetail, setPaywallDetail] = useState<string | null>(null);
  const [dismissedBillingWarningStatus, setDismissedBillingWarningStatus] = useState<string | null>(null);
  const [expandedThoughts, setExpandedThoughts] = useState<Record<string, boolean>>({});
  const [expandedDiagnostics, setExpandedDiagnostics] = useState<Record<string, boolean>>({});
  const [feedbackPrompts, setFeedbackPrompts] = useState<Record<string, "open" | "minimized">>({});
  const [chatTurnInProgress, setChatTurnInProgress] = useState(false);
  const [activeJobKind, setActiveJobKind] = useState<string | null>(null);
  const [jobKindPending, setJobKindPending] = useState(false);
  const [stopRequestInFlight, setStopRequestInFlight] = useState(false);
  const [activeProgress, setActiveProgress] = useState<{
    messageId: string;
    url: string;
    boundedAttempts?: number;
  } | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const multiTrackWaveSurferRefs = useRef<Record<string, WaveSurfer | null>>({});
  const audioRefreshPromisesRef = useRef<Record<string, Promise<string | null> | undefined>>({});
  const scorePlayerAudioRefreshAttemptsRef = useRef<Set<string>>(new Set());
  const scorePlayerControlsRef = useRef<ScorePlayerPlaybackControls | null>(null);
  const scorePlayerPlaybackRequestRef = useRef(0);
  const scorePlayerPlayPendingRef = useRef(false);
  const scorePlayerAssetsReadyRef = useRef(false);
  const scorePlayerAdapterRef = useRef<ToneAdapter | null>(null);
  const scorePlayerAudioContextResumeRef = useRef<{
    adapter: ToneAdapter;
    promise: Promise<void>;
  } | null>(null);
  const browserMixBounceRef = useRef<BrowserMixBounceOperation | null>(null);
  const browserMixBounceAttemptRef = useRef(0);
  const browserMixBouncePhaseRef = useRef<BrowserMixBouncePhase>(browserMixBouncePhase);
  browserMixBouncePhaseRef.current = browserMixBouncePhase;
  const browserMixBounceFinalizeRef = useRef<(discard: boolean) => void>(() => {});
  const browserMixBounceCancelRef = useRef<(reason?: string) => void>(() => {});
  const scoreCanvasRef = useRef<HTMLDivElement | null>(null);
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const horizontalScoreRendererRef = useRef<HorizontalScoreRendererHandle | null>(null);
  const activeScoreMeasureRef = useRef<{ osmd: OpenSheetMusicDisplay; key: string } | null>(null);
  const performanceMeasureLookupRef = useRef(createPerformanceMeasureLookup());
  const scoreFollowFrameRef = useRef<number | null>(null);
  const activePerformanceMeasureRef = useRef<PerformanceMeasureMapEntry | null>(null);
  const performanceMeasureMapRef = useRef(scoreSummary?.performance_measure_map ?? null);
  performanceMeasureMapRef.current = scoreSummary?.performance_measure_map ?? null;
  // The toggle chooses the repeat setting of the next render. Playback follows
  // the takes in the player instead, which all share one setting: the
  // instrumental MIDI and the score cursor play in that order. With no such
  // take there is nothing to keep in sync, so playback previews the toggle.
  const playbackExpandRepeats =
    multiTrackAudioTracks.find((track) => track.expandRepeats !== undefined)?.expandRepeats ??
    expandRepeats;
  const playbackExpandRepeatsRef = useRef(playbackExpandRepeats);
  playbackExpandRepeatsRef.current = playbackExpandRepeats;
  const scorePreviewLayoutRef = useRef(scorePreviewLayout);
  scorePreviewLayoutRef.current = scorePreviewLayout;
  const voicePickerRef = useRef<HTMLDivElement | null>(null);
  const solfegePickerRef = useRef<HTMLDivElement | null>(null);
  const [scoreMenuOpen, setScoreMenuOpen] = useState(false);
  const scorePickerRef = useRef<HTMLDivElement | null>(null);
  const scoreFileInputRef = useRef<HTMLInputElement | null>(null);
  // The demo song in the score panel, if any; its prompts replace the generic ones.
  const [activeDemoSongId, setActiveDemoSongId] = useState<string | null>(null);
  const sessionInitPromiseRef = useRef<Promise<string> | null>(null);
  const activeUserIdRef = useRef<string | null>(user?.uid ?? null);
  const autoPaywallTriggersRef = useRef<Set<string>>(new Set());
  const feedbackPromptedMessagesRef = useRef<Set<string>>(new Set());
  const feedbackPromptConsumedThisSessionRef = useRef(false);
  const pendingCheckoutStartedRef = useRef(false);
  const billingSyncInFlightRef = useRef(false);
  const lastBillingSyncAtRef = useRef(0);
  const checkoutReturnSyncStartedRef = useRef(false);
  const chatTurnInProgressRef = useRef(false);
  const stopRefundsRef = useRef(new Map<string, number>());
  const stoppedJobIdsRef = useRef(new Set<string>());
  const activeSynthesisStreamRef = useRef<ChatStream | null>(null);
  const suppressedMultiTrackMessageIdsRef = useRef<Set<string>>(new Set());
  const activeScoreIdRef = useRef<string | null>(null);
  const workspaceGenerationRef = useRef(0);
  // Songs: `/app` is the start screen, `/app/song/{id}` shows one Song. A Song
  // exists once its session has a score, and its id is the session id.
  const routeSongId = useMatch("/app/song/:songId")?.params.songId ?? null;
  const [currentSong, setCurrentSong] = useState<SongInfo | null>(null);
  // Share: the Song whose share box is open, and whether this user can share (from the last
  // workspace that said so; My songs offers Share only then).
  const [shareBoxSong, setShareBoxSong] = useState<{ id: string; title: string; url: string | null } | null>(null);
  const [songInfoOpen, setSongInfoOpen] = useState(false);
  const [shareAvailable, setShareAvailable] = useState(false);
  const shareLookupSongRef = useRef<string | null>(null);
  // Sing All: the caller's running job, from any Song, and each job a card shows.
  const [activeSingAll, setActiveSingAll] = useState<SingAllProgress | null>(null);
  const [singAllJobs, setSingAllJobs] = useState<Record<string, SingAllProgress>>({});
  const singAllRunningHere = Boolean(activeSingAll && activeSingAll.songId === sessionId);
  // Stop next to Send asks first while this Song's Sing All runs.
  const [confirmSingAllStop, setConfirmSingAllStop] = useState(false);
  const singAllStopping = singAllRunningHere && Boolean(activeSingAll?.cancelRequested);
  const [songLoadState, setSongLoadState] = useState<SongLoadState>("idle");
  // From sending a chat message until the assistant's reply arrives. The Song
  // can't be left meanwhile; running work alone doesn't keep the user in it.
  const [awaitingReply, setAwaitingReply] = useState(false);
  const [pendingUpload, setPendingUpload] = useState<PendingUpload | null>(null);
  const [songsDrawerOpen, setSongsDrawerOpen] = useState(false);
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const titleEditCancelledRef = useRef(false);
  // With a Song open, a score can be added while work runs: the popup offers
  // New song or Replace. Only the wait for the assistant's reply blocks it.
  const uploadBlocked = currentSong ? awaitingReply : chatTurnInProgress;
  const uploadBlockedTitle = !uploadBlocked
    ? undefined
    : currentSong
      ? "Wait for the reply first"
      : "Stop the running work first";
  // A reply can't be picked up again, so the Song can't be left while one is awaited.
  const canLeaveSong = !awaitingReply && !uploading;
  // Goes up whenever the mix on screen is, by definition, the saved one: a
  // Song was just opened, or its score was just replaced.
  const [mixBaselineEpoch, setMixBaselineEpoch] = useState(0);
  const mixSaveRef = useRef<MixSaveState>({
    epoch: -1,
    songId: null,
    structure: "",
    saved: "",
    pending: null,
    timer: undefined,
    inFlight: false,
    due: false,
  });
  // The Song the page shows or is loading, so a route change to it is not an "open".
  const shownSongIdRef = useRef<string | null>(null);
  // Goes up when the workspace is cleared; a session created before that is not adopted.
  const sessionEpochRef = useRef(0);
  // A reopened Song's saved instrument choices, until the rows are next built.
  const savedInstrumentChoicesRef = useRef<WorkspaceInstrument[] | null>(null);
  const pendingDemoSongRef = useRef<{ songId: string; demoSongId: string | null } | null>(null);
  // A running job that belongs to no chat turn (an export mix) in a reopened Song.
  const [detachedJobUrl, setDetachedJobUrl] = useState<string | null>(null);

  const browserMixBounceActive = browserMixBouncePhase !== "idle";

  const horizontalScoreMeasuresPerBatch = useMemo(() => {
    const fallbackPartCount = scoreSummary?.parts?.length ?? 1;
    const parallelStaffCount = score
      ? countParallelScoreStaves(score.data, fallbackPartCount)
      : fallbackPartCount;
    return getHorizontalScoreIncrementalBatchSize(parallelStaffCount);
  }, [score?.data, scoreSummary?.parts?.length]);

  const setChatTurnBusy = (busy: boolean) => {
    chatTurnInProgressRef.current = busy;
    setChatTurnInProgress(busy);
    if (!busy) {
      setActiveJobKind(null);
      setJobKindPending(false);
    }
  };

  const handleScorePlayerControlsChange = useCallback(
    (controls: ScorePlayerPlaybackControls | null) => {
      scorePlayerControlsRef.current = controls;
      // usePlaylistControls can return a new object after a provider render. Keep
      // that live object in the ref, but do not feed it back into React state on
      // every render (which otherwise remounts the provider indefinitely).
      setScorePlayerControls((current) => {
        if (!controls) return current ? null : current;
        return current ?? controls;
      });
    },
    []
  );

  const handleScorePlayerEngineLoading = useCallback(() => {
    scorePlayerAssetsReadyRef.current = false;
    setScorePlayerAssetsReady(false);
  }, []);

  const handleScorePlayerPlaybackStateChange = useCallback((isPlaying: boolean) => {
    if (browserMixBounceRef.current) {
      traceBrowserMixBounce("playlist-playback-state", {
        isPlaying,
        bouncePhase: browserMixBounceRef.current.phase,
      });
    }
    setMultiTrackPlaying((current) => (current === isPlaying ? current : isPlaying));
    if (!isPlaying && browserMixBounceRef.current?.phase === "recording") {
      browserMixBounceFinalizeRef.current(false);
    }
  }, []);

  const handleScorePlayerTimelineDurationChange = useCallback((seconds: number) => {
    const normalized = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
    setScorePlayerTimelineDuration((current) =>
      Math.abs(current - normalized) < 0.001 ? current : normalized
    );
  }, []);

  const handleScorePlayerAdapterChange = useCallback((adapter: ToneAdapter | null) => {
    if (!adapter && browserMixBounceRef.current) {
      browserMixBounceCancelRef.current("adapter-disposed");
    }
    scorePlayerAdapterRef.current = adapter;
    if (scorePlayerAudioContextResumeRef.current?.adapter !== adapter) {
      scorePlayerAudioContextResumeRef.current = null;
    }
    setScorePlayerAdapterReady(Boolean(adapter?.masterOutputNode));
  }, []);

  const primeScorePlayerAudioContext = useCallback((): Promise<void> | null => {
    const adapter = scorePlayerAdapterRef.current;
    if (!adapter) {
      traceBrowserMixBounce("audio-resume-unavailable");
      return null;
    }
    if (adapter.audioContext.state === "running") {
      traceBrowserMixBounce("audio-already-running");
      return Promise.resolve();
    }
    const current = scorePlayerAudioContextResumeRef.current;
    if (current?.adapter === adapter) {
      traceBrowserMixBounce("audio-resume-reused", { state: adapter.audioContext.state });
      return current.promise;
    }

    // This must be invoked directly from pointer/key activation. Waiting until
    // React's click handler has started asynchronous work can make Chrome
    // treat the same user action as no longer eligible to resume Web Audio.
    traceBrowserMixBounce("audio-resume-requested", { state: adapter.audioContext.state });
    const promise = adapter.audioContext.resume();
    scorePlayerAudioContextResumeRef.current = { adapter, promise };
    void promise.then(
      () => {
        traceBrowserMixBounce("audio-resume-complete", { state: adapter.audioContext.state });
        if (scorePlayerAudioContextResumeRef.current?.promise === promise) {
          scorePlayerAudioContextResumeRef.current = null;
        }
      },
      (error) => {
        traceBrowserMixBounce("audio-resume-failed", {
          state: adapter.audioContext.state,
          error: error instanceof Error ? error.message : String(error),
        });
        if (scorePlayerAudioContextResumeRef.current?.promise === promise) {
          scorePlayerAudioContextResumeRef.current = null;
        }
      }
    );
    return promise;
  }, []);

  const positionActiveScoreMeasure = useCallback((activeMeasure: PerformanceMeasureMapEntry) => {
    const activeKey = `${activeMeasure.played_measure_index}:${activeMeasure.source_measure_index}`;
    const horizontal = scorePreviewLayoutRef.current === "horizontal";
    const renderer = horizontal ? horizontalScoreRendererRef.current : null;
    const osmd = horizontal ? renderer?.osmd : osmdRef.current;
    if (!osmd) return;
    if (activeScoreMeasureRef.current?.osmd === osmd && activeScoreMeasureRef.current.key === activeKey) return;
    try {
      // Native scroll rendering normally supplies upcoming notation. Only
      // check the fallback on a new measure (e.g. an undrawn Coda destination).
      renderer?.ensureSourceMeasureRendered(activeMeasure.source_measure_index);
      if (!positionNativeScoreCursor(osmd, activeMeasure.source_measure_index)) return;
      const cursor = osmd.cursor;
      const cursorElement = cursor.cursorElement;
      cursorElement.classList.add("score-active-measure-highlight");
      cursorElement.dataset.testid = "active-score-measure";
      cursorElement.dataset.sourceMeasureIndex = String(activeMeasure.source_measure_index);
      cursorElement.dataset.playedMeasureIndex = String(activeMeasure.played_measure_index);
      activeScoreMeasureRef.current = { osmd, key: activeKey };
      if (scoreFollowFrameRef.current !== null) window.cancelAnimationFrame(scoreFollowFrameRef.current);
      scoreFollowFrameRef.current = window.requestAnimationFrame(() => {
        scoreFollowFrameRef.current = null;
        const canvas = scoreCanvasRef.current;
        if (canvas?.isConnected && cursorElement.isConnected) {
          scrollScoreMeasureIntoView(canvas, cursorElement, scorePreviewLayoutRef.current);
        }
      });
    } catch {
      // Cursor positioning is visual-only; a score-specific OSMD limitation
      // must never interrupt the shared audio/MIDI transport.
    }
  }, []);

  const handleHorizontalScoreRendererChange = useCallback(
    (renderer: HorizontalScoreRendererHandle | null) => {
      activeScoreMeasureRef.current = null;
      horizontalScoreRendererRef.current = renderer;
      setHorizontalRendererRevision((current) => current + 1);
    },
    []
  );

  const handleHorizontalScoreReady = useCallback(() => {
    setScorePreviewError(null);
    setScoreReady(true);
  }, []);

  const handleScorePlayerPlaybackPositionChange = useCallback((playbackSeconds: number) => {
    const performanceMeasureMap = performanceMeasureMapRef.current;
    const entries = playbackExpandRepeatsRef.current
      ? performanceMeasureMap?.expanded
      : performanceMeasureMap?.written;
    const activeMeasure = performanceMeasureLookupRef.current(entries, playbackSeconds);
    if (!activeMeasure) return;
    activePerformanceMeasureRef.current = activeMeasure;
    positionActiveScoreMeasure(activeMeasure);
  }, [positionActiveScoreMeasure]);

  useEffect(() => {
    if (scorePreviewLayout !== "horizontal") return;
    const activeMeasure = activePerformanceMeasureRef.current;
    if (activeMeasure) positionActiveScoreMeasure(activeMeasure);
  }, [horizontalRendererRevision, positionActiveScoreMeasure, scorePreviewLayout]);

  useEffect(() => {
    activeScoreMeasureRef.current = null;
    activePerformanceMeasureRef.current = null;
    horizontalScoreRendererRef.current = null;
    if (scorePreviewLayout === "horizontal" && score) {
      setScoreReady(false);
      setScorePreviewError(null);
    }
  }, [score?.data, scorePreviewLayout]);

  useEffect(() => () => {
    if (scoreFollowFrameRef.current !== null) {
      window.cancelAnimationFrame(scoreFollowFrameRef.current);
      scoreFollowFrameRef.current = null;
    }
  }, [score?.data, scorePreviewLayout, zoomLevel]);

  useEffect(() => {
    if (scorePreviewLayout !== "page") return;
    const scoreElement = scoreRef.current;
    if (!scoreElement) return;

    let animationFrame = 0;
    const updateFit = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        fitPagePreviewToContainer(scoreElement);
      });
    };
    updateFit();
    const observer = new ResizeObserver(updateFit);
    observer.observe(scoreElement);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      observer.disconnect();
    };
  }, [score?.data, scorePreviewLayout, zoomLevel]);

  // The preview is always the original score, but the playback position may
  // switch between its written and repeat-expanded performance maps while the
  // transport is running. Force the next frame to position the OSMD cursor
  // against the newly selected map.
  useEffect(() => {
    activeScoreMeasureRef.current = null;
  }, [playbackExpandRepeats]);

  const splitStyle = useMemo(
    () => ({ "--split": `${splitPct}%` }) as CSSProperties,
    [splitPct]
  );
  const {
    available,
    overdrafted,
    isExpired,
    loading: creditsLoading,
    status: creditsStatus,
    error: creditsError,
  } = useCredits();
  const billing = useBillingState();
  const creditsReady = creditsStatus === "ready";
  const creditsLocked = creditsReady && (overdrafted || isExpired || available <= 0);
  const billingWarningStatus = hasBillingPaymentIssue(billing)
    ? `${billing.stripeSubscriptionStatus || "unknown"}:${billing.latestInvoiceId || ""}:${billing.latestPaymentIntentStatus || ""}:${billing.latestPaymentFailureCode || ""}`
    : null;
  const billingNeedsAttention =
    Boolean(billingWarningStatus) && dismissedBillingWarningStatus !== billingWarningStatus;

  const openPaywall = useCallback((trigger: PaywallTrigger, detail?: string | null) => {
    setPaywallTrigger(trigger);
    setPaywallDetail(detail ?? null);
  }, []);

  const {
      showAnnouncement,
      currentAnnouncement,
      markAsSeen,
      loading: announcementsLoading,
  } = useAnnouncements();

  // A score whose repeat marks give no played order can only sing as written.
  const repeatsUnavailable = scoreSummary?.repeat_expansion_available === false;
  useEffect(() => {
    if (repeatsUnavailable) setExpandRepeats(false);
  }, [repeatsUnavailable]);

  const estimatedDuration = expandRepeats
    ? (scoreSummary?.expanded_duration_seconds ?? scoreSummary?.duration_seconds)
    : scoreSummary?.duration_seconds;
  const estimatedDurationLabel =
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? `Est. length ${formatDuration(estimatedDuration)}`
      : null;
  const estimatedDurationTitle =
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? `Estimated duration: ${formatDuration(estimatedDuration)} (${
          expandRepeats ? "with repeats" : "written order"
        })`
      : undefined;

  // Credit amounts come only from the backend estimate. While a request is in
  // flight the labels say so rather than briefly showing a locally computed or
  // stale number.
  // Only an in-flight request reads as calculating. A request that failed shows
  // no credit line at all, which is still better than a locally derived number.
  // The header column is narrow, so each line uses a short label and carries
  // the full wording as a tooltip.
  const estimateCalculating = synthesisEstimateLoading;
  const vocalCredits = synthesisEstimate?.vocal_part.estimated_credits;
  const estimatedVocalCostLabel = estimateCalculating
    ? "Vocal: calculating…"
    : synthesisEstimate
      ? `Vocal: ${formatCredits(vocalCredits ?? 0)} / part`
      : null;
  const estimatedVocalCostTitle = synthesisEstimate
    ? `Estimated cost per vocal part: ${formatCredits(vocalCredits ?? 0)}`
    : undefined;
  const instrumentalsAlreadyGenerated = Boolean(
    synthesisEstimate?.instrumentals.has_instrumental_parts &&
      !synthesisEstimate.instrumentals.charge_required
  );
  const instrumentalCredits = synthesisEstimate?.instrumentals.estimated_credits;
  const estimatedInstrumentalCostLabel = estimateCalculating
    ? "Instruments: calculating…"
    : synthesisEstimate
      ? instrumentalsAlreadyGenerated
        ? "Instruments: paid"
        : `Instruments: ${formatCredits(instrumentalCredits ?? 0)}`
      : null;
  const estimatedInstrumentalCostTitle = synthesisEstimate
    ? instrumentalsAlreadyGenerated
      ? "Instrumentals for this score are already generated: no further charge"
      : `Estimated cost for instrumentals: ${formatCredits(
          instrumentalCredits ?? 0
        )}, charged once for all instrumental tracks`
    : undefined;
  const hasScorePlayerTracks = Boolean(instrumentalMidiUrl || (instrumentalTracks.length > 0 && performanceMidi?.has_instrumental_parts)) || multiTrackAudioTracks.length > 0;
  const selectedVoice = voicebanks.find((voice) => voice.id === selectedVoicebankId) ?? null;
  const selectedVoiceLabel = selectedVoice ? selectedVoice.name : "Auto";
  const starterPrompts =
    DEMO_SONGS.find((song) => song.id === activeDemoSongId)?.prompts ?? STARTING_CONVERSATIONS;
  const solfegeSystemLabel = solfegeSystem === "movable_do" ? "Movable Do" : "Fixed Do";
  const solfegeModeLabel =
    solfegeMode === "major"
      ? "Major"
      : solfegeMode === "minor_la_based"
        ? "Minor (La)"
        : "Minor (Do)";
  const solfegeTriggerValue =
    solfegeSystem === "movable_do" ? `${solfegeSystemLabel} · ${solfegeModeLabel}` : solfegeSystemLabel;
  const multiTrackExportPercent =
    multiTrackExportProgress !== null
      ? `${Math.round(Math.max(0, Math.min(1, multiTrackExportProgress)) * 100)}%`
      : null;
  const exportMixRequiredCredits = estimateExportMixCredits(
    shownVocalTracks[0]?.durationSeconds
  );
  const noOpSingleTrackExport = isNoOpSingleTrackExport(shownVocalTracks);

  const instrumentalMidiTrackKeys = useMemo(
    () =>
      (performanceMidi?.instrumental_parts ?? [])
        .filter((part) => part.eligible)
        .map(instrumentalTrackKey),
    [performanceMidi]
  );

  useEffect(() => {
    if (!performanceMidi?.instrumental_parts) {
      setInstrumentalTracks([]);
      return;
    }
    // A reopened Song's saved choices stand in for a row's current values, once.
    const savedChoices = savedInstrumentChoicesRef.current;
    savedInstrumentChoicesRef.current = null;
    setInstrumentalTracks((current) => instrumentRowsFromMidi(performanceMidi, savedChoices, current));
  }, [performanceMidi]);

  const updateInstrumentalTrackMute = useCallback((key: string, muted: boolean) => {
    setInstrumentalTracks((current) =>
      current.map((track) =>
        track.key === key && !track.unassigned ? { ...track, muted, solo: muted ? false : track.solo } : track
      )
    );
  }, []);

  const updateInstrumentalTrackSolo = useCallback((key: string, solo: boolean) => {
    setInstrumentalTracks((current) =>
      current.map((track) =>
        track.key === key && !track.unassigned ? { ...track, solo, muted: solo ? false : track.muted } : track
      )
    );
  }, []);

  const updateInstrumentalTrackVolume = useCallback((key: string, volume: number) => {
    const normalized = Math.max(0, Math.min(1, Number.isFinite(volume) ? volume : 1));
    setInstrumentalTracks((current) =>
      current.map((track) => (track.key === key ? { ...track, volume: normalized } : track))
    );
  }, []);

  const updateInstrumentalTrackGmProgram = useCallback((key: string, gmProgram: number) => {
    setInstrumentalTracks((current) =>
      current.map((track) =>
        track.key === key
          ? { ...track, gmProgram, ...(track.unassigned ? { unassigned: false, muted: false } : {}) }
          : track
      )
    );
  }, []);

  const updateInstrumentalBusMute = useCallback((muted: boolean) => {
    setInstrumentalBus((current) => ({ ...current, muted }));
  }, []);

  const updateInstrumentalBusSolo = useCallback((solo: boolean) => {
    setInstrumentalBus((current) => ({ ...current, solo }));
  }, []);

  const updateInstrumentalBusLevel = useCallback((levelDb: number) => {
    const normalized = Math.max(
      INSTRUMENTAL_BUS_MIN_DB,
      Math.min(INSTRUMENTAL_BUS_MAX_DB, Number.isFinite(levelDb) ? levelDb : 0)
    );
    setInstrumentalBus((current) => ({ ...current, levelDb: normalized }));
  }, []);

  useEffect(() => {
    if (!sessionId || !performanceMidi?.has_instrumental_parts) {
      setInstrumentalMidiUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return null;
      });
      return;
    }
    let disposed = false;
    let nextUrl: string | null = null;
    let published = false;
    setInstrumentalMidiUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return null;
    });
    setScorePlayerError(null);
    void fetchInstrumentalMidi(sessionId, playbackExpandRepeats)
      .then((blob) => {
        nextUrl = URL.createObjectURL(blob);
        if (disposed) {
          URL.revokeObjectURL(nextUrl);
          return;
        }
        setInstrumentalMidiUrl((current) => {
          if (current) URL.revokeObjectURL(current);
          published = true;
          return nextUrl;
        });
      })
      .catch((err: unknown) => {
        if (!disposed) {
          setInstrumentalMidiUrl((current) => {
            if (current) URL.revokeObjectURL(current);
            return null;
          });
          setScorePlayerError(
            err instanceof Error ? err.message : "Instrumental MIDI is unavailable."
          );
        }
      });
    return () => {
      disposed = true;
      if (nextUrl && !published) URL.revokeObjectURL(nextUrl);
    };
  }, [performanceMidi?.has_instrumental_parts, playbackExpandRepeats, sessionId]);

  const partOptions = useMemo(() => buildPartOptions(scoreSummary), [scoreSummary]);

  // Refresh after upload/reparse, on a With Repeats change, on a vocal part
  // change, and after any terminal billing state, because a first successful
  // synthesis drops the instrumental component to 0.
  useEffect(() => {
    if (!sessionId || !scoreSummary) {
      setSynthesisEstimate(null);
      setSynthesisEstimateLoading(false);
      return;
    }
    let cancelled = false;
    setSynthesisEstimate(null);
    setSynthesisEstimateLoading(true);
    void fetchSynthesisEstimate(sessionId, expandRepeats, null)
      .then((estimate) => {
        if (!cancelled) setSynthesisEstimate(estimate);
      })
      .catch(() => {
        if (!cancelled) setSynthesisEstimate(null);
      })
      .finally(() => {
        if (!cancelled) setSynthesisEstimateLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    expandRepeats,
    scoreSummary,
    sessionId,
    synthesisEstimateRevision,
  ]);

  const resolveMultiTrackIdentity = useCallback(
    (
      audioTrack?: AudioTrackMetadata
    ): Omit<
      MultiTrackAudioTrack,
      // sourceJobId names a rendition, not a slot, so the caller supplies it.
      | "audioUrl"
      | "durationSeconds"
      | "jobId"
      | "sourceJobId"
      | "muted"
      | "solo"
      | "volume"
    > => {
      const partIndex =
        typeof audioTrack?.part_index === "number" && Number.isFinite(audioTrack.part_index)
          ? audioTrack.part_index
          : null;
      const partId = typeof audioTrack?.part_id === "string" ? audioTrack.part_id.trim() : "";
      const key =
        (typeof audioTrack?.key === "string" && audioTrack.key.trim()) ||
        (partId ? `id:${partId}` : partIndex !== null ? `index:${partIndex}` : "latest-vocal");
      const matchingPart = partOptions.find((option) => option.key === key);
      const label =
        (typeof audioTrack?.label === "string" && audioTrack.label.trim()) ||
        matchingPart?.label ||
        (partId ? `Part ${partId}` : partIndex !== null ? `Part ${partIndex + 1}` : "Latest vocal");
      return {
        key,
        label,
        partId: partId || matchingPart?.part_id || null,
        partIndex: partIndex ?? matchingPart?.part_index ?? null,
        verseNumber: audioTrack?.verse_number ?? scoreSummary?.selected_verse_number ?? null,
      };
    },
    [partOptions, scoreSummary]
  );

  const addOrReplaceMultiTrackAudio = useCallback(
    (
      audioUrl: string,
      audioTrack?: AudioTrackMetadata,
      jobId?: string,
      durationSeconds?: number | null,
      replaceExistingUrl = false,
      expandRepeats?: boolean
    ) => {
      if (!audioUrl) return;
      const identity = resolveMultiTrackIdentity(audioTrack);
      const takeSignature = audioTrack?.take_signature ?? null;
      const currentTakeSignatures = new Map(
        (scoreSummaryRef.current?.parts ?? []).flatMap((part) =>
          part.part_id && part.take_signature ? [[part.part_id, part.take_signature] as const] : []
        )
      );
      setMultiTrackAudioTracks((all) => {
        // A take in the other repeat order cannot play in sync with this one or
        // with the instrumental MIDI, which follows this take. A take sung
        // before its part's music was edited no longer matches the score. Both
        // keep their audio in the chat.
        const inRepeatOrder = (track: MultiTrackAudioTrack) =>
          expandRepeats === undefined ||
          track.expandRepeats === undefined ||
          track.expandRepeats === expandRepeats;
        const matchesScore = (track: MultiTrackAudioTrack) => {
          const current = track.partId ? currentTakeSignatures.get(track.partId) : undefined;
          return !track.takeSignature || !current || track.takeSignature === current;
        };
        const kept = all.filter(
          (track) => track.key === identity.key || (inRepeatOrder(track) && matchesScore(track))
        );
        const current = kept.length === all.length ? all : kept;
        const existing = current.find((track) => track.key === identity.key);
        const hasBackendDuration =
          typeof durationSeconds === "number" &&
          Number.isFinite(durationSeconds) &&
          durationSeconds > 0;
        // A track represents the latest completed take for one score part. A
        // different source job is therefore a new take (for example, solfege
        // then lyrics) and its source, label, and job metadata must change
        // together.
        // Repeated progress polls for the same job can carry freshly signed
        // URLs for the same asset; retain the already decoded source in that
        // case until an on-demand expiry refresh explicitly replaces it.
        const isNewTake = Boolean(jobId && jobId !== existing?.sourceJobId);
        const shouldReplaceAudioUrl = replaceExistingUrl || isNewTake;
        const nextAudioUrl = existing && !shouldReplaceAudioUrl ? existing.audioUrl : audioUrl;
        const nextSourceJobId = shouldReplaceAudioUrl
          ? (jobId ?? existing?.sourceJobId)
          : existing?.sourceJobId;
        if (!nextSourceJobId) {
          // Every rendition comes from a completed job, so this cannot happen.
          // Fail the add rather than hold a track whose audio cannot be
          // identified: it would be uncacheable and unrefreshable.
          console.error(
            "Refusing to add multitrack audio without a source job id",
            { key: identity.key, audioUrl }
          );
          setError("Couldn't add that audio track. Please retry the render.");
          return all;
        }
        const nextTrack: MultiTrackAudioTrack = {
          ...identity,
          audioUrl: nextAudioUrl,
          jobId: jobId ?? existing?.jobId,
          sourceJobId: nextSourceJobId,
          expandRepeats: expandRepeats ?? (isNewTake ? undefined : existing?.expandRepeats),
          takeSignature: takeSignature ?? (isNewTake ? null : existing?.takeSignature ?? null),
          durationSeconds: hasBackendDuration
            ? durationSeconds
            : existing?.audioUrl === nextAudioUrl
              ? existing?.durationSeconds
              : null,
          muted: existing?.muted ?? false,
          solo: existing?.solo ?? false,
          volume: existing?.volume ?? 1,
        };
        if (existing) {
          if (
            existing.audioUrl === nextTrack.audioUrl &&
            existing.jobId === nextTrack.jobId &&
            existing.sourceJobId === nextTrack.sourceJobId &&
            existing.expandRepeats === nextTrack.expandRepeats &&
            existing.takeSignature === nextTrack.takeSignature &&
            existing.durationSeconds === nextTrack.durationSeconds &&
            existing.label === nextTrack.label &&
            existing.partId === nextTrack.partId &&
            existing.partIndex === nextTrack.partIndex &&
            existing.verseNumber === nextTrack.verseNumber
          ) {
            return current.length === all.length ? all : current;
          }
          return current.map((track) => (track.key === identity.key ? nextTrack : track));
        }
        return [...current, nextTrack];
      });
    },
    [resolveMultiTrackIdentity]
  );

  const handleScorePlayerEngineReady = useCallback((requestId: number) => {
    scorePlayerAssetsReadyRef.current = true;
    setScorePlayerAssetsReady(true);
    if (
      !scorePlayerPlayPendingRef.current ||
      requestId !== scorePlayerPlaybackRequestRef.current
    ) {
      return;
    }
    const controls = scorePlayerControlsRef.current;
    if (!controls) return;
    scorePlayerPlayPendingRef.current = false;
    void controls.play().then(() => setMultiTrackPlaying(true)).catch((err: unknown) => {
      setScorePlayerError(err instanceof Error ? err.message : "Unable to start multitrack playback.");
    });
  }, []);

  const handleMultiTrackPlay = useCallback(() => {
    if (scorePlayerControls) {
      logAnalyticsEvent("multitrack_play", {
        ...multiTrackAnalyticsParams(shownVocalTracks),
        has_instrumental_midi: Boolean(instrumentalMidiUrl),
      });
      setScorePlayerError(null);
      if (scorePlayerAssetsReadyRef.current) {
        void scorePlayerControls.play().then(() => setMultiTrackPlaying(true)).catch((err: unknown) => {
          setScorePlayerError(err instanceof Error ? err.message : "Unable to start multitrack playback.");
        });
      } else {
        // The initial preload is still in flight. A user play request waits for it;
        // an earlier failed preload is retried by remounting the player once.
        scorePlayerPlayPendingRef.current = true;
        if (failedVocalDecodes.size) setFailedVocalDecodes(new Map());
        if (scorePlayerError) {
          const requestId = scorePlayerPlaybackRequestRef.current + 1;
          scorePlayerPlaybackRequestRef.current = requestId;
          setScorePlayerPlaybackRequestId(requestId);
        }
      }
      return;
    }
    const playable = shownVocalTracks
      .map((track) => ({ track, waveSurfer: multiTrackWaveSurferRefs.current[track.key] }))
      .filter((entry): entry is { track: MultiTrackAudioTrack; waveSurfer: WaveSurfer } =>
        Boolean(entry.waveSurfer)
      );
    if (!playable.length) return;
    const baseTime =
      playable.find(({ waveSurfer }) => waveSurfer.getCurrentTime() > 0)?.waveSurfer.getCurrentTime() ?? 0;
    logAnalyticsEvent("multitrack_play", {
      ...multiTrackAnalyticsParams(shownVocalTracks),
      playback_start_seconds: Math.round(baseTime),
    });
    playable.forEach(({ track, waveSurfer }) => {
      const duration = waveSurfer.getDuration();
      waveSurfer.setTime(Math.min(baseTime, Number.isFinite(duration) ? duration : baseTime));
      waveSurfer.setVolume(track.volume);
      waveSurfer.setMuted(shouldMuteMultiTrackForPlayback(track, shownVocalTracks));
    });
    void Promise.allSettled(playable.map(({ waveSurfer }) => waveSurfer.play())).then(() => {
      setMultiTrackPlaying(true);
    });
  }, [failedVocalDecodes, instrumentalMidiUrl, shownVocalTracks, scorePlayerControls, scorePlayerError]);

  const handleMultiTrackPause = useCallback(() => {
    scorePlayerPlayPendingRef.current = false;
    if (scorePlayerControls) {
      scorePlayerControls.pause();
      setMultiTrackPlaying(false);
      return;
    }
    Object.values(multiTrackWaveSurferRefs.current).forEach((waveSurfer) => waveSurfer?.pause());
    setMultiTrackPlaying(false);
  }, [scorePlayerControls]);

  const handleMultiTrackStop = useCallback(() => {
    scorePlayerPlayPendingRef.current = false;
    if (scorePlayerControls) {
      scorePlayerControls.stop();
      // Provider Stop returns to the most recent play/seek start. This app has
      // a distinct Pause action, so Stop explicitly returns the transport to
      // the beginning without invoking seekTo's while-playing restart path.
      scorePlayerControls.setCurrentTime(0);
      handleScorePlayerPlaybackPositionChange(0);
      setMultiTrackPlaying(false);
      return;
    }
    Object.values(multiTrackWaveSurferRefs.current).forEach((waveSurfer) => {
      waveSurfer?.stop();
    });
    handleScorePlayerPlaybackPositionChange(0);
    setMultiTrackPlaying(false);
  }, [handleScorePlayerPlaybackPositionChange, scorePlayerControls]);

  useEffect(() => {
    if (currentSong?.share) setShareAvailable(currentSong.share.available);
  }, [currentSong?.share]);

  // A Song started by an upload has no share state yet. Once it has a sung part, read it
  // from the workspace, once per Song.
  const hasSungTrack = multiTrackAudioTracks.length > 0;
  const shareBlocked = shareBlockedReason(currentSong ? { share: currentSong.share, hasSungTrack } : null);
  useEffect(() => {
    const songId = currentSong?.id;
    if (!songId || currentSong?.share || !hasSungTrack) return;
    if (shareLookupSongRef.current === songId) return;
    shareLookupSongRef.current = songId;
    void fetchWorkspace(songId)
      .then((workspace) => {
        const share = workspace.song.share;
        if (!share) return;
        setCurrentSong((current) => (current?.id === songId && !current.share ? { ...current, share } : current));
      })
      .catch(() => undefined);
  }, [currentSong?.id, currentSong?.share, hasSungTrack]);

  const handleShareChange = useCallback((songId: string, share: ShareState) => {
    setCurrentSong((current) =>
      current?.id === songId
        ? { ...current, share: { available: current.share?.available ?? true, ...share } }
        : current
    );
  }, []);

  const finishBrowserMixBounce = useCallback((discard: boolean) => {
    const operation = browserMixBounceRef.current;
    if (!operation || operation.phase === "finalizing") return;
    operation.phase = "finalizing";
    operation.discarded ||= discard;
    traceBrowserMixBounce("finalizing", { discard: operation.discarded, chunkCount: operation.chunks.length });
    setBrowserMixBouncePhase("finalizing");

    const complete = () => {
      void (async () => {
        if (browserMixBounceRef.current !== operation) return;
        try {
          try {
            operation.adapter.masterOutputNode?.disconnect(operation.destination);
          } catch {
            // The provider may already have disposed the adapter while cancelling.
          }
          operation.destination.disconnect();

        if (operation.discarded) {
          setBrowserMixBounceNotice(null);
          return;
        }
        const recording = new Blob(operation.chunks, { type: operation.recordingMimeType });
        traceBrowserMixBounce("recording-complete", { bytes: recording.size, type: operation.recordingMimeType });
        if (!recording.size) {
          setBrowserMixBounceError("The browser did not produce any mixed audio.");
          return;
        }
        if (operation.discarded || browserMixBounceRef.current !== operation) return;
        traceBrowserMixBounce("recording-ready-as-wav");
        const objectUrl = URL.createObjectURL(recording);
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = "sightsinger-mix.wav";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.requestAnimationFrame(() => URL.revokeObjectURL(objectUrl));
        setBrowserMixBounceNotice("Mix downloaded.");
        logAnalyticsEvent("browser_mix_bounce_completed", {
          duration_seconds: Math.round(scorePlayerTimelineDuration),
          file_bytes: recording.size,
          track_count: instrumentalTracks.length + shownVocalTracks.length,
        });
        } catch (error) {
          traceBrowserMixBounce("finalization-failed", {
            error: error instanceof Error ? error.message : String(error),
          });
          if (!operation.discarded) {
            setBrowserMixBounceError(
              error instanceof Error ? error.message : "The browser could not finalize the recorded WAV mix."
            );
          }
        } finally {
          if (browserMixBounceRef.current === operation) {
            browserMixBounceRef.current = null;
            setBrowserMixBouncePhase("idle");
            setBrowserMixBouncePreparation(null);
          }
        }
      })();
    };

    operation.recorder.addEventListener("stop", complete, { once: true });
    if (operation.recorder.state !== "inactive") {
      operation.recorder.stop();
    } else {
      complete();
    }
  }, [instrumentalTracks.length, shownVocalTracks.length, scorePlayerTimelineDuration]);

  browserMixBounceFinalizeRef.current = finishBrowserMixBounce;

  const cancelBrowserMixBounce = useCallback((reason = "user") => {
    const operation = browserMixBounceRef.current;
    browserMixBounceAttemptRef.current += 1;
    traceBrowserMixBounce("cancelled", { reason, hadOperation: Boolean(operation) });
    if (!operation) {
      const wasActive = browserMixBouncePhaseRef.current !== "idle";
      setBrowserMixBouncePhase("idle");
      setBrowserMixBouncePreparation(null);
      setBrowserMixBounceNotice(null);
      if (wasActive) logAnalyticsEvent("browser_mix_bounce_cancelled");
      return;
    }
    operation.discarded = true;
    scorePlayerPlayPendingRef.current = false;
    scorePlayerControlsRef.current?.stop();
    setMultiTrackPlaying(false);
    finishBrowserMixBounce(true);
    logAnalyticsEvent("browser_mix_bounce_cancelled");
  }, [finishBrowserMixBounce]);

  browserMixBounceCancelRef.current = cancelBrowserMixBounce;

  useEffect(() => () => {
    browserMixBounceCancelRef.current("player-unmount");
  }, []);

  const handleBrowserMixBounce = useCallback(async () => {
    if (browserMixBounceRef.current) return;
    const attempt = browserMixBounceAttemptRef.current + 1;
    browserMixBounceAttemptRef.current = attempt;
    const adapter = scorePlayerAdapterRef.current;
    const controls = scorePlayerControlsRef.current;
    const timelineDuration = scorePlayerTimelineDuration || estimatedDuration || 0;
    traceBrowserMixBounce("requested", {
      hasAdapter: Boolean(adapter),
      hasControls: Boolean(controls),
      assetsReady: scorePlayerAssetsReadyRef.current,
      timelineDuration,
      audioContextState: adapter?.audioContext.state ?? null,
    });
    if (!controls || !adapter?.masterOutputNode || !scorePlayerAssetsReadyRef.current) {
      setBrowserMixBounceError("The score player is still loading. Please try again in a moment.");
      return;
    }
    if (!timelineDuration || timelineDuration > BROWSER_MIX_BOUNCE_MAX_DURATION_SECONDS) {
      setBrowserMixBounceError("Real-time mix download is available for scores up to 5 minutes.");
      return;
    }
    if (!hasAudibleScorePlayerMix(shownVocalTracks, instrumentalTracks, instrumentalBus)) {
      setBrowserMixBounceError("No audible tracks are selected.");
      return;
    }
    if (
      typeof window.MediaStreamAudioDestinationNode === "undefined" ||
      typeof adapter.audioContext.createMediaStreamDestination !== "function"
    ) {
      setBrowserMixBounceError("Real-time WAV download is not supported by this browser.");
      return;
    }

    setBrowserMixBounceError(null);
    setBrowserMixBounceNotice(null);
    setBrowserMixBouncePhase("preparing");
    setBrowserMixBouncePreparation("Preparing WAV recorder…");
    try {
      const resumeAudioContext = primeScorePlayerAudioContext();
      if (!resumeAudioContext) {
        throw new Error("The score player's audio engine is no longer available.");
      }
      traceBrowserMixBounce("awaiting-audio-resume");
      await resumeAudioContext;
      traceBrowserMixBounce("audio-resume-ready", { state: adapter.audioContext.state });
      if (adapter.audioContext.state !== "running") {
        throw new Error("Your browser did not allow audio playback to start for the real-time mix.");
      }
      traceBrowserMixBounce("awaiting-extendable-wav-recorder");
      await ensureExtendableWavRecorderRegistered();
      traceBrowserMixBounce("extendable-wav-recorder-ready");
      if (browserMixBounceAttemptRef.current !== attempt) return;
      if (scorePlayerAdapterRef.current !== adapter) {
        throw new Error("The score player changed while preparing the mix.");
      }
      const destination = adapter.audioContext.createMediaStreamDestination();
      traceBrowserMixBounce("creating-extendable-wav-recorder", {
        recordingMimeType: BROWSER_MIX_RECORDING_MIME_TYPE,
      });
      const recorder = new ExtendableMediaRecorder(destination.stream, {
        mimeType: BROWSER_MIX_RECORDING_MIME_TYPE,
      });
      const operation: BrowserMixBounceOperation = {
        phase: "preparing",
        adapter,
        destination,
        recorder,
        recordingMimeType: BROWSER_MIX_RECORDING_MIME_TYPE,
        chunks: [],
        discarded: false,
      };
      browserMixBounceRef.current = operation;
      const onData = (event: Event) => {
        const data = (event as BlobEvent).data;
        if (data.size) operation.chunks.push(data);
      };
      const onError = (event: Event) => {
        const recorderError = (event as Event & { error?: DOMException }).error;
        traceBrowserMixBounce("recorder-error", {
          error: recorderError?.message ?? "unknown recorder error",
        });
        setBrowserMixBounceError("The browser could not record the mixed audio.");
        operation.discarded = true;
        finishBrowserMixBounce(true);
      };
      recorder.addEventListener("dataavailable", onData);
      recorder.addEventListener("error", onError, { once: true });
      adapter.masterOutputNode.connect(destination);
      traceBrowserMixBounce("recorder-connected", { audioContextState: adapter.audioContext.state });

      scorePlayerPlayPendingRef.current = false;
      controls.stop();
      setMultiTrackPlaying(false);
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const onStart = () => {
          if (settled) return;
          settled = true;
          traceBrowserMixBounce("recorder-started", { state: recorder.state });
          resolve();
        };
        const onStartError = (event: Event) => {
          if (settled) return;
          settled = true;
          const recorderError = (event as Event & { error?: DOMException }).error;
          reject(new Error(recorderError?.message ?? "The recorder could not start."));
        };
        recorder.addEventListener("start", onStart, { once: true });
        recorder.addEventListener("error", onStartError, { once: true });
        traceBrowserMixBounce("recorder-start-requested");
        recorder.start();
        traceBrowserMixBounce("recorder-start-returned", {
          state: recorder.state,
          audioTrackCount: destination.stream.getAudioTracks().length,
          audioTrackStates: destination.stream.getAudioTracks().map((track) => track.readyState),
        });
        // The MediaRecorder state changes synchronously when recording begins.
        // Some Chrome environments do not dispatch the redundant `start`
        // event for an AudioContext-derived MediaStream, so do not allow that
        // missing notification to stall the shared player.
        if (recorder.state === "recording") onStart();
      });
      if (browserMixBounceRef.current !== operation || operation.discarded) return;
      traceBrowserMixBounce("player-play-requested");
      await controls.play(0);
      // usePlaylistControls intentionally catches its underlying play errors,
      // so its promise alone is not proof that Tone's transport started. The
      // adapter is the authoritative state of the shared playback engine.
      if (!adapter.isPlaying()) {
        traceBrowserMixBounce("player-play-rejected", { audioContextState: adapter.audioContext.state });
        throw new Error("The score player could not start playback for the real-time mix.");
      }
      traceBrowserMixBounce("player-playing", { audioContextState: adapter.audioContext.state });
      operation.phase = "recording";
      setBrowserMixBouncePhase("recording");
      logAnalyticsEvent("browser_mix_bounce_started", {
        duration_seconds: Math.round(timelineDuration),
        track_count: instrumentalTracks.length + shownVocalTracks.length,
      });
      setMultiTrackPlaying(true);
    } catch (err) {
      if (browserMixBounceAttemptRef.current !== attempt) return;
      const operation = browserMixBounceRef.current;
      if (operation) {
        operation.discarded = true;
        finishBrowserMixBounce(true);
      } else {
        setBrowserMixBouncePhase("idle");
      }
      setBrowserMixBouncePreparation(null);
      const message = err instanceof Error ? err.message : "Unable to start the real-time mix download.";
      traceBrowserMixBounce("failed", { message, audioContextState: adapter?.audioContext.state ?? null });
      setBrowserMixBounceError(message);
      logAnalyticsEvent("browser_mix_bounce_failed", { stage: "preparing" });
    }
  }, [estimatedDuration, finishBrowserMixBounce, instrumentalBus, instrumentalTracks, shownVocalTracks, primeScorePlayerAudioContext, scorePlayerTimelineDuration]);

  const handleMultiTrackWaveSurferMount = useCallback((trackKey: string, instance: WaveSurfer) => {
    multiTrackWaveSurferRefs.current[trackKey] = instance;
  }, []);

  const handleMultiTrackWaveSurferUnmount = useCallback((trackKey: string) => {
    delete multiTrackWaveSurferRefs.current[trackKey];
  }, []);

  const handleMultiTrackFinished = useCallback(() => {
    const allStopped = Object.values(multiTrackWaveSurferRefs.current).every(
      (waveSurfer) => !waveSurfer || !waveSurfer.isPlaying()
    );
    if (allStopped) setMultiTrackPlaying(false);
  }, []);

  const handleMultiTrackSeek = useCallback((sourceTrackKey: string, time: number) => {
    Object.entries(multiTrackWaveSurferRefs.current).forEach(([trackKey, waveSurfer]) => {
      if (!waveSurfer || trackKey === sourceTrackKey) return;
      const duration = waveSurfer.getDuration();
      waveSurfer.setTime(Math.min(time, Number.isFinite(duration) ? duration : time));
    });
  }, []);

  const updateMultiTrackMute = useCallback((trackKey: string, muted: boolean) => {
    setMultiTrackAudioTracks((current) =>
      current.map((track) =>
        track.key === trackKey ? { ...track, muted, solo: muted ? false : track.solo } : track
      )
    );
  }, []);

  const updateMultiTrackSolo = useCallback((trackKey: string, solo: boolean) => {
    setMultiTrackAudioTracks((current) =>
      current.map((track) =>
        track.key === trackKey ? { ...track, solo, muted: solo ? false : track.muted } : track
      )
    );
  }, []);

  const updateMultiTrackVolume = useCallback((trackKey: string, volume: number) => {
    const normalized = Math.max(0, Math.min(1.2, volume));
    setMultiTrackAudioTracks((current) =>
      current.map((track) => (track.key === trackKey ? { ...track, volume: normalized } : track))
    );
    multiTrackWaveSurferRefs.current[trackKey]?.setVolume(normalized);
  }, []);

  const updateMultiTrackDuration = useCallback((trackKey: string, durationSeconds: number) => {
    if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return;
    setMultiTrackAudioTracks((current) =>
      current.map((track) =>
        track.key === trackKey ? { ...track, durationSeconds } : track
      )
    );
  }, []);

  const handleMultiTrackTrackDownload = useCallback(
    async (track: MultiTrackAudioTrack) => {
      try {
        let nextAudioUrl = track.audioUrl;
        if (sessionId && track.jobId) {
          const payload = await fetchProgress(
            `/sessions/${sessionId}/progress?job_id=${encodeURIComponent(track.jobId)}`
          );
          nextAudioUrl = payload.audio_url || nextAudioUrl;
        }
        if (!nextAudioUrl) {
          setError("No audio available to download.");
          return;
        }
        logAnalyticsEvent("multitrack_track_download", {
          ...multiTrackAnalyticsParams(shownVocalTracks),
          has_part_id: Boolean(track.partId),
          has_verse_number: Boolean(track.verseNumber),
        });
        downloadAudioUrl(nextAudioUrl, "");
      } catch (err: any) {
        setError(err?.message || "Failed to refresh audio download.");
      }
    },
    [shownVocalTracks, sessionId]
  );

  const handleMultiTrackExport = useCallback(async () => {
    if (!sessionId || multiTrackExportProgress !== null) return;
    if (chatTurnInProgressRef.current) {
      setMultiTrackExportError("Stop the running work first");
      return;
    }
    setMultiTrackExportError(null);
    setError(null);
    if (isNoOpSingleTrackExport(shownVocalTracks)) {
      await handleMultiTrackTrackDownload(shownVocalTracks[0]);
      return;
    }
    const audibleTracks = selectAudibleMultiTrackTracks(shownVocalTracks);
    if (!audibleTracks.length) {
      setMultiTrackExportError("No audible tracks selected.");
      return;
    }
    if (creditsLocked) {
      openPaywall("insufficient_credits");
      return;
    }
    const billingReferenceTrack = shownVocalTracks[0];
    if (!billingReferenceTrack?.jobId || exportMixRequiredCredits === null) {
      setMultiTrackExportError("Export credits are still being calculated.");
      return;
    }
    const missingMetadata = audibleTracks.find((track) => !track.jobId || !track.partId);
    if (missingMetadata) {
      setMultiTrackExportError("Export requires generated tracks with part IDs.");
      return;
    }
    logAnalyticsEvent("multitrack_export_mix", {
      ...multiTrackAnalyticsParams(shownVocalTracks),
    });
    setMultiTrackExportProgress(0);
    let exportMessageId: string | null = null;
    // The Song may be left, or its score replaced, while the mix is made.
    const generation = workspaceGenerationRef.current;
    const workspaceChanged = () => workspaceGenerationRef.current !== generation;
    try {
      const response = await exportMix(
        sessionId,
        shownVocalTracks.map((track) => ({
          job_id: track.jobId as string,
          part_id: track.partId as string,
          key: track.key,
          label: track.label,
          verse_number: track.verseNumber ?? null,
          muted: track.muted,
          solo: track.solo,
          volume: track.volume,
        })),
        billingReferenceTrack.jobId
      );
      if (workspaceChanged()) return;
      exportMessageId = crypto.randomUUID();
      appendMessage({
        id: exportMessageId,
        role: "assistant",
        content: "Exporting mix…",
        jobId: response.job_id,
        isProgress: true,
      });
      setChatTurnBusy(true);
      setActiveJobKind("export_mix");
      let latestProgress = 0;
      for (;;) {
        await wait(1200);
        if (workspaceChanged()) return;
        const payload = await fetchProgress(response.progress_url);
        if (workspaceChanged()) return;
        if (typeof payload.progress === "number") {
          latestProgress = Math.max(latestProgress, payload.progress);
          setMultiTrackExportProgress(latestProgress);
        }
        if (payload.status === "done" && payload.audio_url) {
          setMultiTrackExportProgress(1);
          setMessages((current) => current.map((message) =>
            message.id === exportMessageId
              ? { ...message, content: appendProgressMessage(message.content, payload.message || "Mix ready."), isProgress: false }
              : message
          ));
          await downloadAudioUrl(payload.audio_url, "sightsinger-mix.wav");
          break;
        }
        if (payload.status === "stopped") {
          stoppedJobIdsRef.current.add(response.job_id);
          const refunded = stopRefundsRef.current.get(response.job_id);
          const stoppedMessage = `${payload.message || "Stopped."}${refunded ? ` Refunded ${refunded} ${refunded === 1 ? "credit" : "credits"}.` : ""}`;
          setMessages((current) => current.map((message) =>
            message.id === exportMessageId
              ? { ...message, content: appendProgressMessage(message.content, stoppedMessage), isProgress: false }
              : message
          ));
          setSynthesisEstimateRevision((current) => current + 1);
          break;
        }
        if (payload.status === "error") {
          throw new Error(payload.error || "Export failed.");
        }
      }
    } catch (err) {
      if (workspaceChanged()) return;
      if (exportMessageId) {
        setMessages((current) => current.map((message) =>
          message.id === exportMessageId ? { ...message, isProgress: false } : message
        ));
      }
      const message = err instanceof Error ? err.message : "Export failed.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
        setError(message);
        return;
      }
      setMultiTrackExportError(message);
    } finally {
      if (!workspaceChanged()) {
        setMultiTrackExportProgress(null);
        if (exportMessageId) setChatTurnBusy(false);
      }
    }
  }, [creditsLocked, exportMixRequiredCredits, handleMultiTrackTrackDownload, shownVocalTracks, multiTrackExportProgress, openPaywall, sessionId]);

  useEffect(() => {
    shownVocalTracks.forEach((track) => {
      const waveSurfer = multiTrackWaveSurferRefs.current[track.key];
      if (!waveSurfer) return;
      waveSurfer.setMuted(shouldMuteMultiTrackForPlayback(track, shownVocalTracks));
      waveSurfer.setVolume(track.volume);
    });
  }, [shownVocalTracks]);

  const renderVoiceAvatar = (voice: VoicebankOption, className: string) => {
    const imageUrl = failedVoiceImages[voice.id] ? null : voiceImageUrl(voice);
    if (imageUrl) {
      return (
        <img
          src={imageUrl}
          alt=""
          className={className}
          aria-hidden="true"
          onError={() => setFailedVoiceImages((current) => ({ ...current, [voice.id]: true }))}
        />
      );
    }
    return (
      <span className={clsx(className, "initials")} aria-hidden="true">
        {voiceInitials(voice.name)}
      </span>
    );
  };

  const layoutRef = useRef<HTMLDivElement | null>(null);
  const scorePreviewTrapActiveRef = useRef(false);
  const scorePreviewTrapTimerRef = useRef<number | null>(null);
  const dragStateRef = useRef<{
    containerLeft: number;
    containerWidth: number;
  } | null>(null);

  const endScorePreviewTrap = useCallback(() => {
    if (scorePreviewTrapTimerRef.current !== null) {
      window.clearTimeout(scorePreviewTrapTimerRef.current);
      scorePreviewTrapTimerRef.current = null;
    }
    scorePreviewTrapActiveRef.current = false;
  }, []);

  const beginScorePreviewTrap = useCallback(() => {
    if (scorePreviewTrapTimerRef.current !== null) {
      window.clearTimeout(scorePreviewTrapTimerRef.current);
    }
    scorePreviewTrapActiveRef.current = true;
    scorePreviewTrapTimerRef.current = window.setTimeout(() => {
      scorePreviewTrapActiveRef.current = false;
      scorePreviewTrapTimerRef.current = null;
    }, 2000);
  }, []);

  const handleScorePreviewFailure = useCallback((message = SCORE_PREVIEW_RENDER_ERROR) => {
    endScorePreviewTrap();
    setScoreReady(false);
    setScorePreviewError(message);
    setError(message);
    try {
      osmdRef.current?.clear();
    } catch {
      // Ignore cleanup errors from a half-rendered OSMD instance.
    }
    osmdRef.current = null;
    scoreRef.current?.replaceChildren();
  }, [endScorePreviewTrap]);

  useEffect(() => {
    return () => {
      endScorePreviewTrap();
    };
  }, [endScorePreviewTrap]);

  useEffect(() => {
    activeUserIdRef.current = user?.uid ?? null;
  }, [user?.uid]);

  useEffect(() => {
    if (!isAuthenticated || !user) {
      setVoicebanks([]);
      setSelectedVoicebankId(null);
      return;
    }
    let cancelled = false;
    setVoicebanksLoading(true);
    void fetchVoicebanks()
      .then((availableVoicebanks) => {
        if (cancelled) return;
        const sortedVoicebanks = [...availableVoicebanks].sort((left, right) =>
          left.name.localeCompare(right.name, undefined, { sensitivity: "base" })
        );
        setVoicebanks(sortedVoicebanks);
        setFailedVoiceImages({});
        setSelectedVoicebankId((current) =>
          current && sortedVoicebanks.some((voice) => voice.id === current)
            ? current
            : null
        );
      })
      .catch((err) => {
        if (!cancelled) {
          setVoicebanks([]);
          setSelectedVoicebankId(null);
          setError(err?.message || "Failed to load AI voices.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setVoicebanksLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, user]);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    fetchSolfegeSettings(sessionId)
      .then((response) => {
        if (cancelled) return;
        setSolfegeSystem(response.settings.system);
        setSolfegeMode(response.settings.mode);
        setDraftSolfegeSystem(response.settings.system);
        setDraftSolfegeMode(response.settings.mode);
      })
      .catch(() => {
        // Defaults remain usable while the session is still initializing.
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    if (!voiceMenuOpen && !solfegeMenuOpen && !scoreMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        (voicePickerRef.current?.contains(target) ||
          solfegePickerRef.current?.contains(target) ||
          scorePickerRef.current?.contains(target))
      ) {
        return;
      }
      setVoiceMenuOpen(false);
      setSolfegeMenuOpen(false);
      setScoreMenuOpen(false);
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setVoiceMenuOpen(false);
        setSolfegeMenuOpen(false);
        setScoreMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [scoreMenuOpen, solfegeMenuOpen, voiceMenuOpen]);

  // The mix as it would be saved with the Song: mixer values of the vocal
  // tracks, the instrument rows' choices, and the instrument bus.
  const mixSignature = useMemo(
    () =>
      JSON.stringify({
        vocals: multiTrackAudioTracks.map((track) => ({
          key: track.key,
          muted: track.muted,
          solo: track.solo,
          volume: track.volume,
        })),
        instruments: instrumentalTracks
          .filter((track) => track.rawPartId)
          .map((track) => ({
            raw_part_id: track.rawPartId,
            muted: track.muted,
            solo: track.solo,
            volume: track.volume,
            gm_program: track.gmProgram,
            soundfont_bank: track.soundfontBank,
            preset_kind: track.presetKind,
            unassigned: track.unassigned,
          })),
        bus: {
          muted: instrumentalBus.muted,
          solo: instrumentalBus.solo,
          level_db: instrumentalBus.levelDb,
        },
      }),
    [instrumentalBus, instrumentalTracks, multiTrackAudioTracks]
  );

  // Sends the waiting mix now. One request at a time and the latest wins: a
  // mix that is due while another is on its way goes out when that one returns.
  const sendPendingMix = useCallback(() => {
    const state = mixSaveRef.current;
    if (state.timer !== undefined) {
      window.clearTimeout(state.timer);
      state.timer = undefined;
    }
    if (state.inFlight) {
      state.due = true;
      return;
    }
    const pending = state.pending;
    if (!pending) return;
    state.pending = null;
    state.due = false;
    state.inFlight = true;
    void savePlayerMix(pending.songId, pending.body)
      .then(() => {
        if (state.songId === pending.songId && state.epoch === pending.epoch) {
          state.saved = pending.signature;
        }
      })
      .catch(() => {
        // `score_changed`: the mix was for a score that has been replaced, so
        // it is dropped. Any other failure stays quiet; the next change saves.
      })
      .finally(() => {
        state.inFlight = false;
        if (state.due) {
          state.due = false;
          sendPendingMix();
        }
      });
  }, []);

  useEffect(() => {
    const state = mixSaveRef.current;
    const songId = currentSong?.id ?? null;
    // No Song open, or it is still being restored: nothing to save.
    if (!songId || songLoadState !== "idle") return;
    const structure = [
      mixBaselineEpoch,
      songId,
      multiTrackAudioTracks.map((track) => track.key).join(","),
      instrumentalTracks.map((track) => track.key).join(","),
    ].join("|");
    const dropPending = () => {
      if (state.timer !== undefined) window.clearTimeout(state.timer);
      state.timer = undefined;
      state.pending = null;
      state.due = false;
    };
    if (state.epoch !== mixBaselineEpoch || state.songId !== songId) {
      dropPending();
      state.epoch = mixBaselineEpoch;
      state.songId = songId;
      state.structure = structure;
      state.saved = mixSignature;
      return;
    }
    if (state.structure !== structure) {
      // A new take or freshly built instrument rows, not a change by the user.
      state.structure = structure;
      if (!state.pending) {
        state.saved = mixSignature;
        return;
      }
    }
    if (mixSignature === state.saved && !state.inFlight) {
      dropPending();
      return;
    }
    const scoreId = activeScoreIdRef.current;
    if (!scoreId) return;
    state.pending = {
      songId,
      epoch: mixBaselineEpoch,
      signature: mixSignature,
      body: { score_id: scoreId, ...JSON.parse(mixSignature) },
    };
    if (state.timer !== undefined) window.clearTimeout(state.timer);
    state.timer = window.setTimeout(sendPendingMix, MIX_SAVE_DELAY_MS);
  }, [
    currentSong?.id,
    instrumentalTracks,
    mixBaselineEpoch,
    mixSignature,
    multiTrackAudioTracks,
    sendPendingMix,
    songLoadState,
  ]);

  // A waiting mix is sent at once when the tab is hidden or closed.
  useEffect(() => {
    const flush = () => {
      if (document.visibilityState === "hidden") sendPendingMix();
    };
    document.addEventListener("visibilitychange", flush);
    window.addEventListener("pagehide", sendPendingMix);
    return () => {
      document.removeEventListener("visibilitychange", flush);
      window.removeEventListener("pagehide", sendPendingMix);
      sendPendingMix();
    };
  }, [sendPendingMix]);

  // Empties the page of the Song it shows. Nothing is stopped on the server:
  // work that is running keeps running, and only this page stops following it.
  const clearWorkspace = () => {
    sendPendingMix();
    workspaceGenerationRef.current += 1;
    sessionEpochRef.current += 1;
    sessionInitPromiseRef.current = null;
    const priorStream = activeSynthesisStreamRef.current;
    activeSynthesisStreamRef.current = null;
    if (priorStream) void priorStream.cancel();
    if (browserMixBouncePhaseRef.current !== "idle") browserMixBounceCancelRef.current("song-closed");
    handleMultiTrackStop();
    suppressedMultiTrackMessageIdsRef.current = new Set();
    savedInstrumentChoicesRef.current = null;
    activeScoreIdRef.current = null;
    multiTrackWaveSurferRefs.current = {};
    setMessages([]);
    setMultiTrackAudioTracks([]);
    setInstrumentalTracks([]);
    setInstrumentalBus(DEFAULT_INSTRUMENTAL_BUS);
    setInstrumentalTracksExpanded(true);
    setMultiTrackExportProgress(null);
    setMultiTrackExportError(null);
    setAudioUrl(null);
    setActiveProgress(null);
    setDetachedJobUrl(null);
    setStatus(null);
    setChatTurnBusy(false);
    setAwaitingReply(false);
    setScore(null);
    setScoreSummary(null);
    setPerformanceMidi(null);
    setExpandRepeats(false);
    setActiveDemoSongId(null);
    setError(null);
    setNotice(null);
    setScorePreviewError(null);
    setExpandedThoughts({});
    setExpandedDiagnostics({});
    setFeedbackPrompts({});
    setPendingUpload(null);
    setTitleDraft(null);
    setCurrentSong(null);
  };

  const openSong = (songId: string) => {
    clearWorkspace();
    shownSongIdRef.current = songId;
    setSessionId(null);
    setSongLoadState("loading");
    const generation = workspaceGenerationRef.current;
    void (async () => {
      let workspace: SongWorkspace;
      try {
        workspace = await fetchWorkspace(songId);
      } catch (err) {
        if (workspaceGenerationRef.current !== generation) return;
        const missing =
          err instanceof ApiError && (err.status === 404 || err.code === "song_not_found");
        setSongLoadState(missing ? "unavailable" : "failed");
        return;
      }
      if (workspaceGenerationRef.current !== generation) return;

      const summary = workspace.score.score_summary ?? null;
      const rebuilt = rebuildChat(workspace.messages, workspace.jobs);
      // An older take's bubble keeps its audio, but must not push itself into
      // the player when its audio link is refreshed: the player holds the
      // latest take of each part, as the workspace lists them.
      const playerJobIds = new Set(workspace.player.vocals.map((vocal) => vocal.source_job_id));
      suppressedMultiTrackMessageIdsRef.current = new Set(
        rebuilt.messages
          .filter((message) => message.jobId && !message.isProgress && !playerJobIds.has(message.jobId))
          .map((message) => message.id)
      );
      activeScoreIdRef.current = workspace.score.score_id ?? null;
      savedInstrumentChoicesRef.current = workspace.player.instruments;
      setSessionId(workspace.session_id);
      setCurrentSong(workspace.song);
      const solfege = workspace.score.solfege_settings;
      if (solfege) {
        setSolfegeSystem(solfege.system);
        setSolfegeMode(solfege.mode);
        setDraftSolfegeSystem(solfege.system);
        setDraftSolfegeMode(solfege.mode);
      }
      setScoreSummary(summary);
      setPerformanceMidi(summary?.performance_midi ?? null);
      setMessages(rebuilt.messages);
      setMultiTrackAudioTracks(workspace.player.vocals.map(workspaceVocalTrack));
      setExpandRepeats(workspace.player.repeats && summary?.repeat_expansion_available !== false);
      setInstrumentalBus({
        muted: workspace.player.bus.muted,
        solo: workspace.player.bus.solo,
        levelDb: workspace.player.bus.level_db,
      });
      setMixBaselineEpoch((current) => current + 1);
      const demo = pendingDemoSongRef.current;
      pendingDemoSongRef.current = null;
      if (demo?.songId === songId) setActiveDemoSongId(demo.demoSongId);
      if (rebuilt.resume) {
        setChatTurnBusy(true);
        setActiveJobKind(rebuilt.resume.job.job_kind ?? null);
        setActiveProgress({
          messageId: rebuilt.resume.messageId,
          url: rebuilt.resume.job.progress_url,
        });
      } else if (rebuilt.detachedJob) {
        setChatTurnBusy(true);
        setActiveJobKind(rebuilt.detachedJob.job_kind ?? null);
        setDetachedJobUrl(rebuilt.detachedJob.progress_url);
      }
      setSongLoadState("idle");
      requestChatAutoScroll();

      try {
        const data = await fetchScoreXml(workspace.session_id);
        if (workspaceGenerationRef.current !== generation) return;
        setScore({ name: `${workspace.song.title}.xml`, data });
      } catch (err) {
        if (workspaceGenerationRef.current !== generation) return;
        setError(err instanceof Error ? err.message : "Couldn't load this song's score.");
      }
    })();
  };

  // The address decides which Song is shown. Runs only when the address
  // changes, so a page that has just uploaded its first score (and is about to
  // move to that Song's address) is not mistaken for one that left a Song.
  useEffect(() => {
    if (!isAuthenticated || !user) return;
    if (routeSongId) {
      if (routeSongId !== shownSongIdRef.current) openSong(routeSongId);
      return;
    }
    if (shownSongIdRef.current === null) return;
    // Back on the start screen: the session effect makes a fresh session.
    shownSongIdRef.current = null;
    clearWorkspace();
    setSongLoadState("idle");
    setSessionId(null);
  }, [isAuthenticated, routeSongId, user?.uid]);

  // A running job with no chat turn has no bubble. It only keeps the Song busy
  // (Stop shown, replies and uploads held) until it ends.
  useEffect(() => {
    if (!detachedJobUrl) return;
    const generation = workspaceGenerationRef.current;
    let cancelled = false;
    void (async () => {
      for (;;) {
        await wait(1500);
        if (cancelled) return;
        let ended = false;
        try {
          const payload = await fetchProgress(detachedJobUrl);
          ended = payload.status !== "queued" && payload.status !== "running";
        } catch {
          // Keep asking; a job that can no longer be read ends on the next open.
        }
        if (cancelled) return;
        if (ended) {
          if (workspaceGenerationRef.current !== generation) return;
          setDetachedJobUrl(null);
          setChatTurnBusy(false);
          return;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [detachedJobUrl]);

  const ensureSession = async (): Promise<string> => {
    if (sessionId) {
      return sessionId;
    }
    if (sessionInitPromiseRef.current) {
      return sessionInitPromiseRef.current;
    }
    if (!user || !isAuthenticated) {
      throw new Error("Authentication required.");
    }

    const requestUserId = user.uid;
    const sessionEpoch = sessionEpochRef.current;
    const promise = createSession()
      .then((data) => {
        // A Song opened meanwhile has its own session; this one is not adopted.
        if (activeUserIdRef.current === requestUserId && sessionEpochRef.current === sessionEpoch) {
          setSessionId(data.session_id);
        }
        return data.session_id;
      })
      .catch((err) => {
        if (activeUserIdRef.current === requestUserId) {
          setError(err?.message || "Failed to create session.");
        }
        throw err;
      })
      .finally(() => {
        if (sessionInitPromiseRef.current === promise) {
          sessionInitPromiseRef.current = null;
        }
      });

    sessionInitPromiseRef.current = promise;
    return promise;
  };

  useEffect(() => {
    if (!isAuthenticated || !user) {
      setSessionId(null);
      sessionInitPromiseRef.current = null;
      return;
    }
    if (sessionId || sessionInitPromiseRef.current) {
      return;
    }
    // A Song address brings its own session, restored by openSong.
    if (routeSongId) return;
    void ensureSession().catch(() => {
      // Error is stored in component state and retried on the next session-dependent action.
    });
  }, [ensureSession, isAuthenticated, routeSongId, sessionId, user]);

  useEffect(() => {
    if (!isAuthenticated || billing.loading || !billing.stripeCustomerId) return;

    const syncIfNeeded = () => {
      const pendingPortalSync = hasPendingBillingPortalSync();
      const paidPlanMayNeedRefresh = billing.activePlanKey !== "free";
      const now = Date.now();
      if (!pendingPortalSync && !paidPlanMayNeedRefresh) return;
      if (!pendingPortalSync && now - lastBillingSyncAtRef.current < 30000) return;
      if (billingSyncInFlightRef.current) return;

      billingSyncInFlightRef.current = true;
      lastBillingSyncAtRef.current = now;
      void syncBillingSubscription()
        .then(() => clearPendingBillingPortalSync())
        .catch((err) => {
          const message = err instanceof Error ? err.message : "Could not sync billing status.";
          setError(message);
        })
        .finally(() => {
          billingSyncInFlightRef.current = false;
        });
    };

    syncIfNeeded();
    const onFocus = () => syncIfNeeded();
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") syncIfNeeded();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [billing.activePlanKey, billing.loading, billing.stripeCustomerId, isAuthenticated]);

  useEffect(() => {
    if (billing.loading || !isAuthenticated || checkoutReturnSyncStartedRef.current) return;
    if (typeof window === "undefined") return;

    const url = new URL(window.location.href);
    const checkoutStatus = url.searchParams.get("checkout");
    const topupStatus = url.searchParams.get("topup");
    const sessionId = url.searchParams.get("session_id");
    const billingSync = url.searchParams.get("billing") === "sync";
    const returnedFromTopup = topupStatus === "success" || topupStatus === "cancel";
    const returnedFromCheckout = !returnedFromTopup && (checkoutStatus === "success" || Boolean(sessionId));
    const returnedFromPortal = billingSync || hasPendingBillingPortalSync();
    if (!returnedFromCheckout && !returnedFromPortal && !returnedFromTopup) return;

    const cleanupReturnUrl = () => {
      url.searchParams.delete("checkout");
      url.searchParams.delete("topup");
      url.searchParams.delete("session_id");
      url.searchParams.delete("billing");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    };

    if (returnedFromTopup) {
      cleanupReturnUrl();
      setPaywallTrigger(null);
      setPaywallDetail(null);
      return;
    }

    checkoutReturnSyncStartedRef.current = true;
    const syncPromise =
      returnedFromCheckout && sessionId
        ? syncCheckoutSession(sessionId)
        : syncBillingSubscription().then(() => clearPendingBillingPortalSync());

    void syncPromise
      .then(() => {
        cleanupReturnUrl();
        clearPendingCheckoutPlan();
        clearPendingBillingPortalSync();
        setPaywallTrigger(null);
        setPaywallDetail(null);
      })
      .catch((err) => {
        cleanupReturnUrl();
        openPaywall("billing_menu", err instanceof Error ? err.message : "Could not sync billing status.");
      });
  }, [billing.loading, isAuthenticated]);

  useEffect(() => {
    if (!creditsReady || billing.loading || billing.error) return;
    let trigger: PaywallTrigger | null = null;
    if (overdrafted || available < 0) {
      trigger = "overdrafted";
    } else if (isExpired) {
      trigger = "trial_migrated";
    } else if (available <= 0) {
      trigger = "credits_exhausted";
    }
    if (!trigger) return;
    const key = `${trigger}:${user?.uid ?? "anon"}`;
    if (autoPaywallTriggersRef.current.has(key)) return;
    autoPaywallTriggersRef.current.add(key);
    openPaywall(trigger);
  }, [available, billing.error, billing.loading, creditsReady, isExpired, overdrafted, user?.uid]);

  useEffect(() => {
    if (!score) return;

    const handleWindowError = (event: ErrorEvent) => {
      // Media load failures belong to their player's token-refresh handler.
      if (event.target instanceof HTMLMediaElement) return;
      if (!scorePreviewTrapActiveRef.current) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      handleScorePreviewFailure();
    };

    const handleUnhandledRejection = (event: PromiseRejectionEvent) => {
      if (!scorePreviewTrapActiveRef.current) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      handleScorePreviewFailure();
    };

    window.addEventListener("error", handleWindowError, true);
    window.addEventListener("unhandledrejection", handleUnhandledRejection, true);

    return () => {
      window.removeEventListener("error", handleWindowError, true);
      window.removeEventListener("unhandledrejection", handleUnhandledRejection, true);
    };
  }, [handleScorePreviewFailure, score]);

  useEffect(() => {
    if (billing.loading || !isAuthenticated || pendingCheckoutStartedRef.current) return;
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const queryPlan = params.get("checkoutPlan");
    const storedPlan = getStoredPendingCheckoutPlan();
    const planKey = isPaidPlanKey(queryPlan) ? queryPlan : storedPlan;
    const returnedFromHostedBilling =
      params.get("checkout") === "success" ||
      params.has("topup") ||
      params.get("billing") === "sync" ||
      params.has("session_id") ||
      hasPendingBillingPortalSync();

    if (returnedFromHostedBilling) return;
    if (!planKey) return;
    pendingCheckoutStartedRef.current = true;
    clearPendingCheckoutPlan();

    if (billing.activePlanKey !== "free") {
      openPaywall("billing_menu");
      return;
    }

    void startCheckout(planKey as BillingPlanKey).then((url) => {
      window.location.assign(url);
    }).catch((err) => {
      pendingCheckoutStartedRef.current = false;
      openPaywall("billing_menu", err instanceof Error ? err.message : "Could not start Checkout.");
    });
  }, [billing.activePlanKey, billing.loading, isAuthenticated]);

  useEffect(() => {
    if (scorePreviewLayout !== "page" || !scoreRef.current || !score) return;
    let cancelled = false;
    beginScorePreviewTrap();
    setScoreReady(false);
    setScorePreviewError(null);
    activeScoreMeasureRef.current = null;
    scoreRef.current.replaceChildren();
    let osmd: OpenSheetMusicDisplay | null = null;

    void (async () => {
      try {
        if (cancelled || !scoreRef.current) return;

        osmd = new OpenSheetMusicDisplay(scoreRef.current, {
          // The Page layout is fitted below by changing OSMD's own zoom. CSS
          // scaling of only the SVG would leave the OSMD cursor overlay at its
          // original dimensions.
          autoResize: false,
          drawTitle: true,
          followCursor: false,
          cursorsOptions: [{ type: 3, color: "#8b5cf6", alpha: 0.24, follow: false }],
          pageFormat: "A4_P",
          renderSingleHorizontalStaffline: false,
        });
        osmdRef.current = osmd;

        await osmd.load(score.data);
        if (cancelled) return;

        beginScorePreviewTrap();
        if (scorePreviewLayout === "page") {
          // OSMD performs the zoom-aware engraving and recomputes its systems
          // for the fixed A4 page. Preview-pane fitting happens only after
          // engraving and does not participate in the user zoom level.
          applyPageLayoutZoom(osmd, zoomLevel);
          osmd.render();
          const scoreElement = scoreRef.current;
          if (scoreElement) {
            preparePagePreviewWrappers(scoreElement);
            fitPagePreviewToContainer(scoreElement);
          }
        } else {
          osmd.zoom = zoomLevel;
          osmd.render();
        }
        osmd.cursor.hide();
        setScorePreviewError(null);
        setScoreReady(true);
      } catch {
        if (cancelled) return;
        handleScorePreviewFailure();
      }
    })();

    return () => {
      cancelled = true;
      endScorePreviewTrap();
      try {
        osmd?.clear();
      } catch {
        // Ignore cleanup errors from a half-rendered OSMD instance.
      }
      if (osmd && osmdRef.current === osmd) {
        osmdRef.current = null;
      }
    };
  }, [
    beginScorePreviewTrap,
    endScorePreviewTrap,
    handleScorePreviewFailure,
    score,
    scorePreviewLayout,
    zoomLevel,
  ]);

  const requestChatAutoScroll = useCallback(() => {
    setChatAutoScrollRequest((current) => current + 1);
  }, []);

  useEffect(() => {
    const container = chatStreamRef.current;
    if (!container) return;
    if (!shouldAutoScrollRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [chatAutoScrollRequest]);

  // An email's job link opens the Song at ?job={jobId}: scroll to that job's card and
  // highlight it once. If the card isn't in this Song's history, the Song opens as usual.
  const location = useLocation();
  const linkedSingAllJob = new URLSearchParams(location.search).get("job");
  const [highlightedSingAllJob, setHighlightedSingAllJob] = useState<string | null>(null);
  useEffect(() => {
    if (!linkedSingAllJob || !routeSongId || currentSong?.id !== routeSongId) return;
    if (!messages.some((message) => message.singAllJobId === linkedSingAllJob)) return;
    const card = chatStreamRef.current?.querySelector(
      `[data-sing-all-job="${CSS.escape(linkedSingAllJob)}"]`,
    );
    if (!card) return;
    shouldAutoScrollRef.current = false;
    card.scrollIntoView({ block: "center" });
    setHighlightedSingAllJob(linkedSingAllJob);
    navigate(location.pathname, { replace: true });
  }, [linkedSingAllJob, routeSongId, currentSong?.id, messages, navigate, location.pathname]);
  useEffect(() => {
    if (!highlightedSingAllJob) return;
    const timer = window.setTimeout(() => setHighlightedSingAllJob(null), 2500);
    return () => window.clearTimeout(timer);
  }, [highlightedSingAllJob]);

  const handleChatScroll = () => {
    const container = chatStreamRef.current;
    if (!container) return;
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom < 48;
  };

  const appendMessage = (message: Message) => {
    setMessages((prev) => [...prev, message]);
    requestChatAutoScroll();
  };

  const reportedSingAllStops = useRef(new Set<string>());
  const recordSingAllStop = (job: SingAllProgress) => {
    if (job.stage === "done" || reportedSingAllStops.current.has(job.jobId)) return;
    reportedSingAllStops.current.add(job.jobId);
    singAllStop(job.stage, job.elapsedS);
  };

  const handleStopWork = async () => {
    if (!sessionId || stopRequestInFlight) return;
    if (!chatTurnInProgressRef.current && !singAllRunningHere) return;
    setConfirmSingAllStop(false);
    setStopRequestInFlight(true);
    try {
      const response = await stopSessionWork(sessionId);
      const stoppedInThisRequest = new Set(
        response.jobs.filter((job) => job.result === "stopped").map((job) => job.jobId)
      );
      for (const job of response.jobs) {
        if (job.result === "stopped") {
          stopRefundsRef.current.set(job.jobId, job.refundedCredits);
        }
      }
      setMessages((current) => current.map((message) => {
        if (!message.jobId || !stoppedInThisRequest.has(message.jobId) || !stoppedJobIdsRef.current.has(message.jobId)) return message;
        const refunded = stopRefundsRef.current.get(message.jobId);
        if (!refunded) return message;
        const refundMessage = `Refunded ${refunded} ${refunded === 1 ? "credit" : "credits"}.`;
        return { ...message, content: appendProgressMessage(message.content, refundMessage) };
      }));
      if (response.result === "too_late") {
        setNotice("Too late to stop: it's just finishing");
      }
      if (response.singAll) refreshActiveSingAll();
    } catch {
      setNotice("Couldn't stop the work. Please try again.");
    } finally {
      setStopRequestInFlight(false);
    }
  };

  const toggleThoughtSummary = (messageId: string) => {
    setExpandedThoughts((prev) => ({
      ...prev,
      [messageId]: !prev[messageId],
    }));
  };

  const toggleDiagnostics = (messageId: string) => {
    setExpandedDiagnostics((prev) => ({
      ...prev,
      [messageId]: !prev[messageId],
    }));
  };

  const refreshScorePreview = useCallback(async () => {
    if (!sessionId || !score) return;
    const generation = workspaceGenerationRef.current;
    const data = await fetchScoreXml(sessionId);
    if (generation !== workspaceGenerationRef.current) return;
    setScore({ name: score.name, data });
  }, [score, sessionId]);

  // Jobs this page saw running: only their end changes the player under the owner.
  const singAllSeenRunningRef = useRef<Set<string>>(new Set());
  const rememberSingAllJob = useCallback((job: SingAllProgress) => {
    if (job.status === "running") singAllSeenRunningRef.current.add(job.jobId);
    setSingAllJobs((current) => ({ ...current, [job.jobId]: job }));
  }, []);

  // A Sing All this tab saw running chimes once when it ends, if the tab is in the background.
  const singAllNoticedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    Object.values(singAllJobs).forEach((job) => {
      if (job.status === "running" || job.status === "needs_decision") return;
      if (!singAllSeenRunningRef.current.has(job.jobId) || singAllNoticedRef.current.has(job.jobId)) return;
      singAllNoticedRef.current.add(job.jobId);
      noticeInBackground(singAllTabReturn);
    });
  }, [singAllJobs]);

  const loadSingAllJob = useCallback(
    (jobId: string) => {
      fetchSingAllJob(jobId)
        .then(rememberSingAllJob)
        .catch(() => {
          // The card keeps waiting; reopening the Song asks again.
        });
    },
    [rememberSingAllJob]
  );

  // The caller's running Sing All, from any Song: asked once on sign-in...
  useEffect(() => {
    if (!isAuthenticated || !user) {
      setActiveSingAll(null);
      return;
    }
    let cancelled = false;
    fetchActiveSingAll()
      .then((job) => {
        if (cancelled) return;
        setActiveSingAll(job);
        if (job) rememberSingAllJob(job);
      })
      .catch(() => {
        // No pill until the next sign-in or "Sing it".
      });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, user?.uid, rememberSingAllJob]);

  // ...then every few seconds while it runs. When it ends, the job is read once
  // more for its ended card.
  const activeSingAllId = activeSingAll?.jobId ?? null;
  useEffect(() => {
    if (!activeSingAllId) return;
    let cancelled = false;
    void (async () => {
      for (;;) {
        await wait(SING_ALL_POLL_MS);
        if (cancelled) return;
        let job: SingAllProgress | null;
        try {
          job = await fetchActiveSingAll();
        } catch {
          continue;
        }
        if (cancelled) return;
        if (job?.jobId === activeSingAllId) {
          rememberSingAllJob(job);
          setActiveSingAll(job);
          continue;
        }
        loadSingAllJob(activeSingAllId);
        setActiveSingAll(job);
        if (job) rememberSingAllJob(job);
        return;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeSingAllId, loadSingAllJob, rememberSingAllJob]);

  const handleSingAllStarted = (messageId: string, jobId: string, progress: SingAllProgress | null) => {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? { ...message, singAllJobId: jobId } : message))
    );
    if (progress) {
      rememberSingAllJob(progress);
      setActiveSingAll(progress);
      return;
    }
    fetchActiveSingAll()
      .then((job) => {
        setActiveSingAll(job);
        if (job) rememberSingAllJob(job);
      })
      .catch(() => loadSingAllJob(jobId));
  };

  // Step 6: a paused job answered. Going on keeps it active; a cancel ends it.
  const handleSingAllJobUpdated = (job: SingAllProgress) => {
    rememberSingAllJob(job);
    const active = job.status === "running" || job.status === "needs_decision";
    setActiveSingAll((current) => (active ? job : current?.jobId === job.jobId ? null : current));
  };

  // A retry is a new job: its card joins the chat, as the history shows it after a reload.
  // The score bar's Download zip: the Song's latest Sing All whose zip was made.
  const songZipJobId = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const job = messages[index].singAllJobId ? singAllJobs[messages[index].singAllJobId!] : undefined;
      if (job && job.songId === sessionId && job.package?.state === "done") return job.jobId;
    }
    return null;
  }, [messages, singAllJobs, sessionId]);

  const handleSingAllRetryStarted = (job: SingAllProgress) => {
    setMessages((current) => [
      ...current,
      { id: `sing-all-${job.jobId}`, role: "assistant", content: "", singAllJobId: job.jobId },
    ]);
    rememberSingAllJob(job);
    setActiveSingAll(job);
    // The retried job now links to this one in place of its Retry button.
    if (job.retryOf) loadSingAllJob(job.retryOf);
  };

  const handleSingAllStop = async (job: SingAllProgress) => {
    recordSingAllStop(job);
    try {
      const response = await stopSessionWork(job.songId);
      if (response.singAll?.result === "too_late") {
        setNotice("Too late to stop: it's just finishing");
      }
    } catch {
      setNotice("Couldn't stop the work. Please try again.");
    }
    refreshActiveSingAll();
  };

  // After a stop: the card shows "Stopping…" at once; the poll follows the job to its end.
  const refreshActiveSingAll = () => {
    fetchActiveSingAll()
      .then((job) => {
        if (job) rememberSingAllJob(job);
      })
      .catch(() => {});
  };

  // Takes made by a Sing All in the Song on screen: each new one reloads the player
  // and the score, which the job may have split. A finished zip reloads it once more,
  // with the instruments and the bus: the job set the player to the full mix's balance.
  const singAllTakesHere = Object.values(singAllJobs)
    .filter((job) => job.songId === sessionId)
    .reduce((sum, job) => sum + job.counts.takes.done, 0);
  const singAllPackagesHere = Object.values(singAllJobs).filter(
    (job) =>
      job.songId === sessionId &&
      job.package?.state === "done" &&
      singAllSeenRunningRef.current.has(job.jobId)
  ).length;
  const singAllTakesSeenRef = useRef<{ songId: string | null; done: number; packaged: number }>({
    songId: null,
    done: 0,
    packaged: 0,
  });
  useEffect(() => {
    const seen = singAllTakesSeenRef.current;
    singAllTakesSeenRef.current = { songId: sessionId, done: singAllTakesHere, packaged: singAllPackagesHere };
    if (!sessionId || seen.songId !== sessionId) return;
    const balanced = singAllPackagesHere > seen.packaged;
    if (singAllTakesHere <= seen.done && !balanced) return;
    const generation = workspaceGenerationRef.current;
    void (async () => {
      let workspace: SongWorkspace;
      try {
        workspace = await fetchWorkspace(sessionId);
      } catch {
        return;
      }
      if (workspaceGenerationRef.current !== generation) return;
      const summary = workspace.score.score_summary ?? null;
      if (balanced) {
        savedInstrumentChoicesRef.current = workspace.player.instruments;
        setInstrumentalBus({
          muted: workspace.player.bus.muted,
          solo: workspace.player.bus.solo,
          levelDb: workspace.player.bus.level_db,
        });
      }
      setScoreSummary(summary);
      setPerformanceMidi(summary?.performance_midi ?? null);
      setMultiTrackAudioTracks(workspace.player.vocals.map(workspaceVocalTrack));
      setMixBaselineEpoch((current) => current + 1);
      try {
        await refreshScorePreview();
      } catch {
        // The preview keeps the score it shows.
      }
    })();
  }, [sessionId, singAllTakesHere, singAllPackagesHere, refreshScorePreview]);

  // The single sink for job progress. Stream events and the polling fallback
  // both land here, so audio, MIDI and estimate updates cannot depend on which
  // transport delivered the job's final status.
  const handleProgressPayload = useCallback(
    async (
      messageId: string,
      payload: ProgressResponse,
      generation: number
    ): Promise<boolean> => {
      if (generation !== workspaceGenerationRef.current) return false;
      if (payload.score_id && payload.score_id !== activeScoreIdRef.current) return false;
      setJobKindPending(false);
      if (payload.job_kind && payload.status !== "stopped") {
        setActiveJobKind(payload.job_kind);
      }
      const nextAudioUrl = payload.audio_url;
      const isTerminalProgress = isTerminalProgressStatus(payload.status);
      if (payload.status === "stopped" && payload.job_id) {
        stoppedJobIdsRef.current.add(payload.job_id);
      }
      const refunded = payload.job_id ? stopRefundsRef.current.get(payload.job_id) : undefined;
      setMessages((prev) => {
        const messageIndex = prev.findIndex((msg) => msg.id === messageId);
        if (messageIndex < 0) return prev;

        const message = prev[messageIndex];
        const nextMessageState = applyProgressToMessage(message, payload, refunded);
        if (nextMessageState === message) return prev;

        const next = [...prev];
        next[messageIndex] = nextMessageState;
        return next;
      });
      if (nextAudioUrl && payload.status !== "stopped") {
        setAudioUrl((current) => current || nextAudioUrl);
        if (payload.job_kind !== "preprocess") {
          addOrReplaceMultiTrackAudio(
            nextAudioUrl,
            payload.audio_track,
            payload.job_id,
            payload.actual_duration_seconds,
            false,
            payload.expand_repeats
          );
        }
      }
      if (payload.performance_midi) {
        // Every completed synthesis carries this metadata, so applying it
        // unconditionally rebuilt the instrumental tracks even when the job
        // only re-sang a vocal part. Adopt it when the job actually published
        // MIDI, or when we have nothing yet and need it to hydrate.
        setPerformanceMidi((current) =>
          payload.performance_midi_published || current === null
            ? payload.performance_midi ?? null
            : current
        );
      }
      if (payload.status === "done" && payload.review_required) {
        await refreshScorePreview();
      }
      if (generation !== workspaceGenerationRef.current) return false;
      if (payload.warning) {
        setError(payload.warning);
      }
      if (payload.status === "done" || payload.status === "stopped") {
        setActiveProgress((current) =>
          current?.messageId === messageId ? null : current
        );
        setChatTurnBusy(false);
      } else if (payload.status === "error") {
        setActiveProgress((current) =>
          current?.messageId === messageId ? null : current
        );
        setChatTurnBusy(false);
        const fallbackError =
          payload.job_kind === "preprocess" ? "Preprocess failed." : "Synthesis failed.";
        const baseMessage = payload.message || fallbackError;
        setError(
          payload.error && payload.error !== baseMessage
            ? `${baseMessage} Reason: ${payload.error}`
            : baseMessage
        );
      } else if (payload.status === "action_required") {
        setActiveProgress((current) =>
          current?.messageId === messageId ? null : current
        );
        setChatTurnBusy(false);
      }
      if (isTerminalProgress) {
        requestChatAutoScroll();
        setSynthesisEstimateRevision((current) => current + 1);
      }
      return isTerminalProgress;
    },
    [addOrReplaceMultiTrackAudio, refreshScorePreview, requestChatAutoScroll]
  );

  useEffect(() => {
    if (!activeProgress) return;
    let cancelled = false;
    // Set once nothing more will be polled: a terminal status, the end of a
    // bounded recovery, or giving up.
    let stopped = false;
    let timer: number | undefined;
    let completedAttempts = 0;
    // The current run of failed polls; a successful poll ends it.
    let failingSince: number | null = null;
    let consecutiveFailures = 0;
    let successfulPolls = 0;
    const generation = workspaceGenerationRef.current;

    const clearRecoveryError = () => {
      setError((current) =>
        current === SYNTHESIS_STREAM_RECONNECTING_MESSAGE ||
        current === SYNTHESIS_STATUS_UNCONFIRMED_MESSAGE
          ? null
          : current
      );
    };

    const finishBoundedRecovery = () => {
      stopped = true;
      setMessages((current) =>
        current.map((message) =>
          message.id === activeProgress.messageId
            ? { ...message, isProgress: false }
            : message
        )
      );
      setActiveProgress((current) =>
        current?.messageId === activeProgress.messageId ? null : current
      );
      setChatTurnBusy(false);
      // The job's outcome is unknown, but it may have settled credits, so the
      // estimate is re-read. MIDI is left as is rather than guessed.
      setSynthesisEstimateRevision((current) => current + 1);
    };

    const poll = async () => {
      try {
        const payload = await fetchProgress(activeProgress.url);
        if (cancelled || generation !== workspaceGenerationRef.current) return;
        failingSince = null;
        consecutiveFailures = 0;
        successfulPolls += 1;
        const terminal = await handleProgressPayload(
          activeProgress.messageId,
          payload,
          generation
        );
        if (terminal) stopped = true;
        if (terminal || activeProgress.boundedAttempts == null) {
          clearRecoveryError();
        }
        if (!terminal && activeProgress.boundedAttempts != null) {
          completedAttempts += 1;
          if (completedAttempts >= activeProgress.boundedAttempts) {
            finishBoundedRecovery();
          }
        }
      } catch (err: any) {
        if (!cancelled && generation === workspaceGenerationRef.current) {
          if (activeProgress.boundedAttempts != null) {
            completedAttempts += 1;
            if (completedAttempts >= activeProgress.boundedAttempts) {
              finishBoundedRecovery();
            }
            return;
          }
          // The job outlives a lost instance: its record still gets the
          // outcome, so keep polling for a while, backing off, before giving up.
          const now = Date.now();
          if (failingSince === null) failingSince = now;
          consecutiveFailures += 1;
          successfulPolls = 0;
          if (now - failingSince < PROGRESS_POLL_FAILURE_RETRY_MS) {
            setError(SYNTHESIS_STREAM_RECONNECTING_MESSAGE);
            return;
          }
          stopped = true;
          setError(
            err?.message === "Failed to fetch"
              ? SYNTHESIS_STREAM_RECONNECTING_MESSAGE
              : err?.message || "Failed to fetch synthesis progress."
          );
          setActiveProgress(null);
          setChatTurnBusy(false);
        }
      }
    };

    // Each poll is timed from when the previous one started, as a fixed
    // interval would be, so a slow reply does not slow down normal polling.
    const pollThenSchedule = async () => {
      const startedAt = Date.now();
      await poll();
      if (cancelled || stopped) return;
      timer = window.setTimeout(
        () => void pollThenSchedule(),
        Math.max(
          0,
          progressPollDelayMs(successfulPolls, consecutiveFailures) - (Date.now() - startedAt)
        )
      );
    };

    void pollThenSchedule();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeProgress, handleProgressPayload]);

  const consumeSynthesisStream = useCallback(
    async (
      stream: ChatStream,
      messageId: string,
      progressUrl: string,
      generation: number
    ) => {
      activeSynthesisStreamRef.current = stream;
      let reachedTerminalState = false;
      let finalStatusUnconfirmed = false;
      try {
        const iterator = stream[Symbol.asyncIterator]();
        for (;;) {
          const next = await nextStreamEventWithTimeout(
            iterator,
            SYNTHESIS_STREAM_LIVENESS_TIMEOUT_MS
          );
          if (next.done) break;
          const streamEvent = next.value;
          if (generation !== workspaceGenerationRef.current) {
            await stream.cancel();
            return;
          }
          if (streamEvent.event === "heartbeat") continue;
          if (streamEvent.event === "stream-error") {
            finalStatusUnconfirmed = true;
            setError(streamEvent.data.message || SYNTHESIS_STATUS_UNCONFIRMED_MESSAGE);
            break;
          }
          reachedTerminalState = await handleProgressPayload(
            messageId,
            streamEvent.data,
            generation
          );
          if (reachedTerminalState) return;
        }
      } catch {
        if (generation === workspaceGenerationRef.current) {
          await stream.cancel();
          setError(SYNTHESIS_STREAM_RECONNECTING_MESSAGE);
        }
      } finally {
        if (activeSynthesisStreamRef.current === stream) {
          activeSynthesisStreamRef.current = null;
        }
        if (
          !reachedTerminalState &&
          generation === workspaceGenerationRef.current
        ) {
          setActiveProgress({
            messageId,
            url: progressUrl,
            boundedAttempts: finalStatusUnconfirmed
              ? SYNTHESIS_STREAM_ERROR_POLL_ATTEMPTS
              : undefined,
          });
        }
      }
    },
    [handleProgressPayload]
  );

  useEffect(() => {
    return () => {
      const stream = activeSynthesisStreamRef.current;
      activeSynthesisStreamRef.current = null;
      if (stream) void stream.cancel();
    };
  }, []);

  const handleScoreDownload = () => {
    if (!score) {
      setError("No score available to download.");
      return;
    }
    downloadTextFile(scoreDownloadFileName(score.name), score.data);
  };

  const handleScorePrint = async () => {
    if (!score || !scoreReady) {
      setError("No rendered score available to print.");
      return;
    }

    const printWindow = window.open("", "_blank", "width=1024,height=768");
    if (!printWindow) {
      window.print();
      return;
    }

    const title = score.name ? `${score.name} score` : "SightSinger score";
    printWindow.document.open();
    printWindow.document.write(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>${escapeHtml(title)}</title>
  </head>
  <body>Preparing score print preview...</body>
</html>`);
    printWindow.document.close();

    let scoreMarkup: string;
    try {
      scoreMarkup = await renderScorePageLayoutForPrint(score.data, zoomLevel);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unable to prepare score print preview.";
      setError(message);
      printWindow.close();
      return;
    }

    printWindow.document.open();
    printWindow.document.write(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>${escapeHtml(title)}</title>
    <style>
      @page { margin: 8mm; }
      * {
        box-sizing: border-box;
      }
      html, body {
        margin: 0;
        padding: 0;
        background: #ffffff;
        color: #111827;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .score {
        width: 100%;
      }
      .score-page {
        width: 100%;
        margin: 0;
        padding: 0;
        break-after: page;
        page-break-after: always;
      }
      .score-page:last-child {
        break-after: auto;
        page-break-after: auto;
      }
      .score-page svg {
        display: block;
        width: 100%;
        height: auto;
        max-width: 100%;
        margin: 0 auto;
        overflow: visible;
      }
      @media print {
        .score-page {
          break-inside: avoid;
          page-break-inside: avoid;
        }
      }
    </style>
  </head>
  <body>
    <div class="score">${scoreMarkup}</div>
    <script>
      window.addEventListener("load", () => {
        window.focus();
        window.print();
      });
    </script>
  </body>
</html>`);
    printWindow.document.close();
  };

  const progressUrlForJob = (progressUrl: string, jobId?: string): string => {
    if (!jobId) return progressUrl;
    try {
      const url = new URL(progressUrl, window.location.origin);
      if (!url.searchParams.has("job_id")) {
        url.searchParams.set("job_id", jobId);
      }
      return url.toString();
    } catch {
      return progressUrl;
    }
  };

  const refreshMessageAudioUrl = useCallback(async (
    messageId: string,
    progressUrl?: string,
    jobId?: string
  ): Promise<string | null> => {
    if (!progressUrl) return null;
    const refreshUrl = progressUrlForJob(progressUrl, jobId);
    const generation = workspaceGenerationRef.current;
    const pending = audioRefreshPromisesRef.current[messageId];
    if (pending) {
      return pending;
    }
    const refreshPromise = (async () => {
      const payload = await fetchProgress(refreshUrl);
      const nextAudioUrl = payload.audio_url;
      if (!nextAudioUrl) return null;
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === messageId
            ? {
                ...msg,
                audioUrl: nextAudioUrl,
                audioTrack: payload.audio_track ?? msg.audioTrack,
                progressUrl: refreshUrl,
                jobId: payload.job_id ?? msg.jobId,
                feedback: payload.feedback ?? msg.feedback,
              }
            : msg
        )
      );
      const belongsToWorkspace = !suppressedMultiTrackMessageIdsRef.current.has(messageId)
        && generation === workspaceGenerationRef.current
        && (!payload.score_id || payload.score_id === activeScoreIdRef.current);
      if (belongsToWorkspace) {
        setAudioUrl((current) => (current ? nextAudioUrl : current));
      }
      if (belongsToWorkspace && payload.job_kind !== "preprocess") {
        addOrReplaceMultiTrackAudio(
          nextAudioUrl,
          payload.audio_track,
          payload.job_id,
          payload.actual_duration_seconds,
          true,
          payload.expand_repeats
        );
      }
      return nextAudioUrl;
    })();
    audioRefreshPromisesRef.current[messageId] = refreshPromise;
    try {
      return await refreshPromise;
    } finally {
      delete audioRefreshPromisesRef.current[messageId];
    }
  }, [addOrReplaceMultiTrackAudio]);

  const handleScorePlayerEngineError = useCallback(
    (message: string | null) => {
      if (!message) {
        setScorePlayerError(null);
        return;
      }
      const isExpiredToken = /unauthorized|\b401\b/i.test(message);
      if (!isExpiredToken || !scorePlayerPlayPendingRef.current) {
        setScorePlayerError(message);
        return;
      }

      // An audio resource is only renewed as a direct response to the Play action
      // that caused its fetch. It is never renewed while the player is idle.
      const loadedJobIds = new Set(
        multiTrackAudioTracks.map((track) => track.jobId).filter((jobId): jobId is string => Boolean(jobId))
      );
      const refreshTargets = messages.filter((candidate) =>
        candidate.role === "assistant" &&
        Boolean(candidate.audioUrl) &&
        Boolean(candidate.progressUrl) &&
        Boolean(candidate.jobId) &&
        loadedJobIds.has(candidate.jobId as string) &&
        !suppressedMultiTrackMessageIdsRef.current.has(candidate.id)
      );
      const newTargets = refreshTargets.filter((candidate) => {
        const attemptKey = `${candidate.id}:${candidate.audioUrl}`;
        if (scorePlayerAudioRefreshAttemptsRef.current.has(attemptKey)) return false;
        scorePlayerAudioRefreshAttemptsRef.current.add(attemptKey);
        return true;
      });
      if (!newTargets.length) {
        setScorePlayerError(message);
        return;
      }

      setScorePlayerError("Refreshing expired vocal audio access…");
      void Promise.all(
        newTargets.map((candidate) =>
          refreshMessageAudioUrl(candidate.id, candidate.progressUrl, candidate.jobId)
            .then((audioUrl) => ({ jobId: candidate.jobId, audioUrl }))
            .catch(() => ({ jobId: candidate.jobId, audioUrl: null }))
        )
      ).then((refreshed) => {
        const refreshedUrls = new Map(
          refreshed
            .filter((result): result is { jobId: string; audioUrl: string } =>
              Boolean(result.jobId && result.audioUrl)
            )
            .map((result) => [result.jobId, result.audioUrl])
        );
        if (!refreshedUrls.size) {
          scorePlayerPlayPendingRef.current = false;
          setScorePlayerError("Unable to refresh vocal audio access. Please try again.");
          return;
        }
        setMultiTrackAudioTracks((current) =>
          current.map((track) =>
            track.jobId && refreshedUrls.has(track.jobId)
              ? { ...track, audioUrl: refreshedUrls.get(track.jobId) as string }
              : track
          )
        );
        const requestId = scorePlayerPlaybackRequestRef.current + 1;
        scorePlayerPlaybackRequestRef.current = requestId;
        setScorePlayerPlaybackRequestId(requestId);
        setScorePlayerError(null);
      });
    },
    [messages, multiTrackAudioTracks, refreshMessageAudioUrl]
  );

  // Decode each take once, when it enters the player, and forget the audio of
  // takes that left it. The player only receives what is decoded here, so it
  // never downloads a take itself.
  const handleScorePlayerEngineErrorRef = useRef(handleScorePlayerEngineError);
  handleScorePlayerEngineErrorRef.current = handleScorePlayerEngineError;
  useEffect(() => {
    const live = new Set(multiTrackAudioTracks.map((track) => track.sourceJobId));
    liveVocalJobsRef.current = live;
    setDecodedVocals((current) => withoutKeysOutside(current, live));
    setFailedVocalDecodes((current) => withoutKeysOutside(current, live));
    multiTrackAudioTracks.forEach((track) => {
      const jobId = track.sourceJobId;
      const audioUrl = track.audioUrl;
      if (decodedVocals.has(jobId) || vocalDecodesInFlightRef.current.has(jobId)) return;
      if (failedVocalDecodes.get(jobId) === audioUrl) return;
      vocalDecodesInFlightRef.current.add(jobId);
      void decodeVocalAudio(audioUrl).then(
        (buffer) => {
          vocalDecodesInFlightRef.current.delete(jobId);
          // A take replaced or cleared while decoding is not stored.
          if (!liveVocalJobsRef.current.has(jobId)) return;
          setDecodedVocals((current) => new Map(current).set(jobId, buffer));
        },
        (err: unknown) => {
          vocalDecodesInFlightRef.current.delete(jobId);
          if (!liveVocalJobsRef.current.has(jobId)) return;
          setFailedVocalDecodes((current) => new Map(current).set(jobId, audioUrl));
          handleScorePlayerEngineErrorRef.current(
            err instanceof Error ? err.message : "Failed to load vocal audio."
          );
        }
      );
    });
  }, [decodedVocals, failedVocalDecodes, multiTrackAudioTracks]);
  const vocalsDecoding = multiTrackAudioTracks.some(
    (track) =>
      !decodedVocals.has(track.sourceJobId) && !failedVocalDecodes.has(track.sourceJobId)
  );

  const shouldOpenFeedbackPrompt = (message: Message): boolean =>
    Boolean(
      message.jobId &&
      message.feedback?.promptCandidate &&
      !message.feedback.prompted &&
        !message.feedback.submitted &&
        !feedbackPromptConsumedThisSessionRef.current
    );

  const openFeedbackPrompt = (message: Message, trigger: "audio_played" | "audio_downloaded") => {
    if (!shouldOpenFeedbackPrompt(message)) return;
    if (feedbackPromptedMessagesRef.current.has(message.id)) return;
    const jobId = message.jobId;
    if (!jobId) return;
    feedbackPromptConsumedThisSessionRef.current = true;
    feedbackPromptedMessagesRef.current.add(message.id);
    setFeedbackPrompts((prev) => ({ ...prev, [message.id]: "open" }));
    setMessages((prev) =>
      prev.map((msg) =>
        msg.id === message.id
          ? { ...msg, feedback: { ...(msg.feedback ?? {}), prompted: true } }
          : msg
      )
    );
    void markFeedbackPrompted(jobId, trigger).catch((err) => {
      setError(err instanceof Error ? err.message : "Could not record feedback prompt.");
    });
  };

  const toFeedbackRatingsRequest = (ratings: FeedbackRatings): FeedbackRatingsRequest => ({
    voiceQuality: ratings["Voice quality"],
    pronunciation: ratings.Pronunciation,
    timingRhythm: ratings["Timing/rhythm"],
    lyricsAlignment: ratings["Lyrics alignment"],
    partSplittingAccuracy: ratings["Part splitting accuracy"],
  });

  const handleFeedbackSubmit = async (
    messageId: string,
    payload: { ratings: FeedbackRatings; feedback: string }
  ) => {
    const message = messages.find((item) => item.id === messageId);
    if (!message?.jobId) {
      setFeedbackPrompts((prev) => {
        const next = { ...prev };
        delete next[messageId];
        return next;
      });
      return;
    }
    try {
      await submitAudioFeedback({
        jobId: message.jobId,
        ratings: toFeedbackRatingsRequest(payload.ratings),
        comment: payload.feedback,
      });
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === messageId
            ? { ...msg, feedback: { ...(msg.feedback ?? {}), submitted: true } }
            : msg
        )
      );
      setFeedbackPrompts((prev) => {
        const next = { ...prev };
        delete next[messageId];
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit feedback.");
    }
  };

  const handleAudioDownload = async (
    messageId: string,
    audioUrl?: string,
    progressUrl?: string,
    jobId?: string
  ) => {
    try {
      let downloadUrl = audioUrl;
      if (!downloadUrl) {
        downloadUrl = await refreshMessageAudioUrl(messageId, progressUrl, jobId) ?? undefined;
      }
      if (!downloadUrl) {
        setError("No audio available to download.");
        return;
      }
      try {
        await downloadAudioUrl(downloadUrl, "");
      } catch (downloadError: unknown) {
        if (!isExpiredAudioDownloadError(downloadError)) throw downloadError;
        const refreshedAudioUrl = await refreshMessageAudioUrl(messageId, progressUrl, jobId);
        if (!refreshedAudioUrl) throw new Error("Audio link expired. Please try again.");
        await downloadAudioUrl(refreshedAudioUrl, "");
      }
      const message = messages.find((item) => item.id === messageId);
      if (message) {
        logAnalyticsEvent("synthesis_audio_download", synthesisAudioAnalyticsParams(message));
        openFeedbackPrompt(message, "audio_downloaded");
      }
    } catch (err: any) {
      setError(err?.message || "Failed to download audio.");
    }
  };

  // Puts a score into the session on screen: the first score of a new Song,
  // or, with `replace`, a new score for the open Song. Replace stops the
  // running work itself; any other upload waits for the work to end.
  const handleUpload = async (
    file: File,
    demoSongId: string | null = null,
    options: { replace?: boolean } = {}
  ) => {
    if (chatTurnInProgressRef.current && !options.replace) return;
    if (browserMixBounceActive) {
      setBrowserMixBounceError("Cancel the real-time mix download before replacing the score.");
      return;
    }
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    setUploading(true);
    setError(null);
    setScorePreviewError(null);
    const sessionEpoch = sessionEpochRef.current;
    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const uploadResponse = await uploadScore(
        activeSessionId,
        file,
        options.replace ? { replace: true } : {}
      );
      // The page moved to another Song meanwhile; this score is not shown here.
      if (sessionEpochRef.current !== sessionEpoch) return;
      activeScoreIdRef.current = uploadResponse.score_id ?? null;
      workspaceGenerationRef.current += 1;
      savedInstrumentChoicesRef.current = null;
      const uploadedSong = uploadResponse.song;
      if (uploadedSong) {
        // A Replace keeps the Song, and with it its link; the upload answer doesn't carry it.
        setCurrentSong((current) =>
          current?.id === uploadedSong.id && current.share
            ? { ...uploadedSong, share: current.share }
            : uploadedSong
        );
        // The cleared player is this score's saved mix.
        setMixBaselineEpoch((current) => current + 1);
        if (shownSongIdRef.current !== uploadedSong.id) {
          shownSongIdRef.current = uploadedSong.id;
          navigate(`/app/song/${uploadedSong.id}`, { replace: true });
        }
      }
      if (options.replace) settleInterruptedBubbles(messages);
      const priorStream = activeSynthesisStreamRef.current;
      activeSynthesisStreamRef.current = null;
      if (priorStream) void priorStream.cancel();
      messages.forEach((message) => {
        suppressedMultiTrackMessageIdsRef.current.add(message.id);
      });
      handleMultiTrackStop();
      setMultiTrackAudioTracks([]);
      setInstrumentalTracks([]);
      setInstrumentalBus(DEFAULT_INSTRUMENTAL_BUS);
      setInstrumentalTracksExpanded(true);
      setMultiTrackExportProgress(null);
      setMultiTrackExportError(null);
      multiTrackWaveSurferRefs.current = {};
      // A replacement upload starts a new score workflow while retaining the chat transcript.
      setAudioUrl(null);
      setActiveProgress(null);
      setStatus(null);
      setChatTurnBusy(false);
      const summary = uploadResponse.score_summary ?? null;
      if (uploadResponse.solfege_settings) {
        setSolfegeSystem(uploadResponse.solfege_settings.system);
        setSolfegeMode(uploadResponse.solfege_settings.mode);
        setDraftSolfegeSystem(uploadResponse.solfege_settings.system);
        setDraftSolfegeMode(uploadResponse.solfege_settings.mode);
      }
      setScoreSummary(summary);
      setPerformanceMidi(uploadResponse.performance_midi ?? summary?.performance_midi ?? null);
      setExpandRepeats(false);
      // Say so once, where messages appear; the toggle's tooltip is easy to miss.
      setNotice((current) =>
        summary?.repeat_expansion_available === false
          ? REPEATS_UNAVAILABLE_MESSAGE
          : current === REPEATS_UNAVAILABLE_MESSAGE
            ? null
            : current,
      );
      const data = await fetchScoreXml(activeSessionId);
      if (sessionEpochRef.current !== sessionEpoch) return;
      setScore({ name: file.name, data });
      setActiveDemoSongId(demoSongId);
    } catch (err: any) {
      if (sessionEpochRef.current !== sessionEpoch) return;
      if (options.replace && err instanceof ApiError && err.code === "stop_too_late") {
        // Keep the file; the choice is offered again when the job has ended.
        setNotice(STOP_TOO_LATE_NOTICE);
        setPendingUpload({ file, demoSongId, waitForWorkToEnd: true });
        return;
      }
      const message = err?.message || "Upload failed.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
      }
      setError(message);
    } finally {
      setUploading(false);
    }
  };

  // Replace stopped the open Song's work on the server, and this page stopped
  // following it. Its bubbles are closed here, each with its job's last word.
  const settleInterruptedBubbles = (shown: Message[]) => {
    const interrupted = shown.filter((message) => message.isProgress);
    if (!interrupted.length) return;
    const interruptedIds = new Set(interrupted.map((message) => message.id));
    setMessages((prev) =>
      prev.map((message) =>
        interruptedIds.has(message.id)
          ? {
              ...message,
              isProgress: false,
              // An export mix has no progress link to ask; say so here.
              content: message.progressUrl
                ? message.content
                : appendProgressMessage(message.content, "Stopped."),
            }
          : message
      )
    );
    interrupted.forEach((bubble) => {
      if (!bubble.progressUrl) return;
      void fetchProgress(progressUrlForJob(bubble.progressUrl, bubble.jobId))
        .then((payload) => {
          if (!isTerminalProgressStatus(payload.status)) return;
          setMessages((prev) =>
            prev.map((message) =>
              message.id === bubble.id ? applyProgressToMessage(message, payload) : message
            )
          );
        })
        .catch(() => {
          // The bubble is already closed; its last line is simply missing.
        });
    });
  };

  // Every way of adding a score comes through here. With no Song open the
  // score starts one. With a Song open the user chooses: new Song, or replace.
  const requestUpload = (file: File, demoSongId: string | null = null) => {
    if (songLoadState !== "idle") return;
    if (!currentSong) {
      void handleUpload(file, demoSongId);
      return;
    }
    if (awaitingReply || uploading) return;
    if (browserMixBounceActive) {
      setBrowserMixBounceError("Cancel the real-time mix download before replacing the score.");
      return;
    }
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    setPendingUpload({ file, demoSongId, waitForWorkToEnd: false });
  };

  // "New song" from the popup: the open Song stays as it is, with its work
  // still running, and the score goes into a Song of its own.
  const startNewSongWith = async (file: File, demoSongId: string | null) => {
    setPendingUpload(null);
    setUploading(true);
    setError(null);
    const sessionEpoch = sessionEpochRef.current;
    try {
      const created = await createSession();
      const uploadResponse = await uploadScore(created.session_id, file);
      if (sessionEpochRef.current !== sessionEpoch) return;
      const newSongId = uploadResponse.song?.id ?? created.session_id;
      pendingDemoSongRef.current = { songId: newSongId, demoSongId };
      navigate(`/app/song/${newSongId}`);
    } catch (err: any) {
      if (sessionEpochRef.current !== sessionEpoch) return;
      const message = err?.message || "Upload failed.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
      }
      setError(message);
    } finally {
      setUploading(false);
    }
  };

  const replaceScoreWith = (file: File, demoSongId: string | null) => {
    setPendingUpload(null);
    void handleUpload(file, demoSongId, { replace: true });
  };

  // Replace was refused because the job was already finishing. Offer the
  // choice again once that job has ended.
  useEffect(() => {
    if (!pendingUpload?.waitForWorkToEnd || chatTurnInProgress || singAllRunningHere) return;
    setPendingUpload({ ...pendingUpload, waitForWorkToEnd: false });
  }, [chatTurnInProgress, pendingUpload, singAllRunningHere]);

  // Enter and leaving the field both end up here; Escape marks the edit cancelled first.
  const commitTitleEdit = async () => {
    const draft = titleDraft;
    setTitleDraft(null);
    if (titleEditCancelledRef.current) {
      titleEditCancelledRef.current = false;
      return;
    }
    const song = currentSong;
    const title = draft?.trim() ?? "";
    if (!song || !title || title === song.title) return;
    try {
      const renamed = await renameSong(song.id, title);
      setCurrentSong((current) =>
        current && current.id === song.id
          ? {
              ...current,
              title: renamed.title,
              title_edited: true,
              info: current.info ? { ...current.info, title: renamed.title } : current.info,
            }
          : current
      );
    } catch (err: any) {
      setError(err?.message || "Couldn't rename the song.");
    }
  };

  // A demo song is a score shipped with the app, loaded exactly like an upload.
  const loadDemoSong = async (song: DemoSong) => {
    setScoreMenuOpen(false);
    if (uploadBlocked) return;
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    logAnalyticsEvent("demo_song_load", { demo_song_id: song.id });
    let file: File;
    try {
      const response = await fetch(song.file);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      file = new File([await response.blob()], `${song.id}.xml`, { type: "application/xml" });
    } catch {
      setError(`Couldn't load the demo song "${song.title}". Please try again.`);
      return;
    }
    requestUpload(file, song.id);
  };

  const chooseScoreFile = () => {
    setScoreMenuOpen(false);
    if (uploadBlocked) return;
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    scoreFileInputRef.current?.click();
  };

  const sendMessage = async (
    content: string,
    voicebankId: string | null = selectedVoicebankId
  ) => {
    if (!content.trim()) return;
    if (browserMixBounceActive) {
      setBrowserMixBounceError("Cancel the real-time mix download before starting another synthesis.");
      return;
    }
    if (chatTurnInProgressRef.current) return;
    if (creditsLocked) {
      openPaywall("chat_blocked");
      return;
    }
    let keepTurnBusy = false;
    // If the Song is left before the reply arrives, the reply is not for this page any more.
    const generation = workspaceGenerationRef.current;
    setChatTurnBusy(true);
    setAwaitingReply(true);
    setActiveJobKind(null);
    setJobKindPending(false);
    setStatus("Thinking...");
    setError(null);
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      content,
    });

    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const { response, stream } = await chat(
        activeSessionId,
        content,
        voicebankId,
        expandRepeats,
        multiTrackAudioTracks.flatMap((track) =>
          track.expandRepeats === undefined
            ? []
            : [{
                part_id: track.partId ?? null,
                label: track.label,
                expand_repeats: track.expandRepeats,
                take_signature: track.takeSignature ?? null,
              }]
        )
      );
      if (workspaceGenerationRef.current !== generation) {
        if (stream) void stream.cancel();
        return;
      }
      setAwaitingReply(false);
      if (response.type === "chat_error") {
        setError(response.message || "LLM request failed. Please try again.");
        return;
      }
      const assistantMessage: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: response.message,
        details: "details" in response ? response.details : undefined,
        attemptMessages: extractAttemptMessages(
          "details" in response ? response.details : undefined
        ),
      };
      if (response.type === "chat_text" && response.singAll) {
        assistantMessage.singAll = response.singAll;
      }
      if (response.type === "chat_audio") {
        assistantMessage.audioUrl = response.audio_url;
        assistantMessage.audioTrack = response.audio_track;
        setAudioUrl(response.audio_url);
        // A synchronous audio response is already a completed new take, so it
        // replaces any previous rendition of the same score part. The backend no
        // longer returns this type -- synthesis always runs as a job -- and a
        // track needs its source job id to be cached or refreshed, so any
        // revival of this path must carry one.
        if (response.job_id) {
          addOrReplaceMultiTrackAudio(
            response.audio_url, response.audio_track, response.job_id, undefined, true
          );
        } else {
          console.error(
            "Ignoring chat_audio without a job id; it cannot be cached or refreshed",
            { audioUrl: response.audio_url }
          );
        }
      }
      if (response.type === "chat_progress") {
        assistantMessage.progressUrl = response.progress_url;
        assistantMessage.isProgress = true;
        assistantMessage.jobId = response.job_id;
        keepTurnBusy = true;
        setJobKindPending(true);
      }
      appendMessage(assistantMessage);
      if ("score_summary" in response && response.score_summary) {
        setScoreSummary(response.score_summary);
        setPerformanceMidi(response.score_summary.performance_midi ?? null);
      }
      if (typeof response.expand_repeats === "boolean" && !repeatsUnavailable) {
        setExpandRepeats(response.expand_repeats);
      }
      if ("solfege_settings" in response && response.solfege_settings) {
        setSolfegeSystem(response.solfege_settings.system);
        setSolfegeMode(response.solfege_settings.mode);
        setDraftSolfegeSystem(response.solfege_settings.system);
        setDraftSolfegeMode(response.solfege_settings.mode);
      }
      if ("warning" in response && response.warning) {
        setError(String(response.warning));
      }
      // Start consuming job progress only after the accepted response's own
      // state is applied: a job update must not be overwritten by the older
      // score summary that came with the request.
      if (response.type === "chat_progress") {
        if (stream) {
          void consumeSynthesisStream(
            stream,
            assistantMessage.id,
            response.progress_url,
            workspaceGenerationRef.current
          );
        } else {
          setActiveProgress({ messageId: assistantMessage.id, url: response.progress_url });
        }
      }
      if ("current_score" in response && response.current_score) {
        await refreshScorePreview();
      }
    } catch (err: any) {
      if (workspaceGenerationRef.current !== generation) return;
      const message = err?.message || "Failed to send message.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
      }
      setError(message);
    } finally {
      if (workspaceGenerationRef.current === generation) {
        setAwaitingReply(false);
        setStatus(null);
        if (!keepTurnBusy) {
          setChatTurnBusy(false);
        }
      }
    }
  };

  const handleSend = async () => {
    if (!input.trim()) return;
    if (chatTurnInProgressRef.current) return;
    if (creditsLocked) {
      openPaywall("chat_blocked");
      return;
    }
    const content = input.trim();
    const voicebankId = selectedVoicebankId;
    setInput("");
    await sendMessage(content, voicebankId);
  };

  const handleConfirmSolfegeSettings = async () => {
    if (!sessionId || solfegeSettingsSaving) return;
    setSolfegeSettingsSaving(true);
    setError(null);
    try {
      const response = await updateSolfegeSettings(sessionId, {
        system: draftSolfegeSystem,
        mode: draftSolfegeMode,
      });
      setSolfegeSystem(response.settings.system);
      setSolfegeMode(response.settings.mode);
      setDraftSolfegeSystem(response.settings.system);
      setDraftSolfegeMode(response.settings.mode);
      if (response.score_summary) {
        setScoreSummary(response.score_summary);
        setPerformanceMidi(response.score_summary.performance_midi ?? null);
      }
      if (score && response.current_score) {
        const data = await fetchScoreXml(sessionId);
        setScore({ name: score.name, data });
      }
      setSolfegeMenuOpen(false);
    } catch (err: any) {
      setError(err?.message || "Failed to update solfege settings.");
    } finally {
      setSolfegeSettingsSaving(false);
    }
  };

  const handleZoom = (delta: number) => {
    const next = Math.min(2, Math.max(0.6, zoomLevel + delta));
    setZoomLevel(Math.round(next * 10) / 10);
  };

  const handleDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (creditsLocked || browserMixBounceActive || uploadBlocked) return;
    if (!isDragging) setIsDragging(true);
  };

  const handleDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node)) return;
    setIsDragging(false);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (uploadBlocked) return;
    if (browserMixBounceActive) {
      setBrowserMixBounceError("Cancel the real-time mix download before replacing the score.");
      return;
    }
    if (creditsLocked) {
      openPaywall("drag_blocked");
      return;
    }
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) requestUpload(file);
  };

  const handleResizeStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!layoutRef.current) return;
    event.preventDefault();
    const rect = layoutRef.current.getBoundingClientRect();
    dragStateRef.current = {
      containerLeft: rect.left,
      containerWidth: rect.width,
    };
    const handleMove = (moveEvent: PointerEvent) => {
      if (!dragStateRef.current) return;
      const delta = moveEvent.clientX - dragStateRef.current.containerLeft;
      const next = (delta / dragStateRef.current.containerWidth) * 100;
      const clamped = Math.min(70, Math.max(30, next));
      setSplitPct(clamped);
    };
    const handleUp = () => {
      dragStateRef.current = null;
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
  };

  const handleJoinWaitlist = (source: WaitlistSource) => {
    setWaitlistSource(source);
    setShowWaitlistModal(true);
  };

  const handleOpenBilling = () => {
    openPaywall("billing_menu");
  };

  const handleOpenBillingPortal = () => {
    void startBillingPortal()
      .then((url) => {
        window.location.assign(url);
      })
      .catch((err) => {
        openPaywall("billing_menu", err instanceof Error ? err.message : "Could not open Billing.");
      });
  };

  const marketingBaseUrl =
    (import.meta.env.VITE_MARKETING_BASE_URL as string | undefined) ?? "/";
  const handleBrandClick = () => {
    if (typeof window === "undefined") return;
    window.location.assign(marketingBaseUrl);
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand" onClick={handleBrandClick} style={{ cursor: "pointer" }}>
          <span className="brand-banner-crop" aria-label="SightSinger">
            <img
              className="brand-banner"
              src="/content/images/logo-hackaton-white.png"
              alt=""
            />
          </span>
        </div>
        {currentSong ? (
          <div className="song-title-slot">
            {titleDraft !== null ? (
              <input
                className="song-title-input"
                aria-label="Song title"
                value={titleDraft}
                maxLength={120}
                autoFocus
                onFocus={(event) => event.currentTarget.select()}
                onChange={(event) => setTitleDraft(event.target.value)}
                onBlur={() => void commitTitleEdit()}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.currentTarget.blur();
                  } else if (event.key === "Escape") {
                    titleEditCancelledRef.current = true;
                    event.currentTarget.blur();
                  }
                }}
              />
            ) : (
              <button
                type="button"
                className="song-title-button"
                data-testid="song-title"
                title="Rename this song"
                onClick={() => setTitleDraft(currentSong.title)}
              >
                <span className="song-title-text">{currentSong.title}</span>
                <Pencil size={13} aria-hidden="true" />
              </button>
            )}
            {currentSong.info && titleDraft === null ? (
              <button
                type="button"
                className="song-info-button"
                data-testid="song-info-open"
                title="Song info"
                aria-label="Song info"
                onClick={() => {
                  songInfoOpened("title");
                  setSongInfoOpen(true);
                }}
              >
                <Info size={15} aria-hidden="true" />
              </button>
            ) : null}
          </div>
        ) : null}
        <div className="header-actions">
          {activeSingAll ? (
            <button
              type="button"
              className="btn-secondary-inline sing-all-pill"
              data-testid="sing-all-pill"
              title={activeSingAll.songId === sessionId ? activeSingAll.title : `Open ${activeSingAll.title}`}
              disabled={activeSingAll.songId !== sessionId && !canLeaveSong}
              onClick={() => {
                if (activeSingAll.songId !== sessionId) navigate(`/app/song/${activeSingAll.songId}`);
              }}
            >
              <Sparkles size={14} aria-hidden="true" />
              <span>Sing All · {singAllPillStage(activeSingAll)}</span>
            </button>
          ) : null}
          {currentSong ? (
            <button
              type="button"
              className="btn-secondary-inline song-nav-button"
              data-testid="new-song"
              disabled={!canLeaveSong}
              title={canLeaveSong ? "Start a new song" : "Wait for the reply first"}
              onClick={() => navigate("/app")}
            >
              <FilePlus size={15} aria-hidden="true" />
              <span className="song-nav-label">New song</span>
            </button>
          ) : null}
          <button
            type="button"
            className="btn-secondary-inline song-nav-button"
            data-testid="my-songs"
            data-tour="my-songs"
            title="My songs"
            onClick={() => setSongsDrawerOpen(true)}
          >
            <ListMusic size={15} aria-hidden="true" />
            <span className="song-nav-label">My songs</span>
          </button>
          <CreditsHeader
            available={available}
            subscriptionAvailable={billing.subscriptionCredits}
            topupAvailable={billing.topupCredits}
            topupEarliestExpiresAt={billing.topupEarliestExpiresAt}
            topupPacks={billing.topupPacks}
            planFamily={billing.family}
            nextCreditRefreshAt={billing.nextCreditRefreshAt}
            isExpired={isExpired}
            overdrafted={overdrafted}
            loading={creditsLoading || billing.loading}
            error={creditsError || billing.error}
          />
          <div className="status-pill">{status ?? "Ready"}</div>
          <button
            className="btn-primary-inline app-join-button"
            onClick={handleOpenBilling}
          >
            Upgrade
          </button>
          <UserMenu
            activePlanKey={billing.activePlanKey}
            stripeCustomerId={billing.stripeCustomerId}
            onBilling={handleOpenBillingPortal}
            onJoinWaitlist={() => handleJoinWaitlist("studio_menu")}
          />
        </div>
      </header>

      {billingNeedsAttention && (
        <div className="billing-warning-banner" role="alert">
          <span>Payment issue. Manage Billing to avoid service interruption.</span>
          <div className="billing-warning-actions">
            <button type="button" onClick={handleOpenBillingPortal}>
              Manage Billing
            </button>
            <button
              type="button"
              className="billing-warning-close"
              onClick={() => setDismissedBillingWarningStatus(billingWarningStatus)}
              aria-label="Dismiss billing warning"
            >
              <X size={14} />
            </button>
          </div>
        </div>
      )}
      {error && (
        <div className="message-box error" role="alert">
          {error === SYNTHESIS_JOB_DETAILS_TIMEOUT_MESSAGE ? (
            <span className="message-box-content synthesis-timeout-message">
              <strong>{SYNTHESIS_JOB_DETAILS_TIMEOUT_TITLE}</strong>
              <span>{SYNTHESIS_JOB_DETAILS_TIMEOUT_BODY}</span>
            </span>
          ) : (
            <span className="message-box-content">{error}</span>
          )}
          <button
            type="button"
            className="message-box-close"
            onClick={() => setError(null)}
            aria-label="Dismiss message"
          >
            <X size={14} />
          </button>
        </div>
      )}
      {notice && (
        <div className="message-box info" role="status">
          <span className="message-box-content">{notice}</span>
          <button
            type="button"
            className="message-box-close"
            onClick={() => setNotice(null)}
            aria-label="Dismiss message"
          >
            <X size={14} />
          </button>
        </div>
      )}
      <WaitlistModal
        isOpen={showWaitlistModal}
        onClose={() => setShowWaitlistModal(false)}
        source={waitlistSource}
        title={showTrialExpiredModal ? "Trial Expired" : showCreditsModal ? "Credits Exhausted" : undefined}
        subtitle={
          showTrialExpiredModal
            ? "Your free trial has ended. Join the waiting list for paid plans."
            : showCreditsModal
              ? "You're out of credits. Join the waiting list to get notified."
              : undefined
        }
      />
      <BillingPaywallModal
        isOpen={paywallTrigger !== null}
        trigger={paywallTrigger ?? "billing_menu"}
        billing={billing}
        detail={paywallDetail}
        onClose={() => setPaywallTrigger(null)}
        onConfirmed={(message) => {
          setNotice(message);
          setPaywallTrigger(null);
          setPaywallDetail(null);
        }}
      />

      <main
        className={clsx("split-grid", { "chat-collapsed": chatCollapsed })}
        ref={layoutRef}
        style={splitStyle}
      >
        <section
          className={clsx("chat-panel", isDragging && "drag-active", { collapsed: chatCollapsed })}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="chat-header">
            {!chatCollapsed && (
              <>
                <h2>Studio Chat</h2>
                <span className="chat-subtitle">Natural language takes, no DAW edits</span>
              </>
            )}
            <button
              type="button"
              className="panel-collapse-toggle chat-collapse-toggle"
              data-tour="chat-collapse"
              onClick={() => setChatCollapsed((current) => !current)}
              aria-label={chatCollapsed ? "Expand Studio Chat" : "Collapse Studio Chat"}
              title={chatCollapsed ? "Expand Studio Chat" : "Collapse Studio Chat"}
            >
              {chatCollapsed ? (
                <PanelLeftOpen size={17} aria-hidden="true" />
              ) : (
                <PanelLeftClose size={17} aria-hidden="true" />
              )}
            </button>
          </div>
          <div className="chat-stream" ref={chatStreamRef} onScroll={handleChatScroll} data-testid="chat-stream">
            {messages.length === 0 && (
              <div className="empty-state">
                <p>Drop a MusicXML file here to begin.</p>
                <p className="demo-song-links">
                  or try a demo song:{" "}
                  {DEMO_SONGS.map((song, index) => (
                    <span key={song.id}>
                      {index > 0 ? " · " : ""}
                      <button
                        type="button"
                        className="demo-song-link"
                        disabled={uploading || browserMixBounceActive || uploadBlocked}
                        title={uploadBlockedTitle}
                        onClick={() => void loadDemoSong(song)}
                      >
                        {song.title}
                      </button>
                    </span>
                  ))}
                </p>
              </div>
            )}
            {messages.map((msg, index) => (
              <div className="chat-message-group" key={msg.id}>
              <div
                className={clsx(
                  "chat-bubble",
                  msg.role,
                  msg.isProgress && "progress-bubble",
                  msg.audioUrl && "audio-bubble",
                  (msg.singAll || msg.singAllJobId) && "sing-all-inline-bubble",
                  msg.singAllJobId && msg.singAllJobId === highlightedSingAllJob && "sing-all-linked",
                  "reveal"
                )}
                style={{ animationDelay: `${index * 40}ms` }}
                data-sing-all-job={msg.singAllJobId}
              >
                {msg.role === "assistant" ? (
                  (() => {
                    const { mainContent, thoughtSummary, trailingContent } = splitThoughtSummary(
                      msg.content
                    );
                    const isExpanded = Boolean(expandedThoughts[msg.id]);
                    const diagnosticsExpanded = Boolean(expandedDiagnostics[msg.id]);
                    const diagnosticsText = formatDiagnostics(msg.details);
                    const followupAttempts = (msg.attemptMessages ?? []).filter(
                      (attempt) => attempt.attempt_number > 1
                    );
                    return (
                      <>
                        {mainContent ? (
                          <ReactMarkdown className="chat-markdown" remarkPlugins={[remarkGfm]}>
                            {mainContent}
                          </ReactMarkdown>
                        ) : null}
                        {thoughtSummary ? (
                          <div className="thought-summary">
                            <button
                              type="button"
                              className="thought-summary-toggle"
                              onClick={() => toggleThoughtSummary(msg.id)}
                              aria-expanded={isExpanded}
                            >
                              <span
                                className={clsx(
                                  "thought-summary-caret",
                                  isExpanded && "expanded"
                                )}
                                aria-hidden="true"
                              >
                                ▾
                              </span>
                              <span>Thought summary</span>
                            </button>
                            {isExpanded ? (
                              <ReactMarkdown
                                className="chat-markdown thought-summary-content"
                                remarkPlugins={[remarkGfm]}
                              >
                                {thoughtSummary}
                              </ReactMarkdown>
                            ) : null}
                          </div>
                        ) : null}
                        {followupAttempts.map((attempt) => {
                          const attemptKey = `${msg.id}:attempt:${attempt.attempt_number}`;
                          const attemptMessage = attempt.message?.trim() ?? "";
                          return (
                            <div key={attemptKey} className="attempt-block">
                              <div className="attempt-label">Attempt {attempt.attempt_number}</div>
                              {attemptMessage ? (
                                <ReactMarkdown className="chat-markdown" remarkPlugins={[remarkGfm]}>
                                  {attemptMessage}
                                </ReactMarkdown>
                              ) : null}
                            </div>
                          );
                        })}
                        {trailingContent ? (
                          <ReactMarkdown className="chat-markdown" remarkPlugins={[remarkGfm]}>
                            {trailingContent}
                          </ReactMarkdown>
                        ) : null}
                        {diagnosticsText ? (
                          <div className="thought-summary diagnostics-panel">
                            <button
                              type="button"
                              className="thought-summary-toggle"
                              onClick={() => toggleDiagnostics(msg.id)}
                              aria-expanded={diagnosticsExpanded}
                            >
                              <span
                                className={clsx(
                                  "thought-summary-caret",
                                  diagnosticsExpanded && "expanded"
                                )}
                                aria-hidden="true"
                              >
                                ▾
                              </span>
                              <span>Diagnostics</span>
                            </button>
                            {diagnosticsExpanded ? (
                              <pre className="diagnostics-content">{diagnosticsText}</pre>
                            ) : null}
                          </div>
                        ) : null}
                      </>
                    );
                  })()
                ) : (
                  <p>{msg.content}</p>
                )}
                {(msg.singAll || msg.singAllJobId) && sessionId && (
                  <SingAllCard
                    songId={sessionId}
                    songTitle={currentSong?.title ?? "This song"}
                    open={msg.singAll}
                    jobId={msg.singAllJobId}
                    job={msg.singAllJobId ? singAllJobs[msg.singAllJobId] : null}
                    onStarted={(jobId, progress) => handleSingAllStarted(msg.id, jobId, progress)}
                    onLoadJob={loadSingAllJob}
                    onOpenSong={(songId) => navigate(`/app/song/${songId}`)}
                    onTopUp={() => openPaywall("sing_all_topup")}
                    onUpgrade={() => openPaywall("sing_all_upgrade")}
                    billingKey={`${billing.activePlanKey}:${available}`}
                    planKey={billing.activePlanKey}
                    demoSong={activeDemoSongId !== null}
                    onStop={handleSingAllStop}
                    onJobUpdated={handleSingAllJobUpdated}
                    onRetryStarted={handleSingAllRetryStarted}
                    onOpenJob={(jobId) => navigate(`${location.pathname}?job=${encodeURIComponent(jobId)}`)}
                  />
                )}
                {msg.isProgress && !msg.audioUrl && (
                  <div className="thinking-dots" aria-label="Processing">
                    <span />
                    <span />
                    <span />
                  </div>
                )}
                {msg.audioUrl && (
                  <RecoverableAudioPlayer
                    audioUrl={msg.audioUrl}
                    onRefresh={() =>
                      refreshMessageAudioUrl(msg.id, msg.progressUrl, msg.jobId)
                    }
                    onPlayback={() => {
                      logAnalyticsEvent("synthesis_audio_play", synthesisAudioAnalyticsParams(msg));
                      openFeedbackPrompt(msg, "audio_played");
                    }}
                    onDownload={() => {
                      void handleAudioDownload(
                        msg.id,
                        msg.audioUrl,
                        msg.progressUrl,
                        msg.jobId
                      );
                    }}
                    onRecoveryError={setError}
                  />
                )}
              </div>
              {feedbackPrompts[msg.id] && (
                <FeedbackPrototypeBubble
                  minimized={feedbackPrompts[msg.id] === "minimized"}
                  onClose={() =>
                    setFeedbackPrompts((prev) => {
                      const next = { ...prev };
                      delete next[msg.id];
                      return next;
                    })
                  }
                  onReopen={() =>
                    setFeedbackPrompts((prev) => ({ ...prev, [msg.id]: "open" }))
                  }
                  onSubmit={(payload) => {
                    void handleFeedbackSubmit(msg.id, payload);
                  }}
                />
              )}
              </div>
            ))}
            {status && (
              <div className={clsx("chat-bubble", "assistant", "thinking-bubble")}>
                <div className="thinking-dots" aria-label="Processing">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            )}
            {isDragging && (
              <div className="drop-overlay">
                <p>Release to upload your MusicXML file.</p>
              </div>
            )}
          </div>
          <div className="chat-input">
            <div className="starting-conversations" aria-label="Suggested starting conversations">
              <button
                type="button"
                className="starting-conversation-button sing-all-trigger"
                data-testid="sing-all-button"
                data-tour="sing-all"
                disabled={creditsLocked || browserMixBounceActive || chatTurnInProgress || singAllRunningHere}
                title={singAllRunningHere ? "A Sing All is running" : undefined}
                onClick={() => void sendMessage(SING_ALL_PROMPT)}
              >
                <Sparkles size={15} aria-hidden="true" />
                <span>Sing All</span>
              </button>
              {starterPrompts.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  className="starting-conversation-button"
                  disabled={creditsLocked || browserMixBounceActive || singAllRunningHere}
                  title={singAllRunningHere ? "A Sing All is running" : undefined}
                  onClick={() => {
                    setInput(suggestion);
                    composerInputRef.current?.focus();
                  }}
                >
                  <Music2 size={15} aria-hidden="true" />
                  <span>{suggestion}</span>
                </button>
              ))}
            </div>
            {confirmSingAllStop && singAllRunningHere && !singAllStopping && (
              <div className="sing-all-confirm composer-stop-confirm" role="alertdialog" aria-label="Stop Sing All?">
                <p>Stop Sing All? Parts already sung are kept and charged. The rest is refunded.</p>
                <div className="sing-all-actions">
                  <button type="button" className="sing-all-link" onClick={() => setConfirmSingAllStop(false)}>
                    Keep singing
                  </button>
                  <button
                    type="button"
                    className="selection-send"
                    disabled={stopRequestInFlight}
                    onClick={() => {
                      if (activeSingAll) recordSingAllStop(activeSingAll);
                      void handleStopWork();
                    }}
                  >
                    Stop
                  </button>
                </div>
              </div>
            )}
            <div className="input-row composer-row">
              <textarea
                ref={composerInputRef}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="Ask me to sing a specific part or verse..."
                data-testid="chat-input"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (chatTurnInProgress) return;
                    handleSend();
                  }
                }}
                disabled={creditsLocked || browserMixBounceActive}
                rows={2}
              />
              <button
                onClick={handleSend}
                className="send-button"
                disabled={!input.trim() || creditsLocked || chatTurnInProgress || browserMixBounceActive}
                aria-disabled={creditsLocked || chatTurnInProgress || browserMixBounceActive}
                aria-label="Send message"
                data-testid="send-message"
              >
                <Send size={18} />
              </button>
              {((chatTurnInProgress && !jobKindPending) || singAllRunningHere) && (
                <button
                  type="button"
                  className="stop-work-button"
                  onClick={() => {
                    if (singAllRunningHere && !singAllStopping) setConfirmSingAllStop(true);
                    else void handleStopWork();
                  }}
                  disabled={stopRequestInFlight || (singAllStopping && !chatTurnInProgress)}
                  aria-label="Stop"
                  data-testid="stop-work"
                  title={stopRequestInFlight || singAllStopping ? "Stopping…" : "Stop"}
                >
                  {stopRequestInFlight || (singAllStopping && !chatTurnInProgress)
                    ? "Stopping…"
                    : <Square size={17} fill="currentColor" aria-hidden="true" />}
                </button>
              )}
            </div>
            {/* One compact toolbar of icon controls. Each control names itself
                and its current value in an instant tooltip (data-tooltip). */}
            <div className="composer-menu-bar composer-toolbar" role="toolbar" aria-label="Composer settings">
              <div className="score-picker" ref={scorePickerRef}>
                {scoreMenuOpen ? (
                  <div className="voice-picker-menu score-picker-menu" role="menu" aria-label="Add a score">
                    <button type="button" role="menuitem" className="voice-picker-option" disabled={uploadBlocked} title={uploadBlockedTitle} onClick={chooseScoreFile}>
                      <span className="voice-picker-option-avatar recommended" aria-hidden="true">
                        <Upload size={14} />
                      </span>
                      <span className="voice-picker-option-copy">
                        <span className="voice-picker-option-name">Upload MusicXML…</span>
                        <span className="voice-picker-option-meta">.musicxml, .xml or .mxl</span>
                      </span>
                    </button>
                    <div className="score-picker-heading">Demo songs</div>
                    {DEMO_SONGS.map((song) => (
                      <button
                        key={song.id}
                        type="button"
                        role="menuitem"
                        className="voice-picker-option"
                        disabled={uploadBlocked}
                        title={uploadBlockedTitle}
                        onClick={() => void loadDemoSong(song)}
                      >
                        <span className="voice-picker-option-avatar recommended" aria-hidden="true">
                          <Music2 size={14} />
                        </span>
                        <span className="voice-picker-option-copy">
                          <span className="voice-picker-option-name">{song.title}</span>
                          <span className="voice-picker-option-meta">{song.detail}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                ) : null}
                <button
                  type="button"
                  className={clsx("composer-tool", { open: scoreMenuOpen })}
                  aria-haspopup="menu"
                  aria-expanded={scoreMenuOpen}
                  aria-label={uploading ? "Uploading score" : "Add a score"}
                  data-tooltip={uploadBlockedTitle ?? (uploading ? "Uploading score…" : "Upload a score or load a demo song")}
                  title={uploadBlockedTitle}
                  disabled={uploading || browserMixBounceActive || uploadBlocked}
                  onClick={() => {
                    setScoreMenuOpen((open) => !open);
                    setVoiceMenuOpen(false);
                    setSolfegeMenuOpen(false);
                  }}
                >
                  <Plus size={16} aria-hidden="true" />
                </button>
                <input
                  ref={scoreFileInputRef}
                  type="file"
                  hidden
                  data-testid="score-upload-input"
                  accept=".musicxml,.xml,.mxl"
                  disabled={uploading || creditsLocked || browserMixBounceActive || uploadBlocked}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    // Cleared so that choosing the same file again is still a change.
                    event.target.value = "";
                    if (file) requestUpload(file);
                  }}
                />
              </div>
              <span className="composer-toolbar-divider" aria-hidden="true" />
              <div className="voice-picker" ref={voicePickerRef}>
                {voiceMenuOpen ? (
                  <div className="voice-picker-menu" role="listbox" aria-label="Select AI voice">
                    <button
                      type="button"
                      className={clsx("voice-picker-option", { selected: selectedVoicebankId === null })}
                      onClick={() => {
                        setSelectedVoicebankId(null);
                        setVoiceMenuOpen(false);
                      }}
                    >
                      <span className="voice-picker-option-avatar recommended" aria-hidden="true">
                        <Sparkles size={15} />
                      </span>
                      <span className="voice-picker-option-copy">
                        <span className="voice-picker-option-name">Auto</span>
                        <span className="voice-picker-option-meta">Let the model choose</span>
                      </span>
                      {selectedVoicebankId === null ? <Check size={14} aria-hidden="true" /> : null}
                    </button>
                    {voicebanks.map((voice) => {
                      const isSelected = voice.id === selectedVoicebankId;
                      return (
                        <button
                          key={voice.id}
                          type="button"
                          className={clsx("voice-picker-option", { selected: isSelected })}
                          onClick={() => {
                            setSelectedVoicebankId(voice.id);
                            setVoiceMenuOpen(false);
                          }}
                        >
                          {renderVoiceAvatar(voice, "voice-picker-option-avatar")}
                          <span className="voice-picker-option-copy">
                            <span className="voice-picker-option-name">{voice.name}</span>
                          </span>
                          {isSelected ? <Check size={14} aria-hidden="true" /> : null}
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <button
                  type="button"
                  className={clsx("composer-tool", "composer-voice-tool", { open: voiceMenuOpen })}
                  aria-haspopup="listbox"
                  aria-expanded={voiceMenuOpen}
                  aria-label={`Voice: ${voicebanksLoading ? "Loading voices" : selectedVoiceLabel}`}
                  data-tooltip={`Voice: ${voicebanksLoading ? "Loading voices…" : selectedVoiceLabel}`}
                  onClick={() => {
                    setVoiceMenuOpen((open) => !open);
                    setSolfegeMenuOpen(false);
                    setScoreMenuOpen(false);
                  }}
                >
                  <Mic size={15} aria-hidden="true" />
                  <span className="composer-voice-name">
                    {voicebanksLoading ? "Loading…" : selectedVoiceLabel}
                  </span>
                </button>
              </div>
              <div className="solfege-picker" ref={solfegePickerRef}>
                {solfegeMenuOpen ? (
                  <div
                    className="solfege-picker-menu"
                    role="dialog"
                    aria-label="Solfege settings"
                  >
                    <fieldset
                      className="solfege-setting-group"
                      disabled={solfegeSettingsSaving}
                    >
                      <legend>System</legend>
                      <div className="solfege-segmented-control">
                        {([
                          ["movable_do", "Movable Do"],
                          ["fixed_do", "Fixed Do"],
                        ] as const).map(([value, label]) => (
                          <button
                            key={value}
                            type="button"
                            className={clsx({ selected: draftSolfegeSystem === value })}
                            aria-pressed={draftSolfegeSystem === value}
                            onClick={() => setDraftSolfegeSystem(value)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    </fieldset>
                    <fieldset
                      className="solfege-setting-group"
                      disabled={draftSolfegeSystem === "fixed_do" || solfegeSettingsSaving}
                    >
                      <legend>Mode</legend>
                      <div className="solfege-mode-options">
                        {([
                          ["major", "Major"],
                          ["minor_la_based", "Minor - La based"],
                          ["minor_do_based", "Minor - Do based"],
                        ] as const).map(([value, label]) => (
                          <label
                            key={value}
                            className={clsx("solfege-mode-option", {
                              selected: draftSolfegeMode === value,
                            })}
                          >
                            <input
                              type="radio"
                              name="solfege-mode"
                              value={value}
                              checked={draftSolfegeMode === value}
                              onChange={() => setDraftSolfegeMode(value)}
                            />
                            <span className="solfege-radio-indicator" aria-hidden="true" />
                            <span>{label}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <div className="solfege-menu-actions">
                      <button
                        type="button"
                      className="solfege-cancel-button"
                        disabled={solfegeSettingsSaving}
                        aria-label="Cancel solfege settings changes"
                        title="Cancel changes"
                        onClick={() => {
                          setDraftSolfegeSystem(solfegeSystem);
                          setDraftSolfegeMode(solfegeMode);
                          setSolfegeMenuOpen(false);
                        }}
                      >
                        <X size={17} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="solfege-confirm-button"
                        disabled={solfegeSettingsSaving}
                        aria-label="Apply solfege settings"
                        title="Apply solfege settings"
                        onClick={() => void handleConfirmSolfegeSettings()}
                      >
                        <Check size={17} aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                ) : null}
                <button
                  type="button"
                  className={clsx("composer-tool", { open: solfegeMenuOpen })}
                  aria-haspopup="dialog"
                  aria-expanded={solfegeMenuOpen}
                  aria-label={`Solfege: ${solfegeTriggerValue}`}
                  data-tour="solfege"
                  data-tooltip={`Solfege: ${solfegeTriggerValue}`}
                  disabled={solfegeSettingsSaving}
                  onClick={() => {
                    if (!solfegeMenuOpen) {
                      setDraftSolfegeSystem(solfegeSystem);
                      setDraftSolfegeMode(solfegeMode);
                    }
                    setSolfegeMenuOpen((open) => !open);
                    setVoiceMenuOpen(false);
                    setScoreMenuOpen(false);
                  }}
                >
                  <Music2 size={16} aria-hidden="true" />
                </button>
              </div>
              {/* A render setting, like the voice and solfege: it applies to the
                  next take. The player keeps playing the takes it has. */}
              <button
                type="button"
                role="switch"
                aria-checked={expandRepeats}
                aria-label="With Repeats"
                data-tour="repeats"
                className={clsx("composer-tool composer-repeat-toggle", { active: expandRepeats })}
                data-tooltip={
                  repeatsUnavailable
                    ? REPEATS_UNAVAILABLE_MESSAGE
                    : expandRepeats
                      ? "Repeats: on. The next take sings the repeats"
                      : "Repeats: off. The next take sings in written order"
                }
                disabled={!score || browserMixBounceActive || repeatsUnavailable}
                onClick={() => setExpandRepeats((current) => !current)}
              >
                <Repeat size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
        </section>

        <div
          className="split-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panels"
          onPointerDown={handleResizeStart}
        />

        <div className="score-column">
          <ScorePlayerEngine
            key={`score-player-${scorePlayerPlaybackRequestId}`}
            midiUrl={instrumentalMidiUrl}
            vocalTracks={shownVocalTracks}
            decodedVocals={decodedVocals}
            vocalsDecoding={vocalsDecoding}
            instrumentalTracks={instrumentalTracks}
            instrumentalMidiTrackKeys={instrumentalMidiTrackKeys}
            instrumentalBus={instrumentalBus}
            playbackRequestId={scorePlayerPlaybackRequestId}
            onControlsChange={handleScorePlayerControlsChange}
            onEngineLoading={handleScorePlayerEngineLoading}
            onEngineReady={handleScorePlayerEngineReady}
            onPlaybackStateChange={handleScorePlayerPlaybackStateChange}
            onPlaybackPositionChange={handleScorePlayerPlaybackPositionChange}
            onTimelineDurationChange={handleScorePlayerTimelineDurationChange}
            onAdapterChange={handleScorePlayerAdapterChange}
            onError={handleScorePlayerEngineError}
            seekBarHost={scorePlayerSeekBarHost}
            seekDisabled={browserMixBounceActive || !scorePlayerAssetsReady}
          />

          <section
            className={clsx("score-panel", isDragging && "drag-active")}
            aria-label="Score preview"
            data-testid="score-preview"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
          <div className="score-header">
              <div className="score-controls">
                <div className="score-subtitles">
                  <span className="chat-subtitle">
                    Latest upload only {audioUrl ? "· Audio ready" : ""}
                </span>
                {estimatedDurationLabel && (
                  <span className="score-estimate" title={estimatedDurationTitle}>
                    {estimatedDurationLabel}
                  </span>
                )}
                {estimatedVocalCostLabel && (
                  <span
                    className={
                      estimateCalculating
                        ? "score-estimate score-estimate-pending"
                        : "score-estimate"
                    }
                    title={estimatedVocalCostTitle}
                  >
                    {estimatedVocalCostLabel}
                  </span>
                )}
                {estimatedInstrumentalCostLabel && (
                  <span
                    className={
                      estimateCalculating || instrumentalsAlreadyGenerated
                        ? "score-estimate score-estimate-pending"
                        : "score-estimate"
                    }
                    title={estimatedInstrumentalCostTitle}
                  >
                    {estimatedInstrumentalCostLabel}
                  </span>
                )}
              </div>
              <div className="score-zoom-group">
                {songZipJobId && <SingAllDownloadButton jobId={songZipJobId} compact />}
                <div className="zoom-controls">
                  <button
                    type="button"
                    className="zoom-button"
                    onClick={() => handleZoom(-0.1)}
                    aria-label="Zoom out"
                    disabled={!score || Boolean(scorePreviewError)}
                  >
                    <Minus size={16} />
                  </button>
                  <span className="zoom-value">{Math.round(zoomLevel * 100)}%</span>
                  <button
                    type="button"
                    className="zoom-button"
                    onClick={() => handleZoom(0.1)}
                    aria-label="Zoom in"
                    disabled={!score || Boolean(scorePreviewError)}
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
              <div className="score-player-transport" aria-label="Score player transport">
                <button
                  type="button"
                  className={clsx("score-action-button", {
                    "browser-mix-bounce-active": browserMixBounceActive,
                  })}
                  onPointerDown={() => {
                    if (!browserMixBounceActive) primeScorePlayerAudioContext();
                  }}
                  onKeyDown={(event) => {
                    if (!browserMixBounceActive && (event.key === "Enter" || event.key === " ")) {
                      primeScorePlayerAudioContext();
                    }
                  }}
                  onClick={browserMixBounceActive ? cancelBrowserMixBounce : () => void handleBrowserMixBounce()}
                  disabled={
                    !browserMixBounceActive &&
                    (!hasScorePlayerTracks ||
                      !scorePlayerControls ||
                      !scorePlayerAdapterReady ||
                      !scorePlayerAssetsReady)
                  }
                  aria-label={browserMixBounceActive ? "Cancel real-time mix download" : "Download mix in real time"}
                  data-tour="export-mix"
                  title={
                    browserMixBounceActive
                      ? "Cancel real-time mix download"
                      : "Download mix (real-time)"
                  }
                >
                  {browserMixBounceActive ? <X size={16} /> : <Upload size={16} />}
                </button>
                {BILLABLE_SERVER_EXPORT_MIX_ENABLED && (
                <button
                  type="button"
                  className={clsx("score-action-button", "multitrack-export-button", {
                    exporting: multiTrackExportProgress !== null,
                    "with-credit": !noOpSingleTrackExport,
                  })}
                  onClick={handleMultiTrackExport}
                  disabled={
                    !shownVocalTracks.length ||
                    (!noOpSingleTrackExport &&
                      (creditsLocked || exportMixRequiredCredits === null)) ||
                    multiTrackExportProgress !== null ||
                    browserMixBounceActive
                  }
                  aria-label={
                    noOpSingleTrackExport
                      ? "Download track"
                      : `Export mix, ${exportMixRequiredCredits ?? "unknown"} ${exportMixRequiredCredits === 1 ? "credit" : "credits"}`
                  }
                  title={noOpSingleTrackExport ? "Download track" : "Export mix"}
                >
                  {multiTrackExportPercent ?? (
                    noOpSingleTrackExport ? (
                      <Download size={16} />
                    ) : (
                      <>
                        <Upload size={16} />
                        <span className="multitrack-export-divider" aria-hidden="true" />
                        <span className="multitrack-export-credit-label">
                          {exportMixRequiredCredits ?? "--"}{" "}
                          {exportMixRequiredCredits === 1 ? "credit" : "credits"}
                        </span>
                      </>
                    )
                  )}
                </button>
                )}
                <button
                  type="button"
                  className="score-action-button"
                  onClick={multiTrackPlaying ? handleMultiTrackPause : handleMultiTrackPlay}
                  disabled={!hasScorePlayerTracks || !scorePlayerControls || browserMixBounceActive}
                  aria-label={multiTrackPlaying ? "Pause score player" : "Play score player"}
                  title={multiTrackPlaying ? "Pause" : "Play"}
                >
                  {multiTrackPlaying ? <Pause size={16} /> : <Play size={16} />}
                </button>
                <button
                  type="button"
                  className="score-action-button"
                  onClick={handleMultiTrackStop}
                  disabled={!hasScorePlayerTracks || !scorePlayerControls || browserMixBounceActive}
                  aria-label="Stop score player"
                  title="Stop"
                >
                  <Square size={15} />
                </button>
              </div>
              <div className="score-action-controls" aria-label="Score export controls">
                <div className="score-layout-toggle" role="group" aria-label="Score preview layout" data-tour="score-layout">
                  <button
                    type="button"
                    className={clsx("score-layout-option", { selected: scorePreviewLayout === "page" })}
                    aria-pressed={scorePreviewLayout === "page"}
                    aria-label="Page view"
                    title="Page view"
                    disabled={!score}
                    onClick={() => setScorePreviewLayout("page")}
                  >
                    <PageViewIcon />
                  </button>
                  <button
                    type="button"
                    className={clsx("score-layout-option", { selected: scorePreviewLayout === "horizontal" })}
                    aria-pressed={scorePreviewLayout === "horizontal"}
                    aria-label="Horizontal view"
                    title="Horizontal view"
                    disabled={!score}
                    onClick={() => setScorePreviewLayout("horizontal")}
                  >
                    <HorizontalViewIcon />
                  </button>
                </div>
                <button
                  type="button"
                  className="score-action-button"
                  data-testid="share-song"
                  data-tour="share-song"
                  disabled={shareBlocked !== null}
                  onClick={() => {
                    if (!currentSong) return;
                    shareDialogOpen("song_bar");
                    setShareBoxSong({
                      id: currentSong.id,
                      title: currentSong.title,
                      url: currentSong.share?.enabled ? currentSong.share.url : null,
                    });
                  }}
                  aria-label="Share song"
                  title={shareBlocked ?? "Share song"}
                >
                  <Share2 size={16} />
                </button>
                <button
                  type="button"
                  className="score-action-button"
                  onClick={handleScoreDownload}
                  aria-label="Download score"
                  title="Download score"
                  disabled={!score}
                >
                  <Download size={16} />
                </button>
                <button
                  type="button"
                  className="score-action-button"
                  onClick={handleScorePrint}
                  aria-label="Print score"
                  title="Print score"
                  disabled={!scoreReady || Boolean(scorePreviewError)}
                >
                  <Printer size={16} />
                </button>
              </div>
              <div ref={setScorePlayerSeekBarHost} className="score-player-seek-host" />
            </div>
          </div>
          {scorePlayerError && (
            <div className="score-player-error" role="alert">
              {scorePlayerError}
            </div>
          )}
          {multiTrackExportError && (
            <div className="score-player-error" role="alert">
              {multiTrackExportError}
            </div>
          )}
          {(browserMixBounceActive || browserMixBounceNotice || browserMixBounceError) && (
            <div
              className={clsx("score-player-bounce-status", {
                error: Boolean(browserMixBounceError),
              })}
              role={browserMixBounceError ? "alert" : "status"}
            >
              {browserMixBounceError ??
                (browserMixBouncePhase === "preparing"
                  ? browserMixBouncePreparation ?? "Preparing real-time mix…"
                  : browserMixBouncePhase === "finalizing"
                    ? "Finalizing mix…"
                    : browserMixBounceActive
                      ? "Bouncing mix… keep this tab open."
                      : browserMixBounceNotice)}
            </div>
          )}
          <div className="score-body">
            {hasScorePlayerTracks && (
              <aside
                className={clsx("score-tracks-sidebar", { collapsed: scoreTracksCollapsed })}
                aria-label="Score multitrack controls sidebar"
              >
                {scoreTracksCollapsed ? (
                  <button
                    type="button"
                    className="panel-collapse-toggle score-tracks-sidebar-expand"
                    onClick={() => setScoreTracksCollapsed(false)}
                    aria-label="Expand Tracks & Stems"
                    title="Expand Tracks & Stems"
                  >
                    <PanelLeftOpen size={17} aria-hidden="true" />
                  </button>
                ) : (
                  <ScoreTrackControlsPanel
                    vocalTracks={shownVocalTracks}
                    instrumentalTracks={instrumentalTracks}
                    instrumentalBus={instrumentalBus}
                    instrumentalTracksExpanded={instrumentalTracksExpanded}
                    disabled={browserMixBounceActive}
                    vocalModeShown={vocalsInBothModes ? vocalModeShown : null}
                    onShowVocalMode={(mode) => {
                      handleMultiTrackStop();
                      setVocalModeShown(mode);
                    }}
                    onUpdateVocalMute={updateMultiTrackMute}
                    onUpdateVocalSolo={updateMultiTrackSolo}
                    onUpdateVocalVolume={updateMultiTrackVolume}
                    onDownloadVocalTrack={handleMultiTrackTrackDownload}
                    onUpdateInstrumentalMute={updateInstrumentalTrackMute}
                    onUpdateInstrumentalSolo={updateInstrumentalTrackSolo}
                    onUpdateInstrumentalVolume={updateInstrumentalTrackVolume}
                    onUpdateInstrumentalGmProgram={updateInstrumentalTrackGmProgram}
                    onUpdateInstrumentalBusMute={updateInstrumentalBusMute}
                    onUpdateInstrumentalBusSolo={updateInstrumentalBusSolo}
                    onUpdateInstrumentalBusLevel={updateInstrumentalBusLevel}
                    onToggleInstrumentalTracksExpanded={() =>
                      setInstrumentalTracksExpanded((current) => !current)
                    }
                    onCollapse={() => setScoreTracksCollapsed(true)}
                  />
                )}
              </aside>
            )}
            <div
              ref={scoreCanvasRef}
              className={clsx("score-canvas", { "horizontal-layout": scorePreviewLayout === "horizontal" })}
            >
              {scorePreviewLayout === "horizontal" && score ? (
                <HorizontalScoreRenderer
                  scoreData={score.data}
                  zoomLevel={zoomLevel}
                  measuresPerBatch={horizontalScoreMeasuresPerBatch}
                  scrollContainerRef={scoreCanvasRef}
                  onRendererChange={handleHorizontalScoreRendererChange}
                  onReady={handleHorizontalScoreReady}
                  onRenderError={handleScorePreviewFailure}
                />
              ) : (
                <div
                  ref={scoreRef}
                  className="score-surface page-layout"
                  data-testid="score-preview-surface"
                />
              )}
              {scorePreviewError ? (
                <div className="score-placeholder score-error-placeholder">
                  <p>{scorePreviewError}</p>
                </div>
              ) : !score ? (
                <div className="score-placeholder">
                  <div className="score-placeholder-content">
                    <p>Upload a MusicXML file to render the score here.</p>
                    <p className="demo-song-offer-label">No score handy? Try a demo song:</p>
                    <div className="demo-song-cards">
                      {DEMO_SONGS.map((song) => (
                        <button
                          key={song.id}
                          type="button"
                          className="demo-song-card"
                          disabled={uploading || browserMixBounceActive || uploadBlocked}
                          title={uploadBlockedTitle}
                          onClick={() => void loadDemoSong(song)}
                        >
                          <Music2 size={18} aria-hidden="true" />
                          <span className="demo-song-card-title">{song.title}</span>
                          <span className="demo-song-card-detail">{song.detail}</span>
                          <span className="demo-song-card-action">Load demo</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null}
              {uploading ? (
                <div className="score-loading-overlay" role="status" aria-live="polite">
                  <span className="score-loading-spinner" aria-hidden="true" />
                  <span>Uploading and parsing score…</span>
                </div>
              ) : null}
            </div>
          </div>
          {isDragging && (
            <div className="drop-overlay">
              <p>Release to upload your MusicXML file.</p>
            </div>
          )}
        </section>
        </div>
      </main>
      {songLoadState !== "idle" ? (
        <div className="song-load-overlay" role={songLoadState === "loading" ? "status" : "alert"}>
          {songLoadState === "loading" ? (
            <>
              <span className="score-loading-spinner" aria-hidden="true" />
              <span>Opening your song…</span>
            </>
          ) : (
            <>
              <p className="song-load-message">
                {songLoadState === "unavailable"
                  ? "This song isn't available"
                  : "This song couldn't be opened. Please try again."}
              </p>
              <div className="song-load-actions">
                {songLoadState === "failed" && routeSongId ? (
                  <button
                    type="button"
                    className="btn-secondary-inline"
                    onClick={() => void openSong(routeSongId)}
                  >
                    Try again
                  </button>
                ) : null}
                <Link className="btn-secondary-inline" to="/app">
                  Start a new song
                </Link>
              </div>
            </>
          )}
        </div>
      ) : null}
      <SongsDrawer
        open={songsDrawerOpen}
        onClose={() => setSongsDrawerOpen(false)}
        currentSongId={currentSong?.id ?? null}
        canOpen={canLeaveSong}
        onOpen={(songId) => {
          setSongsDrawerOpen(false);
          navigate(`/app/song/${songId}`);
        }}
        onDeleted={(songId) => {
          if (songId !== shownSongIdRef.current) return;
          setSongsDrawerOpen(false);
          navigate("/app");
        }}
        shareAvailable={shareAvailable}
        onShareChange={handleShareChange}
      />
      {shareBoxSong ? (
        <ShareSongModal
          songId={shareBoxSong.id}
          title={shareBoxSong.title}
          url={shareBoxSong.url}
          initialInfo={shareBoxSong.id === currentSong?.id ? currentSong.info : undefined}
          onClose={() => setShareBoxSong(null)}
          onShareChange={handleShareChange}
          onEditInfo={
            currentSong?.info && shareBoxSong.id === currentSong.id
              ? () => {
                  setShareBoxSong(null);
                  songInfoOpened("share_dialog");
                  setSongInfoOpen(true);
                }
              : undefined
          }
        />
      ) : null}
      {songInfoOpen && currentSong?.info ? (
        <SongInfoPanel
          songId={currentSong.id}
          info={currentSong.info}
          onClose={() => setSongInfoOpen(false)}
          onSaved={(row) =>
            setCurrentSong((current) =>
              current && current.id === row.id
                ? {
                    ...current,
                    title: row.title,
                    title_edited: current.title_edited || row.title !== current.title,
                    info: row.info ?? current.info,
                  }
                : current
            )
          }
        />
      ) : null}
      {pendingUpload && !pendingUpload.waitForWorkToEnd ? (
        <NewSongOrReplaceModal
          scoreName={pendingUpload.file.name}
          working={chatTurnInProgress || singAllRunningHere}
          singAllRunning={singAllRunningHere}
          onNewSong={() => void startNewSongWith(pendingUpload.file, pendingUpload.demoSongId)}
          onReplace={() => replaceScoreWith(pendingUpload.file, pendingUpload.demoSongId)}
          onCancel={() => setPendingUpload(null)}
        />
      ) : null}
      {showAnnouncement && currentAnnouncement ? (
        <AnnouncementModal
          announcement={currentAnnouncement}
          onClose={() => markAsSeen(currentAnnouncement.id)}
        />
      ) : null}
      <FeatureTour paused={announcementsLoading || showAnnouncement} />
    </div>
  );
}

function hasBillingPaymentIssue(billing: ReturnType<typeof useBillingState>): boolean {
  if (["past_due", "unpaid", "paused"].includes(billing.stripeSubscriptionStatus || "")) {
    return true;
  }
  if (billing.latestPaymentFailureCode || billing.latestPaymentFailureMessage) {
    return true;
  }
  return billing.latestInvoiceStatus === "open" && billing.latestPaymentIntentStatus === "requires_payment_method";
}

function formatCredits(credits: number): string {
  return `${credits} ${credits === 1 ? "credit" : "credits"}`;
}

function formatDuration(totalSeconds: number): string {
  const rounded = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const seconds = rounded % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  if (minutes > 0) {
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }
  return `${seconds}s`;
}

// Progress payloads are re-created by each stream event or poll, so
// referential equality alone would make an unchanged payload look new. Keep the
// existing message object when its user-visible and expandable fields are the
// same; otherwise every update makes the chat auto-scroll effect read
// scrollHeight and synchronously lay out the score preview.
function isTerminalProgressStatus(status: ProgressResponse["status"]): boolean {
  return (
    status === "done" ||
    status === "stopped" ||
    status === "error" ||
    status === "action_required"
  );
}

// What one progress payload does to its chat bubble. A live poll result and a
// job restored with a reopened Song both go through here, so they show the same.
// Returns the same message when nothing changed.
function applyProgressToMessage(
  message: Message,
  payload: ProgressResponse,
  refundedCredits?: number
): Message {
  const nextMessage = payload.message;
  const nextProgress = payload.progress;
  const nextAudioUrl = payload.audio_url;
  const appendTerminalPreprocessMessage =
    payload.job_kind === "preprocess" &&
    (payload.status === "done" || payload.status === "error");
  const appendProgressToChatBubble =
    payload.status !== "error" || payload.job_kind === "preprocess";
  const stoppedMessage = payload.status === "stopped"
    ? `${nextMessage || "Stopped."}${refundedCredits ? ` Refunded ${refundedCredits} ${refundedCredits === 1 ? "credit" : "credits"}.` : ""}`
    : nextMessage;
  const nextAttemptMessages = extractAttemptMessages(payload.details);
  const nextContent =
    payload.job_kind === "preprocess" && payload.status === "running"
      ? message.content
      : appendTerminalPreprocessMessage
        ? appendPreprocessTerminalMessage(message.content, nextMessage)
        : appendProgressToChatBubble
          ? appendProgressMessage(message.content, stoppedMessage)
          : message.content;
  const nextMessageState: Message = {
    ...message,
    content: nextContent,
    details: payload.details ?? message.details,
    attemptMessages: nextAttemptMessages ?? message.attemptMessages,
    progressValue: typeof nextProgress === "number" ? nextProgress : message.progressValue,
    // Each progress payload carries a newly signed URL. Keep the first
    // one rather than rebuilding the player and re-fetching audio on every update.
    audioUrl: message.audioUrl || (payload.status !== "stopped" ? nextAudioUrl : undefined),
    audioTrack: payload.audio_track ?? message.audioTrack,
    jobId: payload.job_id ?? message.jobId,
    feedback: payload.feedback ?? message.feedback,
    isProgress: !isTerminalProgressStatus(payload.status),
  };
  const changed =
    nextMessageState.content !== message.content ||
    nextMessageState.progressValue !== message.progressValue ||
    nextMessageState.audioUrl !== message.audioUrl ||
    nextMessageState.jobId !== message.jobId ||
    nextMessageState.isProgress !== message.isProgress ||
    !sameProgressValue(nextMessageState.details, message.details) ||
    !sameProgressValue(nextMessageState.attemptMessages, message.attemptMessages) ||
    !sameProgressValue(nextMessageState.audioTrack, message.audioTrack) ||
    !sameProgressValue(nextMessageState.feedback, message.feedback);
  return changed ? nextMessageState : message;
}

function sameProgressValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function appendProgressMessage(current: string, incoming?: string | null): string {
  if (!incoming) return current;
  const trimmedIncoming = incoming.trim();
  if (!trimmedIncoming) return current;
  if (!current) return trimmedIncoming;
  const trimmedCurrent = current.trimEnd();
  const lastLine = trimmedCurrent.split("\n").pop() ?? "";
  if (lastLine.trim() === trimmedIncoming) {
    return current;
  }
  return `${trimmedCurrent}\n${trimmedIncoming}`;
}

function nextStreamEventWithTimeout<T>(
  iterator: AsyncIterator<T>,
  timeoutMs: number
): Promise<IteratorResult<T>> {
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(
      () => reject(new Error("Synthesis stream stopped responding.")),
      timeoutMs
    );
    iterator.next().then(
      (result) => {
        window.clearTimeout(timeoutId);
        resolve(result);
      },
      (error) => {
        window.clearTimeout(timeoutId);
        reject(error);
      }
    );
  });
}

function splitThoughtSummary(content: string): {
  mainContent: string;
  thoughtSummary: string;
  trailingContent: string;
} {
  const thoughtMarker = "\n\nThought summary:\n";
  const trailingMarker = "\n\nPost-update:\n";
  const thoughtPrefix = "Thought summary:\n";
  const trailingPrefix = "Post-update:\n";
  let main = content;
  let thought = "";
  let trailing = "";

  if (main.includes(trailingMarker)) {
    const parts = main.split(trailingMarker);
    trailing = parts.pop() ?? "";
    main = parts.join(trailingMarker).trim();
  } else if (main.startsWith(trailingPrefix)) {
    trailing = main.slice(trailingPrefix.length);
    main = "";
  }

  if (main.includes(thoughtMarker)) {
    const parts = main.split(thoughtMarker);
    thought = parts.pop() ?? "";
    main = parts.join(thoughtMarker).trim();
  } else if (main.startsWith(thoughtPrefix)) {
    thought = main.slice(thoughtPrefix.length);
    main = "";
  }

  return {
    mainContent: main.trim(),
    thoughtSummary: thought.trim(),
    trailingContent: trailing.trim(),
  };
}

function appendPreprocessTerminalMessage(current: string, incoming?: string | null): string {
  const trimmedIncoming = incoming?.trim();
  if (!trimmedIncoming) return current;
  const { mainContent, thoughtSummary, trailingContent } = splitThoughtSummary(current);
  if (
    mainContent.includes(trimmedIncoming) ||
    trailingContent.includes(trimmedIncoming)
  ) {
    return current;
  }
  const nextTrailingContent = trailingContent
    ? `${trailingContent.trimEnd()}\n\n${trimmedIncoming}`
    : trimmedIncoming;
  let result = mainContent;
  if (thoughtSummary) {
    result = result
      ? `${result}\n\nThought summary:\n${thoughtSummary}`
      : `Thought summary:\n${thoughtSummary}`;
  }
  return result
    ? `${result}\n\nPost-update:\n${nextTrailingContent}`
    : `Post-update:\n${nextTrailingContent}`;
}

function formatDiagnostics(details: unknown): string {
  if (details === null || details === undefined) return "";
  try {
    if (typeof details === "object") {
      const rawAttempts = (details as { attempt_messages?: unknown }).attempt_messages;
      if (Array.isArray(rawAttempts)) {
        const preprocessAttempts = rawAttempts
          .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === "object")
          .map((entry) => {
            const diagnostics = entry.diagnostics;
            if (!diagnostics || typeof diagnostics !== "object") return null;
            const diagnostic = diagnostics as {
              planner_thinking?: unknown;
              submitted_plan?: unknown;
              execution_plan?: unknown;
            };
            return {
              attempt_number: entry.attempt_number,
              model_thinking: diagnostic.planner_thinking,
              submitted_plan: diagnostic.submitted_plan,
              execution_plan: diagnostic.execution_plan,
            };
          })
          .filter((entry) => entry !== null);
        if (preprocessAttempts.length > 0) {
          return JSON.stringify({ preprocess_attempts: preprocessAttempts }, null, 2);
        }
      }
    }
    return JSON.stringify(details, null, 2);
  } catch {
    return String(details);
  }
}

function extractAttemptMessages(details: unknown): AttemptMessage[] | undefined {
  if (!details || typeof details !== "object") return undefined;
  const raw = (details as { attempt_messages?: unknown }).attempt_messages;
  if (!Array.isArray(raw)) return undefined;
  const entries = raw
    .map<AttemptMessage | null>((entry) => {
      if (!entry || typeof entry !== "object") return null;
      const attemptNumber = Number((entry as { attempt_number?: unknown }).attempt_number);
      if (!Number.isFinite(attemptNumber)) return null;
      const message = (entry as { message?: unknown }).message;
      const thought = (entry as { thought_summary?: unknown }).thought_summary;
      return {
        attempt_number: attemptNumber,
        message: typeof message === "string" ? message : undefined,
        thought_summary: typeof thought === "string" ? thought : undefined,
      };
    })
    .filter((entry): entry is AttemptMessage => entry !== null);
  return entries.length > 0 ? entries : undefined;
}
