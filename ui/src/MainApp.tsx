import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import WaveSurfer from "wavesurfer.js";
import { SoundFontCache } from "@waveform-playlist/playout";
import {
  WaveformPlaylistProvider,
  usePlaybackAnimation,
  usePlaylistControls,
  usePlaylistData,
} from "@waveform-playlist/browser";
import { useAudioTracks } from "@waveform-playlist/browser/tone";
import { useMidiTracks } from "@waveform-playlist/midi";
import { UploadCloud, Upload, Send, Sparkles, Minus, Plus, Download, Printer, ChevronsUpDown, Check, X, Music2, Play, Pause, Square, Mic, Volume2, VolumeX, GripVertical, Sliders } from "lucide-react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import {
  chat,
  createSession,
  exportMix,
  fetchInstrumentalMidi,
  fetchScoreXml,
  fetchProgress,
  fetchSolfegeSettings,
  fetchVoicebanks,
  markFeedbackPrompted,
  submitAudioFeedback,
  uploadScore,
  updateSolfegeSettings,
  type AudioTrackMetadata,
  type ChatSelection,
  type FeedbackPromptState,
  type FeedbackRatingsRequest,
  type InstrumentalPart,
  type PerformanceMidi,
  type ProgressResponse,
  type ScoreSummary,
  type SolfegeMode,
  type SolfegeSystem,
  type VoicebankOption,
} from "./api";
import CreditsHeader from "./components/CreditsHeader";
import { UserMenu } from "./components/UserMenu";
import { logAnalyticsEvent } from "./firebase";
import { useCredits } from "./hooks/useCredits";
import { useBillingState } from "./hooks/useBillingState";
import { useAuth } from "./hooks/useAuth";
import { useAnnouncements } from "./hooks/useAnnouncements";
import { WaitlistModal } from "./components/WaitlistModal";
import AnnouncementModal from "./components/AnnouncementModal";
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

const STARTING_CONVERSATIONS = [
  "sing the vocal part, verse 1",
  "sing the soprano part",
  "sing the alto part in solfege / solfa",
] as const;

const SOLFEGE_GUIDE_DISMISSED_KEY = "sightsinger.solfege-guide-dismissed";
const MULTITRACK_TUTORIAL_DISMISSED_KEY = "sightsinger.multitrack-tutorial-dismissed";
const MULTITRACK_TUTORIAL_STEPS = [
  {
    target: "player",
    message: "Generated audio will be added to this multitrack player as separate synchronized tracks.",
  },
  {
    target: "play",
    message: "Use Play to hear all generated tracks together.",
  },
  {
    target: "export",
    message: "Use Export to bounce the mix. Export consumes credits at 1 credit per minute.",
  },
] as const;

type MultitrackTutorialTarget = (typeof MULTITRACK_TUTORIAL_STEPS)[number]["target"];

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
  audioTrack?: AudioTrackMetadata;
  details?: unknown;
  attemptMessages?: AttemptMessage[];
  showSelector?: boolean;
  progressUrl?: string;
  isProgress?: boolean;
  progressValue?: number;
  jobId?: string;
  feedback?: FeedbackPromptState;
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

type ScorePreviewLayout = "page" | "horizontal";

type MultiTrackAudioTrack = {
  key: string;
  label: string;
  audioUrl: string;
  partId?: string | null;
  partIndex?: number | null;
  verseNumber?: string | number | null;
  durationSeconds?: number | null;
  jobId?: string;
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
  solo: boolean;
  volume: number;
  gmProgram: number;
  percussion?: boolean;
};

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

const FLUID_R3_GM_SOUNDFONT_URL = "/soundfonts/FluidR3_GM.sf2";
const DEFAULT_INSTRUMENT_TRACK_VOLUME = 0.65;
let fluidR3SoundFontCachePromise: Promise<SoundFontCache> | null = null;

const loadFluidR3SoundFontCache = (): Promise<SoundFontCache> => {
  if (!fluidR3SoundFontCachePromise) {
    fluidR3SoundFontCachePromise = SoundFontCache.fromUrl(FLUID_R3_GM_SOUNDFONT_URL).catch((err) => {
      fluidR3SoundFontCachePromise = null;
      throw err;
    });
  }
  return fluidR3SoundFontCachePromise;
};

type ScorePlayerPlaybackControls = {
  play: (startTime?: number) => Promise<void>;
  pause: () => void;
  stop: () => void;
};

type ScorePlayerEngineProps = {
  midiUrl: string | null;
  vocalTracks: MultiTrackAudioTrack[];
  instrumentalTracks?: InstrumentalTrackState[];
  playbackRequestId: number;
  onControlsChange: (controls: ScorePlayerPlaybackControls | null) => void;
  onEngineLoading: () => void;
  onEngineReady: (requestId: number) => void;
  onPlaybackStateChange: (isPlaying: boolean) => void;
  onError: (message: string | null) => void;
};

const ScorePlayerEngineBridge = ({
  onControlsChange,
  loading,
  onReady,
  onPlaybackStateChange,
}: Pick<ScorePlayerEngineProps, "onControlsChange"> & {
  loading: boolean;
  onReady: () => void;
  onPlaybackStateChange: (isPlaying: boolean) => void;
}) => {
  const controls = usePlaylistControls();
  const { isReady } = usePlaylistData();
  const { isPlaying } = usePlaybackAnimation();
  const liveControlsRef = useRef(controls);
  liveControlsRef.current = controls;
  const stableControlsRef = useRef<ScorePlayerPlaybackControls | null>(null);
  if (!stableControlsRef.current) {
    stableControlsRef.current = {
      play: (startTime) => liveControlsRef.current.play(startTime),
      pause: () => liveControlsRef.current.pause(),
      stop: () => liveControlsRef.current.stop(),
    };
  }
  useEffect(() => {
    onControlsChange(stableControlsRef.current);
    return () => onControlsChange(null);
  }, [onControlsChange]);
  // Incremental track additions use the provider's engine.addTrack() path and
  // do not emit the provider-level onReady callback. Notify the outer player
  // once the already-live engine and its source loaders are both ready.
  useEffect(() => {
    if (isReady && !loading) onReady();
  }, [isReady, loading, onReady]);
  // This is the provider's native playback lifecycle state. In particular, it
  // switches to false when its engine reaches the end of the timeline, which
  // keeps the outer transport icon in sync after natural completion.
  useEffect(() => {
    onPlaybackStateChange(isPlaying);
  }, [isPlaying, onPlaybackStateChange]);
  return null;
};

const ScorePlayerMixerBridge = ({
  midiTrackCount,
  vocalTracks,
  instrumentalTracks = [],
}: {
  midiTrackCount: number;
  vocalTracks: MultiTrackAudioTrack[];
  instrumentalTracks?: InstrumentalTrackState[];
}) => {
  const controls = usePlaylistControls();
  const { isReady } = usePlaylistData();
  const controlsRef = useRef(controls);
  controlsRef.current = controls;
  const vocalMixerSignature = vocalTracks
    .map((track) => `${track.key}\u0000${track.muted}\u0000${track.solo}\u0000${track.volume}`)
    .join("\u0001");
  const instMixerSignature = instrumentalTracks
    .map((track) => `${track.key}\u0000${track.muted}\u0000${track.solo}\u0000${track.volume}`)
    .join("\u0001");

  useEffect(() => {
    if (!isReady) return;
    const currentControls = controlsRef.current;

    for (let i = 0; i < midiTrackCount; i++) {
      const track = instrumentalTracks[i] ?? (instrumentalTracks.length === 1 ? instrumentalTracks[0] : null);
      if (track) {
        currentControls.setTrackMute(i, track.muted);
        currentControls.setTrackSolo(i, track.solo);
        currentControls.setTrackVolume(i, track.volume);
      } else {
        currentControls.setTrackMute(i, false);
        currentControls.setTrackSolo(i, false);
        currentControls.setTrackVolume(i, 1);
      }
    }

    // Apply vocal tracks mixer state
    vocalTracks.forEach((track, index) => {
      const trackIndex = midiTrackCount + index;
      currentControls.setTrackMute(trackIndex, track.muted);
      currentControls.setTrackSolo(trackIndex, track.solo);
      currentControls.setTrackVolume(trackIndex, track.volume);
    });
  }, [isReady, midiTrackCount, vocalMixerSignature, instMixerSignature, instrumentalTracks, vocalTracks]);

  return null;
};

const ScorePlayerEngine = ({
  midiUrl,
  vocalTracks,
  instrumentalTracks = [],
  playbackRequestId,
  onControlsChange,
  onEngineLoading,
  onEngineReady,
  onPlaybackStateChange,
  onError,
}: ScorePlayerEngineProps) => {
  // WaveformPlaylistProvider treats callback identity changes as an engine
  // reconfiguration. Keep its callbacks stable so parent state updates (such
  // as setting the Play button to its active state) cannot dispose the MIDI
  // adapter while a note sequence is playing.
  const callbacksRef = useRef({ playbackRequestId, onEngineReady, onError });
  callbacksRef.current = { playbackRequestId, onEngineReady, onError };
  const handleProviderReady = useCallback(() => {
    const callbacks = callbacksRef.current;
    callbacks.onEngineReady(callbacks.playbackRequestId);
  }, []);
  const handleProviderError = useCallback((error: Error) => {
    callbacksRef.current.onError(error.message);
  }, []);
  const [soundFontCache, setSoundFontCache] = useState<SoundFontCache | null>(null);
  const [soundFontLoadState, setSoundFontLoadState] = useState<
    "idle" | "loading" | "ready" | "fallback"
  >("idle");
  const shouldLoadSoundFont = Boolean(midiUrl && instrumentalTracks.length > 0);
  const soundFontLoading =
    shouldLoadSoundFont &&
    soundFontLoadState !== "ready" &&
    soundFontLoadState !== "fallback";
  const midiConfigs = useMemo(
    () => (midiUrl ? [{ src: midiUrl, name: "Score instruments" }] : []),
    [midiUrl]
  );
  // Mixer changes are forwarded through usePlaylistControls below. Keeping
  // them out of the source configuration prevents useAudioTracks from
  // needlessly refetching/redecoding vocal audio on every Mute/Solo/volume edit.
  const vocalSourceSignature = vocalTracks
    .map((track) => `${track.key}\u0000${track.audioUrl}\u0000${track.label}\u0000${track.durationSeconds ?? ""}`)
    .join("\u0001");
  const vocalSources = useMemo(
    () =>
      vocalTracks.map((track) => ({
        key: `${track.key}\u0000${track.audioUrl}`,
        src: track.audioUrl,
        name: track.label,
        duration: track.durationSeconds ?? undefined,
        muted: track.muted,
        soloed: track.solo,
        volume: track.volume,
      })),
    // vocalSourceSignature deliberately excludes mixer-only state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [vocalSourceSignature]
  );
  const audioConfigs = useMemo(
    () => vocalSources.map(({ key: _key, ...config }) => config),
    [vocalSources]
  );
  const { tracks: midiTracks, loading: midiLoading, error: midiError } = useMidiTracks(
    midiConfigs,
    { sampleRate: 48_000 }
  );
  const { tracks: audioTracks, loading: audioLoading, error: audioError } = useAudioTracks(
    audioConfigs
  );
  const instrumentalProgramSignature = instrumentalTracks
    .map((track) => `${track.key}\u0000${track.gmProgram}`)
    .join("\u0001");
  const configuredMidiTracks = useMemo(
    () =>
      midiTracks.map((track, index) => {
        const instrumentalTrack =
          instrumentalTracks[index] ?? (instrumentalTracks.length === 1 ? instrumentalTracks[0] : null);
        if (!instrumentalTrack) return track;

        let changed = false;
        const clips = track.clips.map((clip) => {
          if (!clip.midiNotes || clip.midiNotes.length === 0) return clip;
          if (clip.midiProgram === instrumentalTrack.gmProgram) return clip;
          changed = true;
          return { ...clip, midiProgram: instrumentalTrack.gmProgram };
        });
        return changed ? { ...track, clips } : track;
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [midiTracks, instrumentalProgramSignature]
  );
  // useAudioTracks reloads its complete declarative source list when a new
  // vocal arrives. Retain the already-decoded ClipTrack object for every
  // unchanged source so WaveformPlaylistProvider recognizes the new vocal as
  // an incremental append and uses PlaylistEngine.addTrack(), rather than
  // rebuilding the running playout engine.
  const stableAudioTracksRef = useRef(new Map<string, (typeof audioTracks)[number]>());
  const stableAudioTracks = useMemo(() => {
    const next = new Map<string, (typeof audioTracks)[number]>();
    const normalized = audioTracks.map((track, index) => {
      const sourceKey = vocalSources[index]?.key;
      if (!sourceKey) return track;
      const existing = stableAudioTracksRef.current.get(sourceKey);
      const resolved = existing ?? track;
      next.set(sourceKey, resolved);
      return resolved;
    });
    stableAudioTracksRef.current = next;
    return normalized;
  }, [audioTracks, vocalSources]);
  const tracks = useMemo(
    () => [...configuredMidiTracks, ...stableAudioTracks],
    [configuredMidiTracks, stableAudioTracks]
  );
  const hasMountedPlayerRef = useRef(false);

  useEffect(() => {
    if (!shouldLoadSoundFont) {
      setSoundFontLoadState("idle");
      return;
    }

    let cancelled = false;
    setSoundFontLoadState((current) => (current === "ready" ? current : "loading"));
    void loadFluidR3SoundFontCache()
      .then((cache) => {
        if (cancelled) return;
        setSoundFontCache(cache);
        setSoundFontLoadState("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn("[score-player] FluidR3_GM.sf2 unavailable; falling back to default MIDI synth.", err);
        setSoundFontCache(null);
        setSoundFontLoadState("fallback");
      });

    return () => {
      cancelled = true;
    };
  }, [shouldLoadSoundFont]);

  useEffect(() => {
    onError(midiError ?? audioError);
  }, [audioError, midiError, onError]);

  useEffect(() => {
    onEngineLoading();
  }, [midiUrl, onEngineLoading, vocalSourceSignature]);

  useEffect(() => {
    if (soundFontLoading) onEngineLoading();
  }, [onEngineLoading, soundFontLoading]);

  // The documented integration mounts after the first complete set of sources.
  // Later vocal sources remain attached to the existing provider: its supported
  // incremental-add path calls PlaylistEngine.addTrack() without disposing
  // MIDI/audio tracks that are already playing.
  if (tracks.length === 0) {
    hasMountedPlayerRef.current = false;
    return null;
  }
  if (!hasMountedPlayerRef.current && (midiLoading || audioLoading)) {
    return null;
  }
  hasMountedPlayerRef.current = true;

  return (
    <WaveformPlaylistProvider
      tracks={tracks}
      soundFontCache={soundFontCache ?? undefined}
      onError={handleProviderError}
    >
      <ScorePlayerEngineBridge
        onControlsChange={onControlsChange}
        loading={midiLoading || audioLoading || soundFontLoading}
        onReady={handleProviderReady}
        onPlaybackStateChange={onPlaybackStateChange}
      />
      <ScorePlayerMixerBridge
        midiTrackCount={midiTracks.length}
        vocalTracks={vocalTracks}
        instrumentalTracks={instrumentalTracks}
      />
    </WaveformPlaylistProvider>
  );
};

const shouldMuteMultiTrackForPlayback = (
  track: MultiTrackAudioTrack,
  tracks: MultiTrackAudioTrack[]
): boolean => {
  const hasSolo = tracks.some((candidate) => candidate.solo);
  return hasSolo ? !track.solo : track.muted;
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
          max="1"
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
  onUpdateVocalMute: (key: string, muted: boolean) => void;
  onUpdateVocalSolo: (key: string, solo: boolean) => void;
  onUpdateVocalVolume: (key: string, volume: number) => void;
  onDownloadVocalTrack: (track: MultiTrackAudioTrack) => void;
  onUpdateInstrumentalMute: (partId: string, muted: boolean) => void;
  onUpdateInstrumentalSolo: (partId: string, solo: boolean) => void;
  onUpdateInstrumentalVolume: (partId: string, volume: number) => void;
  onUpdateInstrumentalGmProgram: (partId: string, gmProgram: number) => void;
};

const ScoreTrackControlsPanel = ({
  vocalTracks,
  instrumentalTracks,
  onUpdateVocalMute,
  onUpdateVocalSolo,
  onUpdateVocalVolume,
  onDownloadVocalTrack,
  onUpdateInstrumentalMute,
  onUpdateInstrumentalSolo,
  onUpdateInstrumentalVolume,
  onUpdateInstrumentalGmProgram,
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
        <span className="score-tracks-count-badge">
          {totalTracks} {totalTracks === 1 ? "track" : "tracks"}
        </span>
      </div>

      <div className="score-tracks-list">
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
                  max="1"
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

        {/* Instrumental / Accompaniment Tracks - FOLLOWS BELOW */}
        {instrumentalTracks.map((inst) => (
          <div
            key={inst.key}
            className={clsx("score-track-card score-track-row instrument-track", {
              "is-muted": inst.muted,
              "is-soloed": inst.solo,
            })}
            data-testid={`score-inst-track-${inst.partId}`}
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
                  onClick={() => onUpdateInstrumentalSolo(inst.partId, !inst.solo)}
                  aria-pressed={inst.solo}
                  title={inst.solo ? "Unsolo track" : "Solo track"}
                >
                  S
                </button>
                <button
                  type="button"
                  className={clsx("score-track-mute-btn", { muted: inst.muted })}
                  onClick={() => onUpdateInstrumentalMute(inst.partId, !inst.muted)}
                  aria-pressed={inst.muted}
                  title={inst.muted ? "Unmute track" : "Mute track"}
                >
                  {inst.muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
                </button>
              </div>
            </div>

            <div className="score-track-card-footer">
              <div className="score-track-instrument-picker">
                <select
                  className="score-track-instrument-select"
                  value={inst.gmProgram}
                  onChange={(e) => onUpdateInstrumentalGmProgram(inst.partId, parseInt(e.target.value, 10))}
                  aria-label={`${inst.label} instrument sound`}
                >
                  {GM_INSTRUMENTS.map((item) => (
                    <option key={item.program} value={item.program}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </div>

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
                  onChange={(e) => onUpdateInstrumentalVolume(inst.partId, parseFloat(e.target.value))}
                  aria-label={`${inst.label} volume`}
                />
                <span className="score-track-volume-label">{Math.round(inst.volume * 100)}%</span>
              </div>
            </div>
          </div>
        ))}
      </div>
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

const buildVerseOptions = (summary: ScoreSummary | null): string[] => {
  if (!summary) return [];
  const verses =
    summary.available_verses && summary.available_verses.length > 0
      ? summary.available_verses
      : ["1"];
  return verses.map((value) => String(value));
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

const renderScorePageLayoutForPrint = async (scoreData: string): Promise<string> => {
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
    osmd.zoom = 1;
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

const shouldPromptSelection = (summary: ScoreSummary | null): boolean => {
  const parts = buildPartOptions(summary);
  const verses = buildVerseOptions(summary);
  return (parts.length > 1 || verses.length > 1) && parts.length > 0 && verses.length > 0;
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

export default function MainApp() {
  const navigate = useNavigate();
  const { user, isAuthenticated } = useAuth();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [score, setScore] = useState<ScorePayload | null>(null);
  const [scoreSummary, setScoreSummary] = useState<ScoreSummary | null>(null);
  const [performanceMidi, setPerformanceMidi] = useState<PerformanceMidi | null>(null);
  const [instrumentalTracks, setInstrumentalTracks] = useState<InstrumentalTrackState[]>([]);
  const [instrumentalMidiUrl, setInstrumentalMidiUrl] = useState<string | null>(null);
  const [scorePlayerControls, setScorePlayerControls] =
    useState<ScorePlayerPlaybackControls | null>(null);
  const [scorePlayerError, setScorePlayerError] = useState<string | null>(null);
  const [scorePlayerPlaybackRequestId, setScorePlayerPlaybackRequestId] = useState(0);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [multiTrackAudioTracks, setMultiTrackAudioTracks] = useState<MultiTrackAudioTrack[]>([]);
  const [multiTrackPlaying, setMultiTrackPlaying] = useState(false);
  const [multiTrackExportProgress, setMultiTrackExportProgress] = useState<number | null>(null);
  const [multiTrackExportError, setMultiTrackExportError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [splitPct, setSplitPct] = useState(40);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [scorePreviewLayout, setScorePreviewLayout] = useState<ScorePreviewLayout>("page");
  const [expandRepeats, setExpandRepeats] = useState(true);
  const [scoreReady, setScoreReady] = useState(false);
  const [scorePreviewError, setScorePreviewError] = useState<string | null>(null);
  const [selectedPartKey, setSelectedPartKey] = useState<string | null>(null);
  const [selectedVerse, setSelectedVerse] = useState<string | null>(null);
  const [pendingSelection, setPendingSelection] = useState(false);
  const [selectorShown, setSelectorShown] = useState(false);
  const [voicebanks, setVoicebanks] = useState<VoicebankOption[]>([]);
  const [voicebanksLoading, setVoicebanksLoading] = useState(false);
  const [selectedVoicebankId, setSelectedVoicebankId] = useState<string | null>(null);
  const [failedVoiceImages, setFailedVoiceImages] = useState<Record<string, boolean>>({});
  const [voiceMenuOpen, setVoiceMenuOpen] = useState(false);
  const [solfegeMenuOpen, setSolfegeMenuOpen] = useState(false);
  const [showSolfegeHint, setShowSolfegeHint] = useState(
    () =>
      typeof window === "undefined" ||
      window.localStorage.getItem(SOLFEGE_GUIDE_DISMISSED_KEY) !== "true"
  );
  const [showMultitrackTutorial, setShowMultitrackTutorial] = useState(
    () =>
      typeof window === "undefined" ||
      window.localStorage.getItem(MULTITRACK_TUTORIAL_DISMISSED_KEY) !== "true"
  );
  const [multitrackTutorialStepIndex, setMultitrackTutorialStepIndex] = useState(0);
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
  const [activeProgress, setActiveProgress] = useState<{
    messageId: string;
    url: string;
  } | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const multiTrackWaveSurferRefs = useRef<Record<string, WaveSurfer | null>>({});
  const audioRefreshPromisesRef = useRef<Record<string, Promise<string | null> | undefined>>({});
  const scorePlayerAudioRefreshAttemptsRef = useRef<Set<string>>(new Set());
  const scorePlayerControlsRef = useRef<ScorePlayerPlaybackControls | null>(null);
  const scorePlayerPlaybackRequestRef = useRef(0);
  const scorePlayerPlayPendingRef = useRef(false);
  const scorePlayerAssetsReadyRef = useRef(false);
  const voicePickerRef = useRef<HTMLDivElement | null>(null);
  const solfegePickerRef = useRef<HTMLDivElement | null>(null);
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
  const suppressedMultiTrackMessageIdsRef = useRef<Set<string>>(new Set());

  const setChatTurnBusy = (busy: boolean) => {
    chatTurnInProgressRef.current = busy;
    setChatTurnInProgress(busy);
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
  }, []);

  const handleScorePlayerPlaybackStateChange = useCallback((isPlaying: boolean) => {
    setMultiTrackPlaying((current) => (current === isPlaying ? current : isPlaying));
  }, []);

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
      markAsSeen
  } = useAnnouncements();

  const estimatedDuration = expandRepeats
    ? (scoreSummary?.expanded_duration_seconds ?? scoreSummary?.duration_seconds)
    : scoreSummary?.duration_seconds;
  const estimatedDurationLabel =
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? `Estimated duration: ${formatDuration(estimatedDuration)}`
      : null;

  const estimatedCost = 
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? Math.ceil(estimatedDuration / 30)
      : null;
  const estimatedCostLabel = estimatedCost !== null ? `Estimated cost per part: ${estimatedCost} credits` : null;
  const hasScorePlayerTracks = Boolean(instrumentalMidiUrl || (instrumentalTracks.length > 0 && performanceMidi?.has_instrumental_parts)) || multiTrackAudioTracks.length > 0;
  const selectedVoice = voicebanks.find((voice) => voice.id === selectedVoicebankId) ?? null;
  const selectedVoiceLabel = selectedVoice ? selectedVoice.name : "Use Recommended";
  const solfegeSystemLabel = solfegeSystem === "movable_do" ? "Movable Do" : "Fixed Do";
  const solfegeModeLabel =
    solfegeMode === "major"
      ? "Major"
      : solfegeMode === "minor_la_based"
        ? "Minor (La)"
        : "Minor (Do)";
  const multiTrackExportPercent =
    multiTrackExportProgress !== null
      ? `${Math.round(Math.max(0, Math.min(1, multiTrackExportProgress)) * 100)}%`
      : null;
  const exportMixRequiredCredits = estimateExportMixCredits(
    multiTrackAudioTracks[0]?.durationSeconds
  );
  const currentMultitrackTutorialStep =
    MULTITRACK_TUTORIAL_STEPS[
      Math.min(multitrackTutorialStepIndex, MULTITRACK_TUTORIAL_STEPS.length - 1)
    ];
  const multitrackTutorialVisible =
    showMultitrackTutorial && Boolean(currentMultitrackTutorialStep);
  const isMultitrackTutorialTarget = (target: MultitrackTutorialTarget) =>
    multitrackTutorialVisible && currentMultitrackTutorialStep.target === target;

  useEffect(() => {
    if (!performanceMidi?.instrumental_parts) {
      setInstrumentalTracks([]);
      return;
    }
    const eligible = performanceMidi.instrumental_parts.filter((p) => p.eligible);
    setInstrumentalTracks((current) => {
      return eligible.map((part) => {
        const existing = current.find(
          (t) => t.partId === part.part_id || t.rawPartId === part.raw_part_id
        );
        return {
          key: `inst-${part.part_id || part.part_index}`,
          partId: part.part_id,
          rawPartId: part.raw_part_id,
          partIndex: part.part_index,
          label: part.label || `Part ${part.part_index + 1}`,
          muted: existing ? existing.muted : false,
          solo: existing ? existing.solo : false,
          volume: existing ? existing.volume : DEFAULT_INSTRUMENT_TRACK_VOLUME,
          gmProgram: existing ? existing.gmProgram : (part.midi_program ?? 0),
          percussion: part.percussion,
        };
      });
    });
  }, [performanceMidi]);

  const updateInstrumentalTrackMute = useCallback((partId: string, muted: boolean) => {
    setInstrumentalTracks((current) =>
      current.map((track) =>
        track.partId === partId ? { ...track, muted, solo: muted ? false : track.solo } : track
      )
    );
  }, []);

  const updateInstrumentalTrackSolo = useCallback((partId: string, solo: boolean) => {
    setInstrumentalTracks((current) =>
      current.map((track) =>
        track.partId === partId ? { ...track, solo, muted: solo ? false : track.muted } : track
      )
    );
  }, []);

  const updateInstrumentalTrackVolume = useCallback((partId: string, volume: number) => {
    const normalized = Math.max(0, Math.min(1, Number.isFinite(volume) ? volume : 1));
    setInstrumentalTracks((current) =>
      current.map((track) => (track.partId === partId ? { ...track, volume: normalized } : track))
    );
  }, []);

  const updateInstrumentalTrackGmProgram = useCallback((partId: string, gmProgram: number) => {
    setInstrumentalTracks((current) =>
      current.map((track) => (track.partId === partId ? { ...track, gmProgram } : track))
    );
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
    void fetchInstrumentalMidi(sessionId, expandRepeats)
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
  }, [expandRepeats, performanceMidi?.has_instrumental_parts, sessionId]);

  const dismissMultitrackTutorial = useCallback(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(MULTITRACK_TUTORIAL_DISMISSED_KEY, "true");
    }
    setShowMultitrackTutorial(false);
  }, []);

  const advanceMultitrackTutorial = useCallback(() => {
    if (multitrackTutorialStepIndex >= MULTITRACK_TUTORIAL_STEPS.length - 1) {
      dismissMultitrackTutorial();
      return;
    }
    setMultitrackTutorialStepIndex((current) =>
      Math.min(current + 1, MULTITRACK_TUTORIAL_STEPS.length - 1)
    );
  }, [dismissMultitrackTutorial, multitrackTutorialStepIndex]);

  const partOptions = useMemo(() => buildPartOptions(scoreSummary), [scoreSummary]);
  const verseOptions = useMemo(() => buildVerseOptions(scoreSummary), [scoreSummary]);

  const resolveMultiTrackIdentity = useCallback(
    (
      audioTrack?: AudioTrackMetadata
    ): Omit<
      MultiTrackAudioTrack,
      "audioUrl" | "durationSeconds" | "jobId" | "muted" | "solo" | "volume"
    > => {
      const partIndex =
        typeof audioTrack?.part_index === "number" && Number.isFinite(audioTrack.part_index)
          ? audioTrack.part_index
          : null;
      const partId = typeof audioTrack?.part_id === "string" ? audioTrack.part_id.trim() : "";
      const key =
        (typeof audioTrack?.key === "string" && audioTrack.key.trim()) ||
        (partId ? `id:${partId}` : partIndex !== null ? `index:${partIndex}` : selectedPartKey || "latest-vocal");
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
        verseNumber: audioTrack?.verse_number ?? selectedVerse ?? null,
      };
    },
    [partOptions, selectedPartKey, selectedVerse]
  );

  const addOrReplaceMultiTrackAudio = useCallback(
    (
      audioUrl: string,
      audioTrack?: AudioTrackMetadata,
      jobId?: string,
      durationSeconds?: number | null,
      replaceExistingUrl = false
    ) => {
      if (!audioUrl) return;
      const identity = resolveMultiTrackIdentity(audioTrack);
      setMultiTrackAudioTracks((current) => {
        const existing = current.find((track) => track.key === identity.key);
        const hasBackendDuration =
          typeof durationSeconds === "number" &&
          Number.isFinite(durationSeconds) &&
          durationSeconds > 0;
        const nextAudioUrl = existing && !replaceExistingUrl ? existing.audioUrl : audioUrl;
        const nextTrack: MultiTrackAudioTrack = {
          ...identity,
          audioUrl: nextAudioUrl,
          jobId: jobId ?? existing?.jobId,
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
            existing.durationSeconds === nextTrack.durationSeconds &&
            existing.label === nextTrack.label &&
            existing.partId === nextTrack.partId &&
            existing.partIndex === nextTrack.partIndex &&
            existing.verseNumber === nextTrack.verseNumber
          ) {
            return current;
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
        ...multiTrackAnalyticsParams(multiTrackAudioTracks),
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
        if (scorePlayerError) {
          const requestId = scorePlayerPlaybackRequestRef.current + 1;
          scorePlayerPlaybackRequestRef.current = requestId;
          setScorePlayerPlaybackRequestId(requestId);
        }
      }
      return;
    }
    const playable = multiTrackAudioTracks
      .map((track) => ({ track, waveSurfer: multiTrackWaveSurferRefs.current[track.key] }))
      .filter((entry): entry is { track: MultiTrackAudioTrack; waveSurfer: WaveSurfer } =>
        Boolean(entry.waveSurfer)
      );
    if (!playable.length) return;
    const baseTime =
      playable.find(({ waveSurfer }) => waveSurfer.getCurrentTime() > 0)?.waveSurfer.getCurrentTime() ?? 0;
    logAnalyticsEvent("multitrack_play", {
      ...multiTrackAnalyticsParams(multiTrackAudioTracks),
      playback_start_seconds: Math.round(baseTime),
    });
    playable.forEach(({ track, waveSurfer }) => {
      const duration = waveSurfer.getDuration();
      waveSurfer.setTime(Math.min(baseTime, Number.isFinite(duration) ? duration : baseTime));
      waveSurfer.setVolume(track.volume);
      waveSurfer.setMuted(shouldMuteMultiTrackForPlayback(track, multiTrackAudioTracks));
    });
    void Promise.allSettled(playable.map(({ waveSurfer }) => waveSurfer.play())).then(() => {
      setMultiTrackPlaying(true);
    });
  }, [instrumentalMidiUrl, multiTrackAudioTracks, scorePlayerControls, scorePlayerError]);

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
      setMultiTrackPlaying(false);
      return;
    }
    Object.values(multiTrackWaveSurferRefs.current).forEach((waveSurfer) => {
      waveSurfer?.stop();
    });
    setMultiTrackPlaying(false);
  }, [scorePlayerControls]);

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
    const normalized = Math.max(0, Math.min(1, volume));
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

  const handleMultiTrackExport = useCallback(async () => {
    if (!sessionId || multiTrackExportProgress !== null) return;
    if (creditsLocked) {
      openPaywall("insufficient_credits");
      return;
    }
    setMultiTrackExportError(null);
    setError(null);
    const billingReferenceTrack = multiTrackAudioTracks[0];
    if (!billingReferenceTrack?.jobId || exportMixRequiredCredits === null) {
      setMultiTrackExportError("Export credits are still being calculated.");
      return;
    }
    const hasSolo = multiTrackAudioTracks.some((track) => track.solo);
    const audibleTracks = hasSolo
      ? multiTrackAudioTracks.filter((track) => track.solo)
      : multiTrackAudioTracks.filter((track) => !track.muted);
    if (!audibleTracks.length) {
      setMultiTrackExportError("No audible tracks selected.");
      return;
    }
    const missingMetadata = audibleTracks.find((track) => !track.jobId || !track.partId);
    if (missingMetadata) {
      setMultiTrackExportError("Export requires generated tracks with part IDs.");
      return;
    }
    logAnalyticsEvent("multitrack_export_mix", {
      ...multiTrackAnalyticsParams(multiTrackAudioTracks),
    });
    setMultiTrackExportProgress(0);
    try {
      const response = await exportMix(
        sessionId,
        multiTrackAudioTracks.map((track) => ({
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
      let latestProgress = 0;
      for (;;) {
        await wait(1200);
        const payload = await fetchProgress(response.progress_url);
        if (typeof payload.progress === "number") {
          latestProgress = Math.max(latestProgress, payload.progress);
          setMultiTrackExportProgress(latestProgress);
        }
        if (payload.status === "done" && payload.audio_url) {
          setMultiTrackExportProgress(1);
          await downloadAudioUrl(payload.audio_url, "sightsinger-mix.wav");
          break;
        }
        if (payload.status === "error") {
          throw new Error(payload.error || "Export failed.");
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Export failed.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
        setError(message);
        return;
      }
      setMultiTrackExportError(message);
    } finally {
      setMultiTrackExportProgress(null);
    }
  }, [creditsLocked, exportMixRequiredCredits, multiTrackAudioTracks, multiTrackExportProgress, openPaywall, sessionId]);

  const handleMultiTrackTrackDownload = useCallback(
    async (track: MultiTrackAudioTrack) => {
      try {
        await downloadAudioUrl(track.audioUrl, "");
      } catch (err: unknown) {
        if (!isExpiredAudioDownloadError(err) || !sessionId || !track.jobId) {
          setError(err instanceof Error ? err.message : "Failed to download audio.");
          return;
        }
        try {
          const payload = await fetchProgress(
            `/sessions/${sessionId}/progress?job_id=${encodeURIComponent(track.jobId)}`
          );
          if (!payload.audio_url) throw new Error("Audio link expired. Please try again.");
          await downloadAudioUrl(payload.audio_url, "");
        } catch (refreshError: unknown) {
          setError(refreshError instanceof Error ? refreshError.message : "Failed to refresh audio download.");
          return;
        }
      }
      logAnalyticsEvent("multitrack_track_download", {
        ...multiTrackAnalyticsParams(multiTrackAudioTracks),
        has_part_id: Boolean(track.partId),
        has_verse_number: Boolean(track.verseNumber),
      });
    },
    [multiTrackAudioTracks, sessionId]
  );

  useEffect(() => {
    multiTrackAudioTracks.forEach((track) => {
      const waveSurfer = multiTrackWaveSurferRefs.current[track.key];
      if (!waveSurfer) return;
      waveSurfer.setMuted(shouldMuteMultiTrackForPlayback(track, multiTrackAudioTracks));
      waveSurfer.setVolume(track.volume);
    });
  }, [multiTrackAudioTracks]);

  useEffect(() => {
    messages.forEach((message) => {
      if (message.role !== "assistant" || !message.audioUrl) return;
      if (suppressedMultiTrackMessageIdsRef.current.has(message.id)) return;
      addOrReplaceMultiTrackAudio(message.audioUrl, message.audioTrack, message.jobId);
    });
  }, [addOrReplaceMultiTrackAudio, messages]);

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
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
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
    if (!voiceMenuOpen && !solfegeMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        (voicePickerRef.current?.contains(target) || solfegePickerRef.current?.contains(target))
      ) {
        return;
      }
      setVoiceMenuOpen(false);
      setSolfegeMenuOpen(false);
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setVoiceMenuOpen(false);
        setSolfegeMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [solfegeMenuOpen, voiceMenuOpen]);

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
    const promise = createSession()
      .then((data) => {
        if (activeUserIdRef.current === requestUserId) {
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
    void ensureSession().catch(() => {
      // Error is stored in component state and retried on the next session-dependent action.
    });
  }, [ensureSession, isAuthenticated, sessionId, user]);

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
    if (!scoreRef.current || !score) return;
    let cancelled = false;
    beginScorePreviewTrap();
    setScoreReady(false);
    setScorePreviewError(null);
    scoreRef.current.replaceChildren();
    let osmd: OpenSheetMusicDisplay | null = null;

    void (async () => {
      try {
        if (cancelled || !scoreRef.current) return;

        osmd = new OpenSheetMusicDisplay(scoreRef.current, {
          autoResize: true,
          drawTitle: true,
          followCursor: false,
          pageFormat: scorePreviewLayout === "page" ? "A4_P" : "Endless",
          renderSingleHorizontalStaffline: scorePreviewLayout === "horizontal",
        });
        osmdRef.current = osmd;

        await osmd.load(score.data);
        if (cancelled) return;

        beginScorePreviewTrap();
        osmd.zoom = zoomLevel;
        osmd.render();
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
  }, [beginScorePreviewTrap, endScorePreviewTrap, handleScorePreviewFailure, score, scorePreviewLayout]);

  useEffect(() => {
    if (!scoreReady || !osmdRef.current) return;
    try {
      beginScorePreviewTrap();
      osmdRef.current.zoom = zoomLevel;
      osmdRef.current.render();
    } catch {
      handleScorePreviewFailure();
    }
  }, [beginScorePreviewTrap, handleScorePreviewFailure, zoomLevel, scoreReady]);

  useEffect(() => {
    if (!activeProgress) return;
    let cancelled = false;

    const appendProgressMessage = (current: string, incoming?: string | null): string => {
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
    };

    const applyProgress = (payload: ProgressResponse) => {
      const nextMessage = payload.message;
      const nextProgress = payload.progress;
      const nextAudioUrl = payload.audio_url;
      const appendTerminalPreprocessMessage =
        payload.job_kind === "preprocess" &&
        (payload.status === "done" || payload.status === "error");
      const appendProgressToChatBubble =
        payload.status !== "error" || payload.job_kind === "preprocess";
      const isTerminalProgress =
        payload.status === "done" ||
        payload.status === "error" ||
        payload.status === "action_required";
      const nextAttemptMessages = extractAttemptMessages(payload.details);
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== activeProgress.messageId) return msg;
          const nextContent =
            payload.job_kind === "preprocess" && payload.status === "running"
              ? msg.content
              : appendTerminalPreprocessMessage
                ? appendPreprocessTerminalMessage(msg.content, nextMessage)
                : appendProgressToChatBubble
                  ? appendProgressMessage(msg.content, nextMessage)
                  : msg.content;
          return {
            ...msg,
            content: nextContent,
            details: payload.details ?? msg.details,
            attemptMessages: nextAttemptMessages ?? msg.attemptMessages,
            progressValue: typeof nextProgress === "number" ? nextProgress : msg.progressValue,
            // Progress polling returns a newly signed URL each time. Keep the first
            // one rather than rebuilding the player and re-fetching audio on every poll.
            audioUrl: msg.audioUrl || nextAudioUrl,
            audioTrack: payload.audio_track ?? msg.audioTrack,
            jobId: payload.job_id ?? msg.jobId,
            feedback: payload.feedback ?? msg.feedback,
            isProgress: !isTerminalProgress,
          };
        })
      );
      if (nextAudioUrl) {
        setAudioUrl((current) => current || nextAudioUrl);
        if (payload.job_kind !== "preprocess") {
          addOrReplaceMultiTrackAudio(
            nextAudioUrl,
            payload.audio_track,
            payload.job_id,
            payload.actual_duration_seconds
          );
        }
      }
    };

    const poll = async () => {
      try {
        const payload = await fetchProgress(activeProgress.url);
        if (cancelled) return;
        applyProgress(payload);
        if (payload.status === "done" && payload.review_required) {
          await refreshScorePreview();
        }
        if (payload.warning) {
          setError(payload.warning);
        }
        if (payload.status === "done") {
          setActiveProgress(null);
          setChatTurnBusy(false);
        }
        if (payload.status === "error") {
          setActiveProgress(null);
          setChatTurnBusy(false);
          const fallbackError =
            payload.job_kind === "preprocess" ? "Preprocess failed." : "Synthesis failed.";
          const baseMessage = payload.message || fallbackError;
          setError(
            payload.error && payload.error !== baseMessage
              ? `${baseMessage} Reason: ${payload.error}`
              : baseMessage
          );
        }
        if (payload.status === "action_required") {
          setActiveProgress(null);
          setChatTurnBusy(false);
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err?.message || "Failed to fetch synthesis progress.");
          setActiveProgress(null);
          setChatTurnBusy(false);
        }
      }
    };

    poll();
    const interval = window.setInterval(poll, 1200);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [activeProgress, addOrReplaceMultiTrackAudio]);

  useEffect(() => {
    const container = chatStreamRef.current;
    if (!container) return;
    if (!shouldAutoScrollRef.current) return;
    container.scrollTop = container.scrollHeight;
  }, [messages, status]);

  const handleChatScroll = () => {
    const container = chatStreamRef.current;
    if (!container) return;
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom < 48;
  };

  const appendMessage = (message: Message) => {
    setMessages((prev) => [...prev, message]);
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

  const refreshScorePreview = async () => {
    if (!sessionId || !score) return;
    const data = await fetchScoreXml(sessionId);
    setScore({ name: score.name, data });
  };

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
      scoreMarkup = await renderScorePageLayoutForPrint(score.data);
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
      setAudioUrl((current) => (current ? nextAudioUrl : current));
      if (payload.job_kind !== "preprocess") {
        addOrReplaceMultiTrackAudio(
          nextAudioUrl,
          payload.audio_track,
          payload.job_id,
          payload.actual_duration_seconds,
          true
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

  const handleAudioPlaybackError = async (
    messageId: string,
    progressUrl?: string,
    jobId?: string
  ) => {
    try {
      const nextAudioUrl = await refreshMessageAudioUrl(messageId, progressUrl, jobId);
      if (!nextAudioUrl) {
        setError("Audio link expired. Please try again.");
        return;
      }
      const audio = audioRefs.current[messageId];
      if (audio) {
        const currentTime = audio.currentTime;
        const retryPlayback = () => {
          audio.removeEventListener("canplay", retryPlayback);
          if (currentTime > 0 && Number.isFinite(currentTime)) {
            try {
              audio.currentTime = currentTime;
            } catch {
              // Ignore seek failures on freshly loaded media.
            }
          }
          void audio.play().catch(() => undefined);
        };
        audio.addEventListener("canplay", retryPlayback, { once: true });
        audio.src = nextAudioUrl;
        audio.load();
      }
    } catch (err: any) {
      setError(err?.message || "Failed to refresh audio playback.");
    }
  };

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
      if (!audioUrl) {
        setError("No audio available to download.");
        return;
      }
      try {
        await downloadAudioUrl(audioUrl, "");
      } catch (downloadError: unknown) {
        if (!isExpiredAudioDownloadError(downloadError)) throw downloadError;
        const nextAudioUrl = await refreshMessageAudioUrl(messageId, progressUrl, jobId);
        if (!nextAudioUrl) throw new Error("Audio link expired. Please try again.");
        await downloadAudioUrl(nextAudioUrl, "");
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

  const handleUpload = async (file: File) => {
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    setUploading(true);
    setError(null);
    setScorePreviewError(null);
    messages.forEach((message) => {
      if (message.audioUrl) {
        suppressedMultiTrackMessageIdsRef.current.add(message.id);
      }
    });
    handleMultiTrackStop();
    setMultiTrackAudioTracks([]);
    setInstrumentalTracks([]);
    setPerformanceMidi(null);
    setMultiTrackExportProgress(null);
    setMultiTrackExportError(null);
    multiTrackWaveSurferRefs.current = {};
    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const uploadResponse = await uploadScore(activeSessionId, file);
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
      setExpandRepeats(true);
      setPendingSelection(shouldPromptSelection(summary));
      setSelectorShown(false);
      const nextPartOptions = buildPartOptions(summary);
      const nextVerseOptions = buildVerseOptions(summary);
      setSelectedPartKey(nextPartOptions[0]?.key ?? null);
      setSelectedVerse(nextVerseOptions[0] ?? null);
      const data = await fetchScoreXml(activeSessionId);
      setScore({ name: file.name, data });
    } catch (err: any) {
      const message = err?.message || "Upload failed.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
      }
      setError(message);
    } finally {
      setUploading(false);
    }
  };

  const sendMessage = async (
    content: string,
    selection?: ChatSelection,
    voicebankId: string | null = selectedVoicebankId
  ) => {
    if (!content.trim()) return;
    if (chatTurnInProgressRef.current) return;
    if (creditsLocked) {
      openPaywall(selection ? "selection_blocked" : "chat_blocked");
      return;
    }
    let keepTurnBusy = false;
    setChatTurnBusy(true);
    setStatus("Thinking...");
    setError(null);
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      content,
    });

    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const response = await chat(
        activeSessionId,
        content,
        selection,
        voicebankId,
        expandRepeats
      );
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
      if (
        response.type === "chat_text" &&
        pendingSelection &&
        !selectorShown &&
        !response.suppress_selector
      ) {
        assistantMessage.showSelector = true;
        setSelectorShown(true);
      }
      if (response.type === "chat_audio") {
        assistantMessage.audioUrl = response.audio_url;
        assistantMessage.audioTrack = response.audio_track;
        setAudioUrl(response.audio_url);
        addOrReplaceMultiTrackAudio(response.audio_url, response.audio_track);
        if (pendingSelection) {
          setPendingSelection(false);
        }
      }
      if (response.type === "chat_progress") {
        assistantMessage.progressUrl = response.progress_url;
        assistantMessage.isProgress = true;
        assistantMessage.jobId = response.job_id;
        keepTurnBusy = true;
        if (pendingSelection) {
          setPendingSelection(false);
        }
      }
      if ("current_score" in response && response.current_score) {
        await refreshScorePreview();
      }
      if ("score_summary" in response && response.score_summary) {
        setScoreSummary(response.score_summary);
        setPerformanceMidi(response.score_summary.performance_midi ?? null);
        if (response.score_summary.selected_verse_number != null) {
          setSelectedVerse(String(response.score_summary.selected_verse_number));
        }
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
      appendMessage(assistantMessage);
      if (response.type === "chat_progress") {
        setActiveProgress({ messageId: assistantMessage.id, url: response.progress_url });
      }
    } catch (err: any) {
      const message = err?.message || "Failed to send message.";
      if (isInsufficientCreditError(message)) {
        openPaywall("insufficient_credits", message);
      }
      setError(message);
    } finally {
      setStatus(null);
      if (!keepTurnBusy) {
        setChatTurnBusy(false);
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
    await sendMessage(content, undefined, voicebankId);
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

  const handleSelectionSend = async () => {
    if (!selectedPartKey || !selectedVerse) return;
    if (chatTurnInProgressRef.current) return;
    if (creditsLocked) {
      openPaywall("selection_blocked");
      return;
    }
    const selected = partOptions.find((option) => option.key === selectedPartKey);
    if (!selected) return;
    const partDescriptor = selected.part_name
      ? `the ${selected.part_name} part`
      : selected.part_id
        ? `part ${selected.part_id}`
        : `part ${selected.part_index + 1}`;
    const message = `Please sing ${partDescriptor}, verse ${selectedVerse}.`;
    setPendingSelection(false);
    await sendMessage(message, {
      part_index: selected.part_index,
      part_id: selected.part_id,
      verse_number: selectedVerse,
    }, selectedVoicebankId);
  };

  const canShowSelector =
    pendingSelection && partOptions.length > 0 && verseOptions.length > 0;

  const handleZoom = (delta: number) => {
    const next = Math.min(2, Math.max(0.6, zoomLevel + delta));
    setZoomLevel(Math.round(next * 10) / 10);
  };

  const handleDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (creditsLocked) return;
    if (!isDragging) setIsDragging(true);
  };

  const handleDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node)) return;
    setIsDragging(false);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (creditsLocked) {
      openPaywall("drag_blocked");
      return;
    }
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) handleUpload(file);
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
    <div
      className={clsx("app-shell", {
        "multitrack-tutorial-active": multitrackTutorialVisible,
      })}
    >
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
        <div className="header-actions">
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
          <span className="message-box-content">{error}</span>
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
        className="split-grid"
        ref={layoutRef}
        style={splitStyle}
      >
        <section
          className={clsx("chat-panel", isDragging && "drag-active")}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="chat-header">
            <h2>Studio Chat</h2>
            <span className="chat-subtitle">Natural language takes, no DAW edits</span>
          </div>
          <div className="chat-stream" ref={chatStreamRef} onScroll={handleChatScroll} data-testid="chat-stream">
            {messages.length === 0 && (
              <div className="empty-state">
                <p>Drop a MusicXML file here to begin.</p>
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
                  "reveal"
                )}
                style={{ animationDelay: `${index * 40}ms` }}
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
                {msg.isProgress && !msg.audioUrl && (
                  <div className="thinking-dots" aria-label="Processing">
                    <span />
                    <span />
                    <span />
                  </div>
                )}
                {msg.showSelector && canShowSelector && (
                  <div className="selection-panel">
                    <div className="selection-grid">
                      <label className="selection-field">
                        <span className="selection-label">Part</span>
                        <select
                          data-testid="part-selection"
                          className="selection-select"
                          value={selectedPartKey ?? ""}
                          onChange={(event) => setSelectedPartKey(event.target.value)}
                        >
                          {partOptions.map((option) => (
                            <option key={option.key} value={option.key}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="selection-field">
                        <span className="selection-label">Verse</span>
                        <select
                          data-testid="verse-selection"
                          className="selection-select"
                          value={selectedVerse ?? ""}
                          onChange={(event) => setSelectedVerse(event.target.value)}
                        >
                          {verseOptions.map((verse) => (
                            <option key={verse} value={verse}>
                              Verse {verse}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <div className="selection-actions">
                      <button
                        type="button"
                        className="selection-send"
                        data-testid="use-selection"
                        onClick={handleSelectionSend}
                        disabled={!selectedPartKey || !selectedVerse || chatTurnInProgress}
                      >
                        Use selection
                      </button>
                      <span className="selection-hint">Or type your request below.</span>
                    </div>
                  </div>
                )}
                {msg.audioUrl && (
                  <div className="audio-actions">
                    <audio
                      ref={(element) => {
                        if (element) {
                          audioRefs.current[msg.id] = element;
                        } else {
                          delete audioRefs.current[msg.id];
                        }
                      }}
                      className="audio-player"
                      data-testid="synthesis-audio"
                      controls
                      preload="none"
                      src={msg.audioUrl}
                      onPlay={() => {
                        logAnalyticsEvent("synthesis_audio_play", synthesisAudioAnalyticsParams(msg));
                        openFeedbackPrompt(msg, "audio_played");
                      }}
                      onError={() => {
                        void handleAudioPlaybackError(msg.id, msg.progressUrl, msg.jobId);
                      }}
                    />
                    <button
                      type="button"
                      className="audio-download-button"
                      aria-label="Download audio"
                      title="Download audio"
                      onClick={() => {
                        void handleAudioDownload(
                          msg.id,
                          msg.audioUrl,
                          msg.progressUrl,
                          msg.jobId
                        );
                      }}
                    >
                      <Download size={16} aria-hidden="true" />
                    </button>
                  </div>
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
              {STARTING_CONVERSATIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  className="starting-conversation-button"
                  disabled={creditsLocked}
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
                disabled={creditsLocked}
                rows={2}
              />
              <button
                onClick={handleSend}
                className="send-button"
                disabled={!input.trim() || creditsLocked || chatTurnInProgress}
                aria-disabled={creditsLocked || chatTurnInProgress}
                aria-label="Send message"
                data-testid="send-message"
              >
                <Send size={18} />
              </button>
            </div>
            <div className="composer-menu-bar" aria-label="Composer settings">
              <label
                className="upload-button composer-upload-button"
                title="Upload Score"
                aria-label={uploading ? "Uploading score" : "Upload Score"}
                onClick={(event) => {
                  if (creditsLocked) {
                    event.preventDefault();
                    openPaywall("upload_blocked");
                  }
                }}
              >
                <UploadCloud size={18} />
                <span>{uploading ? "Uploading..." : "Upload Score"}</span>
                <input
                  type="file"
                  data-testid="score-upload-input"
                  accept=".xml,.mxl"
                  disabled={uploading || creditsLocked}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) handleUpload(file);
                  }}
                />
              </label>
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
                        <span className="voice-picker-option-name">Use Recommended</span>
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
                  className={clsx("voice-picker-trigger", { open: voiceMenuOpen })}
                  aria-haspopup="listbox"
                  aria-expanded={voiceMenuOpen}
                  onClick={() => {
                    setVoiceMenuOpen((open) => !open);
                    setSolfegeMenuOpen(false);
                  }}
                >
                  {selectedVoice ? (
                    renderVoiceAvatar(selectedVoice, "voice-picker-trigger-avatar")
                  ) : (
                    <span
                      className="voice-picker-trigger-avatar recommended"
                      aria-hidden="true"
                    >
                      <Sparkles size={15} />
                    </span>
                  )}
                  <span className="voice-picker-trigger-copy">
                    <span className="voice-picker-trigger-label">Voice</span>
                    <span className="voice-picker-trigger-name">
                      {voicebanksLoading ? "Loading voices..." : selectedVoiceLabel}
                    </span>
                  </span>
                  <ChevronsUpDown size={16} aria-hidden="true" />
                </button>
              </div>
              <div className="solfege-picker" ref={solfegePickerRef}>
                {showSolfegeHint && !solfegeMenuOpen ? (
                  <div className="solfege-feature-hint" role="note">
                    <span>Change the solfege system here</span>
                    <button
                      type="button"
                      aria-label="Dismiss solfege settings hint"
                      onClick={() => {
                        window.localStorage.setItem(SOLFEGE_GUIDE_DISMISSED_KEY, "true");
                        setShowSolfegeHint(false);
                      }}
                    >
                      <X size={13} aria-hidden="true" />
                    </button>
                  </div>
                ) : null}
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
                  className={clsx("solfege-picker-trigger", { open: solfegeMenuOpen })}
                  aria-haspopup="dialog"
                  aria-expanded={solfegeMenuOpen}
                  disabled={solfegeSettingsSaving}
                  onClick={() => {
                    setShowSolfegeHint(false);
                    if (!solfegeMenuOpen) {
                      setDraftSolfegeSystem(solfegeSystem);
                      setDraftSolfegeMode(solfegeMode);
                    }
                    setSolfegeMenuOpen((open) => !open);
                    setVoiceMenuOpen(false);
                  }}
                >
                  <span className="solfege-picker-trigger-icon" aria-hidden="true">
                    <Music2 size={16} />
                  </span>
                  <span className="solfege-picker-trigger-copy">
                    <span className="solfege-picker-trigger-label">Solfege</span>
                    <span className="solfege-picker-trigger-name">
                      {solfegeSystemLabel}
                      {solfegeSystem === "movable_do" ? ` · ${solfegeModeLabel}` : ""}
                    </span>
                  </span>
                  <ChevronsUpDown size={16} aria-hidden="true" />
                </button>
              </div>
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
            vocalTracks={multiTrackAudioTracks}
            instrumentalTracks={instrumentalTracks}
            playbackRequestId={scorePlayerPlaybackRequestId}
            onControlsChange={handleScorePlayerControlsChange}
            onEngineLoading={handleScorePlayerEngineLoading}
            onEngineReady={handleScorePlayerEngineReady}
            onPlaybackStateChange={handleScorePlayerPlaybackStateChange}
            onError={handleScorePlayerEngineError}
          />

          <section
            className={clsx("score-panel", isDragging && "drag-active")}
            data-testid="score-preview"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
          <div className="score-header">
            <h2>Score Preview</h2>
              <div className="score-controls">
                <div className="score-subtitles">
                  <span className="chat-subtitle">
                    Latest upload only {audioUrl ? "· Audio ready" : ""}
                </span>
                {estimatedDurationLabel && (
                  <span className="score-estimate">{estimatedDurationLabel}</span>
                )}
                {estimatedCostLabel && (
                  <span className="score-estimate">{estimatedCostLabel}</span>
                )}
              </div>
              <div className="score-expansion-control">
                <label htmlFor="expand-repeats-toggle">Expand Repeats</label>
                <input
                  id="expand-repeats-toggle"
                  type="checkbox"
                  role="switch"
                  checked={expandRepeats}
                  disabled={!score}
                  onChange={(event) => setExpandRepeats(event.target.checked)}
                />
              </div>
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
              <div className="score-player-transport" aria-label="Score player transport">
                <button
                  type="button"
                  className="score-action-button"
                  onClick={handleMultiTrackExport}
                  disabled={
                    !multiTrackAudioTracks.length ||
                    creditsLocked ||
                    exportMixRequiredCredits === null ||
                    multiTrackExportProgress !== null
                  }
                  aria-label="Export vocal mix"
                  title={
                    exportMixRequiredCredits === null
                      ? "Export vocal mix"
                      : `Export vocal mix · ${exportMixRequiredCredits} credits`
                  }
                >
                  {multiTrackExportPercent ?? <Upload size={16} />}
                </button>
                <button
                  type="button"
                  className="score-action-button"
                  onClick={multiTrackPlaying ? handleMultiTrackPause : handleMultiTrackPlay}
                  disabled={!hasScorePlayerTracks || !scorePlayerControls}
                  aria-label={multiTrackPlaying ? "Pause score player" : "Play score player"}
                  title={multiTrackPlaying ? "Pause" : "Play"}
                >
                  {multiTrackPlaying ? <Pause size={16} /> : <Play size={16} />}
                </button>
                <button
                  type="button"
                  className="score-action-button"
                  onClick={handleMultiTrackStop}
                  disabled={!hasScorePlayerTracks || !scorePlayerControls}
                  aria-label="Stop score player"
                  title="Stop"
                >
                  <Square size={15} />
                </button>
              </div>
              <div className="score-action-controls" aria-label="Score export controls">
                <div className="score-layout-toggle" role="group" aria-label="Score preview layout">
                  <button
                    type="button"
                    className={clsx("score-layout-option", { selected: scorePreviewLayout === "page" })}
                    aria-pressed={scorePreviewLayout === "page"}
                    disabled={!score}
                    onClick={() => setScorePreviewLayout("page")}
                  >
                    Page
                  </button>
                  <button
                    type="button"
                    className={clsx("score-layout-option", { selected: scorePreviewLayout === "horizontal" })}
                    aria-pressed={scorePreviewLayout === "horizontal"}
                    disabled={!score}
                    onClick={() => setScorePreviewLayout("horizontal")}
                  >
                    Horizontal
                  </button>
                </div>
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
          <div className="score-body">
            {hasScorePlayerTracks && (
              <aside className="score-tracks-sidebar" aria-label="Score multitrack controls sidebar">
                <ScoreTrackControlsPanel
                  vocalTracks={multiTrackAudioTracks}
                  instrumentalTracks={instrumentalTracks}
                  onUpdateVocalMute={updateMultiTrackMute}
                  onUpdateVocalSolo={updateMultiTrackSolo}
                  onUpdateVocalVolume={updateMultiTrackVolume}
                  onDownloadVocalTrack={handleMultiTrackTrackDownload}
                  onUpdateInstrumentalMute={updateInstrumentalTrackMute}
                  onUpdateInstrumentalSolo={updateInstrumentalTrackSolo}
                  onUpdateInstrumentalVolume={updateInstrumentalTrackVolume}
                  onUpdateInstrumentalGmProgram={updateInstrumentalTrackGmProgram}
                />
              </aside>
            )}
            <div className={clsx("score-canvas", { "horizontal-layout": scorePreviewLayout === "horizontal" })}>
              <div ref={scoreRef} className="score-surface" data-testid="score-preview-surface" />
              {scorePreviewError ? (
                <div className="score-placeholder score-error-placeholder">
                  <p>{scorePreviewError}</p>
                </div>
              ) : !score ? (
                <div className="score-placeholder">
                  <p>Upload a MusicXML file to render the score here.</p>
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
      {showAnnouncement && currentAnnouncement && (
        <AnnouncementModal 
          announcement={currentAnnouncement} 
          onClose={() => markAsSeen(currentAnnouncement.id)} 
        />
      )}
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
