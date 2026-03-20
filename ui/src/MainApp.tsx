import { useEffect, useMemo, useRef, useState, useCallback, type CSSProperties } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { UploadCloud, Send, Minus, Plus, Download, Maximize2, X, Play, Pause, Volume2, VolumeX, Mic, Disc, RotateCcw, Replace } from "lucide-react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import WaveSurfer from "wavesurfer.js";
import {
  chat,
  createSession,
  fetchScoreXml,
  fetchProgress,
  fetchAudioBlob,
  uploadScore,
  type ProgressResponse,
  type ScoreSummary,
} from "./api";
import { UserMenu } from "./components/UserMenu";
import { useCredits } from "./hooks/useCredits";
import { WaitlistModal } from "./components/WaitlistModal";
import type { WaitlistSource } from "./components/WaitingListForm";

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
  details?: unknown;
  attemptMessages?: AttemptMessage[];
  showSelector?: boolean;
  progressUrl?: string;
  isProgress?: boolean;
  progressValue?: number;
  jobId?: string;
  jobKind?: string;
  suggestions?: QuickSuggestion[];
};

type AttemptMessage = {
  attempt_number: number;
  message?: string;
  thought_summary?: string;
};

type TrackState = {
  muted: boolean;
  solo: boolean;
  recording: boolean;
  volume: number;
};

type QuickSuggestion = {
  label: string;
  prompt: string;
};

type ScorePayload = {
  name: string;
  data: string;
};

type PartOption = {
  key: string;
  label: string;
  part_id?: string;
  part_name?: string;
  part_index: number;
  has_lyrics?: boolean;
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

const shouldPromptSelection = (summary: ScoreSummary | null): boolean => {
  const parts = buildPartOptions(summary);
  const verses = buildVerseOptions(summary);
  return (parts.length > 1 || verses.length > 1) && parts.length > 0 && verses.length > 0;
};

export default function MainApp() {
  const navigate = useNavigate();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [score, setScore] = useState<ScorePayload | null>(null);
  const [scoreSummary, setScoreSummary] = useState<ScoreSummary | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [scoreReady, setScoreReady] = useState(false);
  const [selectedPartKey, setSelectedPartKey] = useState<string | null>(null);
  const [selectedVerse, setSelectedVerse] = useState<string | null>(null);
  const [pendingSelection, setPendingSelection] = useState(false);
  const [selectorShown, setSelectorShown] = useState(false);
  const [showWaitlistModal, setShowWaitlistModal] = useState(false);
  const [waitlistSource, setWaitlistSource] = useState<WaitlistSource>("studio_menu");
  const [showCreditsModal, setShowCreditsModal] = useState(false);
  const [showTrialExpiredModal, setShowTrialExpiredModal] = useState(false);
  const [expandedThoughts, setExpandedThoughts] = useState<Record<string, boolean>>({});
  const [uploadStep, setUploadStep] = useState<'idle' | 'uploading' | 'parsing' | 'analyzing' | 'ready'>('idle');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [activeProgress, setActiveProgress] = useState<{
    messageId: string;
    url: string;
    jobId?: string;
  } | null>(null);
  const [showScoreLarge, setShowScoreLarge] = useState(false);
  const [scoreLargeZoom, setScoreLargeZoom] = useState(1);
  // Audio tracks state - support for both vocal and backing track
  const [vocalUrl, setVocalUrl] = useState<string | null>(null);
  const [backingUrl, setBackingUrl] = useState<string | null>(null);
  const [vocalDownloadUrl, setVocalDownloadUrl] = useState<string | null>(null);
  const [backingDownloadUrl, setBackingDownloadUrl] = useState<string | null>(null);
  const [backingReplacementCount, setBackingReplacementCount] = useState(0);
  
  // Track states for both audio tracks
  const [vocalTrackState, setVocalTrackState] = useState<TrackState>({
    muted: false,
    solo: false,
    recording: false,
    volume: 80,
  });
  const [backingTrackState, setBackingTrackState] = useState<TrackState>({
    muted: false,
    solo: false,
    recording: false,
    volume: 70,
  });
  
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const audioRefreshPromisesRef = useRef<Record<string, Promise<string | null> | undefined>>({});
  const audioObjectUrlsRef = useRef<{ vocal: string | null; backing: string | null }>({
    vocal: null,
    backing: null,
  });

  const loadTrackAudio = useCallback(
    async (sourceUrl: string, track: "vocal" | "backing") => {
      if (!sourceUrl) return;
      try {
        const blob = await fetchAudioBlob(sourceUrl);
        const objectUrl = URL.createObjectURL(blob);
        if (track === "vocal") {
          if (audioObjectUrlsRef.current.vocal) {
            URL.revokeObjectURL(audioObjectUrlsRef.current.vocal);
          }
          audioObjectUrlsRef.current.vocal = objectUrl;
          setVocalUrl(objectUrl);
          setVocalDownloadUrl(sourceUrl);
        } else {
          if (audioObjectUrlsRef.current.backing) {
            URL.revokeObjectURL(audioObjectUrlsRef.current.backing);
          }
          audioObjectUrlsRef.current.backing = objectUrl;
          setBackingUrl(objectUrl);
          setBackingDownloadUrl(sourceUrl);
        }
      } catch (err: any) {
        console.error(`Failed to load ${track} track`, err);
        setError(err?.message || `Failed to load ${track} track audio.`);
      }
    },
    [setError]
  );

  useEffect(() => {
    return () => {
      if (audioObjectUrlsRef.current.vocal) {
        URL.revokeObjectURL(audioObjectUrlsRef.current.vocal);
      }
      if (audioObjectUrlsRef.current.backing) {
        URL.revokeObjectURL(audioObjectUrlsRef.current.backing);
      }
    };
  }, []);

  const clearLoadedTracks = useCallback(() => {
    vocalWavesurferRef.current?.pause();
    backingWavesurferRef.current?.pause();
    if (audioObjectUrlsRef.current.vocal) {
      URL.revokeObjectURL(audioObjectUrlsRef.current.vocal);
      audioObjectUrlsRef.current.vocal = null;
    }
    if (audioObjectUrlsRef.current.backing) {
      URL.revokeObjectURL(audioObjectUrlsRef.current.backing);
      audioObjectUrlsRef.current.backing = null;
    }
    setVocalUrl(null);
    setBackingUrl(null);
    setVocalDownloadUrl(null);
    setBackingDownloadUrl(null);
    setBackingReplacementCount(0);
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
  }, []);
  
  // Two waveform refs for vocal and backing tracks
  const vocalWaveformRef = useRef<HTMLDivElement | null>(null);
  const backingWaveformRef = useRef<HTMLDivElement | null>(null);
  const vocalWavesurferRef = useRef<WaveSurfer | null>(null);
  const backingWavesurferRef = useRef<WaveSurfer | null>(null);

  const {
    available,
    overdrafted,
    isExpired,
    loading: creditsLoading,
  } = useCredits();
  const appEnv = (import.meta.env.VITE_APP_ENV ?? "").toString().toLowerCase();
  const isDevAppEnv = ["dev", "development", "local", "test", "preview"].includes(appEnv);
  const creditsLocked = !isDevAppEnv && !creditsLoading && (overdrafted || isExpired || available <= 0);
  const estimatedDuration = scoreSummary?.duration_seconds;
  const estimatedDurationLabel =
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? `Estimated duration: ${formatDuration(estimatedDuration)}`
      : null;

  const layoutRef = useRef<HTMLDivElement | null>(null);
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);

  useEffect(() => {
    let alive = true;
    createSession()
      .then((data) => {
        if (alive) {
          setSessionId(data.session_id);
        }
      })
      .catch((err) => {
        if (alive) {
          setError(err.message || "Failed to create session.");
        }
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (creditsLoading) return;
    if (isExpired && !showTrialExpiredModal) {
      setShowTrialExpiredModal(true);
      setWaitlistSource("trial_expired");
      setShowWaitlistModal(true);
      return;
    }
    if ((available <= 0 || overdrafted) && !isExpired && !showCreditsModal) {
      setShowCreditsModal(true);
      setWaitlistSource("credits_exhausted");
      setShowWaitlistModal(true);
    }
  }, [available, creditsLoading, isExpired, overdrafted, showCreditsModal, showTrialExpiredModal]);

  useEffect(() => {
    if (!score?.data || !scoreRef.current) return;
    setScoreReady(false);
    
    // Validate XML before passing to OSMD
    const trimmedData = score.data.trim();
    console.log("Attempting to render MusicXML, size:", score.data.length, "bytes");
    console.log("First 300 chars:", trimmedData.substring(0, 300));
    
    if (!trimmedData.startsWith('<')) {
      setError("Invalid MusicXML: data does not start with XML tag. Check console for details.");
      return;
    }
    
    const osmd = new OpenSheetMusicDisplay(scoreRef.current, {
      autoResize: true,
      drawTitle: true,
      followCursor: false,
      renderSingleHorizontalStaffline: false,
    });
    osmdRef.current = osmd;
    osmd
      .load(score.data)
      .then(() => {
        osmd.zoom = zoomLevel;
        osmd.render();
        setScoreReady(true);
      })
      .catch((err) => {
        console.error("OSMD render error:", err);
        console.error("Score data preview (first 500 chars):", score.data?.substring(0, 500));
        setError(err?.message || "Failed to render the score. The MusicXML file may be invalid or corrupted.");
      });
  }, [score]);

  useEffect(() => {
    if (!scoreReady || !osmdRef.current) return;
    osmdRef.current.zoom = zoomLevel;
    osmdRef.current.render();
  }, [zoomLevel, scoreReady]);

  // Large score modal rendering
  const scoreLargeRef = useRef<HTMLDivElement | null>(null);
  const osmdLargeRef = useRef<OpenSheetMusicDisplay | null>(null);

  useEffect(() => {
    if (!showScoreLarge || !score?.data || !scoreLargeRef.current) return;
    
    const osmd = new OpenSheetMusicDisplay(scoreLargeRef.current, {
      autoResize: true,
      drawTitle: true,
      followCursor: false,
      renderSingleHorizontalStaffline: false,
    });
    osmdLargeRef.current = osmd;
    osmd
      .load(score.data)
      .then(() => {
        osmd.zoom = scoreLargeZoom;
        osmd.render();
      })
      .catch((err) => {
        console.error("OSMD large render error:", err);
      });
  }, [showScoreLarge, score, scoreLargeZoom]);

  useEffect(() => {
    if (!osmdLargeRef.current) return;
    osmdLargeRef.current.zoom = scoreLargeZoom;
    osmdLargeRef.current.render();
  }, [scoreLargeZoom]);

  // Initialize vocal track WaveSurfer
  useEffect(() => {
    if (!vocalUrl || !vocalWaveformRef.current) return;

    vocalWaveformRef.current.innerHTML = "";

    const ws = WaveSurfer.create({
      container: vocalWaveformRef.current,
      waveColor: '#60a5fa',
      progressColor: '#3b82f6',
      cursorColor: 'rgba(239, 68, 68, 1)',
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      height: 80,
      normalize: false,
      minPxPerSec: 50,
      fillParent: true,
      autoplay: false,
      backend: 'MediaElement',
      cursorWidth: 2,
    });
    
    ws.load(vocalUrl);
    vocalWavesurferRef.current = ws;
    ws.setVolume(vocalTrackState.volume / 100);
    ws.setMuted(vocalTrackState.muted);
    
    ws.on('ready', () => {
      setDuration(ws.getDuration());
    });
    
    ws.on('play', () => setIsPlaying(true));
    ws.on('pause', () => setIsPlaying(false));
    ws.on('finish', () => setIsPlaying(false));
    ws.on('audioprocess', () => setCurrentTime(ws.getCurrentTime()));
    
    return () => {
      ws.destroy();
      vocalWavesurferRef.current = null;
    };
  }, [vocalUrl]);

  // Initialize backing track WaveSurfer  
  useEffect(() => {
    if (!backingUrl || !backingWaveformRef.current) return;

    backingWaveformRef.current.innerHTML = "";

    const ws = WaveSurfer.create({
      container: backingWaveformRef.current,
      waveColor: '#4ade80',
      progressColor: '#22c55e',
      cursorColor: 'rgba(239, 68, 68, 1)',
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      height: 80,
      normalize: false,
      minPxPerSec: 50,
      fillParent: true,
      autoplay: false,
      backend: 'MediaElement',
      cursorWidth: 2,
    });
    
    ws.load(backingUrl);
    backingWavesurferRef.current = ws;
    ws.setVolume(backingTrackState.volume / 100);
    ws.setMuted(backingTrackState.muted);
    
    ws.on('ready', () => {
      // Sync duration if backing track is longer
      const backingDuration = ws.getDuration();
      setDuration(prev => Math.max(prev, backingDuration));
    });
    
    ws.on('play', () => setIsPlaying(true));
    ws.on('pause', () => setIsPlaying(false));
    ws.on('finish', () => setIsPlaying(false));
    
    return () => {
      ws.destroy();
      backingWavesurferRef.current = null;
    };
  }, [backingUrl]);

  // Update volumes when track states change
  useEffect(() => {
    vocalWavesurferRef.current?.setVolume(vocalTrackState.volume / 100);
    vocalWavesurferRef.current?.setMuted(vocalTrackState.muted);
  }, [vocalTrackState.volume, vocalTrackState.muted]);

  useEffect(() => {
    backingWavesurferRef.current?.setVolume(backingTrackState.volume / 100);
    backingWavesurferRef.current?.setMuted(backingTrackState.muted);
  }, [backingTrackState.volume, backingTrackState.muted]);

  // Whenever a new backing track arrives, rewind both tracks so the next play starts in sync
  useEffect(() => {
    if (!backingUrl) return;
    const vocal = vocalWavesurferRef.current;
    const backing = backingWavesurferRef.current;
    vocal?.pause();
    backing?.pause();
    vocal?.seekTo(0);
    backing?.seekTo(0);
    setIsPlaying(false);
    setCurrentTime(0);
  }, [backingUrl]);

  // Sync playback between both tracks
  const togglePlay = useCallback(() => {
    const vocal = vocalWavesurferRef.current;
    const backing = backingWavesurferRef.current;
    
    if (!vocal && !backing) return;
    
    const isVocalPlaying = vocal?.isPlaying?.() ?? false;
    const isBackingPlaying = backing?.isPlaying?.() ?? false;
    const anyPlaying = isVocalPlaying || isBackingPlaying;
    
    if (anyPlaying) {
      vocal?.pause();
      backing?.pause();
      setIsPlaying(false);
    } else {
      // Get current positions
      const vocalTime = vocal?.getCurrentTime?.() ?? 0;
      const backingTime = backing?.getCurrentTime?.() ?? 0;
      const vocalDuration = vocal?.getDuration?.() ?? 0;
      const backingDuration = backing?.getDuration?.() ?? 0;
      
      // Check if both tracks have finished - if so, reset to start
      const vocalFinished = vocalDuration > 0 && vocalTime >= vocalDuration - 0.1;
      const backingFinished = backingDuration > 0 && backingTime >= backingDuration - 0.1;
      
      let targetTime = 0;
      if (vocalFinished && backingFinished) {
        // Both finished, restart from beginning
        targetTime = 0;
      } else {
        // Sync to the furthest position
        targetTime = Math.max(vocalTime, backingTime);
      }
      
      if (vocal && backing) {
        // Ensure both are at the same position
        if (vocalDuration > 0) {
          vocal.seekTo(targetTime / vocalDuration);
        }
        if (backingDuration > 0) {
          backing.seekTo(targetTime / backingDuration);
        }
        
        // Play both simultaneously
        Promise.all([
          vocal.play(),
          backing.play()
        ]).catch(err => {
          console.error('Playback error:', err);
          setIsPlaying(false);
        });
      } else if (vocal) {
        if (vocalDuration > 0 && vocalFinished) {
          vocal.seekTo(0);
        }
        vocal.play().catch(err => {
          console.error('Vocal playback error:', err);
          setIsPlaying(false);
        });
      } else if (backing) {
        if (backingDuration > 0 && backingFinished) {
          backing.seekTo(0);
        }
        backing.play().catch(err => {
          console.error('Backing playback error:', err);
          setIsPlaying(false);
        });
      }
      setIsPlaying(true);
    }
  }, []);

  const toggleVocalMute = () => {
    setVocalTrackState(prev => ({ ...prev, muted: !prev.muted }));
  };

  const toggleBackingMute = () => {
    setBackingTrackState(prev => ({ ...prev, muted: !prev.muted }));
  };

  const toggleVocalSolo = () => {
    setVocalTrackState(prev => {
      const newSolo = !prev.solo;
      // If soloing vocal, unmute vocal and mute backing
      if (newSolo) {
        setBackingTrackState(b => ({ ...b, muted: true }));
        return { ...prev, solo: true, muted: false };
      } else {
        // If unsoloing, restore backing track
        setBackingTrackState(b => ({ ...b, muted: false }));
        return { ...prev, solo: false };
      }
    });
  };

  const toggleBackingSolo = () => {
    setBackingTrackState(prev => {
      const newSolo = !prev.solo;
      // If soloing backing, unmute backing and mute vocal
      if (newSolo) {
        setVocalTrackState(v => ({ ...v, muted: true }));
        return { ...prev, solo: true, muted: false };
      } else {
        // If unsoloing, restore vocal track
        setVocalTrackState(v => ({ ...v, muted: false }));
        return { ...prev, solo: false };
      }
    });
  };

  const handleVocalVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const volume = parseInt(e.target.value, 10);
    setVocalTrackState(prev => ({ ...prev, volume }));
  };

  const handleBackingVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const volume = parseInt(e.target.value, 10);
    setBackingTrackState(prev => ({ ...prev, volume }));
  };

  const resetToBeginning = useCallback(() => {
    const vocal = vocalWavesurferRef.current;
    const backing = backingWavesurferRef.current;
    
    vocal?.pause();
    backing?.pause();
    vocal?.seekTo(0);
    backing?.seekTo(0);
    setIsPlaying(false);
    setCurrentTime(0);
  }, []);

  const formatTime = (time: number): string => {
    const minutes = Math.floor(time / 60);
    const seconds = Math.floor(time % 60);
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
  };

  // Poll for progress updates
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
      const nextAttemptMessages = extractAttemptMessages(payload.details);
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== activeProgress.messageId) return msg;
          const nextContent =
            payload.job_kind === "preprocess" && payload.status === "running"
              ? msg.content
              : appendTerminalPreprocessMessage
                ? appendPreprocessTerminalMessage(msg.content, nextMessage)
                : appendProgressMessage(msg.content, nextMessage);
          return {
            ...msg,
            content: nextContent,
            details: payload.details ?? msg.details,
            attemptMessages: nextAttemptMessages ?? msg.attemptMessages,
            progressValue: typeof nextProgress === "number" ? nextProgress : msg.progressValue,
            audioUrl: nextAudioUrl || msg.audioUrl,
            isProgress: payload.status !== "done" && payload.status !== "error",
            jobKind: payload.job_kind || msg.jobKind,
          };
        })
      );
      if (nextAudioUrl) {
        // Route audio to correct track based on job_kind
        if (payload.job_kind === "backing_track") {
          void loadTrackAudio(nextAudioUrl, "backing");
        } else {
          // Default to vocal track for singing audio
          void loadTrackAudio(nextAudioUrl, "vocal");
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
        }
        if (payload.status === "error") {
          setActiveProgress(null);
          setError(
            payload.message ||
              (payload.job_kind === "preprocess"
                ? "Preprocess failed."
                : "Synthesis failed.")
          );
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err?.message || "Failed to fetch synthesis progress.");
          setActiveProgress(null);
        }
      }
    };

    poll();
    const interval = window.setInterval(poll, 1200);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [activeProgress]);

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

  const headerSubtitle = useMemo(
    () =>
      "Zero-shot sight-singing from MusicXML. Tell the singer how to perform.",
    []
  );

  const partOptions = useMemo(() => buildPartOptions(scoreSummary), [scoreSummary]);
  const verseOptions = useMemo(() => buildVerseOptions(scoreSummary), [scoreSummary]);

  const appendMessage = (message: Message) => {
    setMessages((prev) => [...prev, message]);
  };

  const toggleThoughtSummary = (messageId: string) => {
    setExpandedThoughts((prev) => ({
      ...prev,
      [messageId]: !prev[messageId],
    }));
  };

  const refreshScorePreview = async () => {
    if (!sessionId || !score) return;
    const data = await fetchScoreXml(sessionId);
    setScore({ name: score.name, data });
  };

  const refreshMessageAudioUrl = async (
    messageId: string,
    progressUrl?: string
  ): Promise<string | null> => {
    if (!progressUrl) return null;
    const pending = audioRefreshPromisesRef.current[messageId];
    if (pending) {
      return pending;
    }
    const refreshPromise = (async () => {
      const payload = await fetchProgress(progressUrl);
      const nextAudioUrl = payload.audio_url;
      if (!nextAudioUrl) return null;
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === messageId ? { ...msg, audioUrl: nextAudioUrl, progressUrl } : msg
        )
      );
      if (payload.job_kind === "backing_track") {
        void loadTrackAudio(nextAudioUrl, "backing");
      } else {
        void loadTrackAudio(nextAudioUrl, "vocal");
      }
      return nextAudioUrl;
    })();
    audioRefreshPromisesRef.current[messageId] = refreshPromise;
    try {
      return await refreshPromise;
    } finally {
      delete audioRefreshPromisesRef.current[messageId];
    }
  };

  const handleAudioPlaybackError = async (messageId: string, progressUrl?: string) => {
    try {
      const nextAudioUrl = await refreshMessageAudioUrl(messageId, progressUrl);
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

  const handleAudioDownload = async (
    messageId: string,
    audioUrl?: string,
    progressUrl?: string
  ) => {
    try {
      const nextAudioUrl = (await refreshMessageAudioUrl(messageId, progressUrl)) || audioUrl;
      if (!nextAudioUrl) {
        setError("No audio available to download.");
        return;
      }
      const link = document.createElement("a");
      link.href = nextAudioUrl;
      link.download = "";
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } catch (err: any) {
      setError(err?.message || "Failed to refresh audio download.");
    }
  };

  const handleUpload = async (file: File) => {
    if (!sessionId || creditsLocked) return;
    
    const validExtensions = ['.xml', '.mxl', '.musicxml'];
    const fileName = file.name.toLowerCase();
    const isValidType = validExtensions.some(ext => fileName.endsWith(ext));
    
    if (!isValidType) {
      setError("Invalid file type. Please upload a MusicXML file (.xml, .mxl, or .musicxml).");
      return;
    }
    
    setUploading(true);
    clearLoadedTracks();
    setUploadStep('uploading');
    setUploadProgress(0);
    setError(null);
    
    // Simulate progress for better UX
    const progressInterval = setInterval(() => {
      setUploadProgress(prev => Math.min(prev + 5, 90));
    }, 200);
    
    try {
      const uploadResponse = await uploadScore(sessionId, file);
      setUploadStep('parsing');
      setUploadProgress(95);
      
      const summary = uploadResponse.score_summary ?? null;
      setScoreSummary(summary);
      setPendingSelection(shouldPromptSelection(summary));
      setSelectorShown(false);
      
      const nextPartOptions = buildPartOptions(summary);
      const nextVerseOptions = buildVerseOptions(summary);
      setSelectedPartKey(nextPartOptions[0]?.key ?? null);
      setSelectedVerse(nextVerseOptions[0] ?? null);
      
      setUploadStep('analyzing');
      const data = await fetchScoreXml(sessionId);
      
      if (!data || !data.trim()) {
        throw new Error("Empty response received from server.");
      }
      
      const trimmedData = data.trim();
      if (!trimmedData.startsWith('<')) {
        throw new Error("Invalid MusicXML data received from server.");
      }
      
      setUploadStep('ready');
      setUploadProgress(100);
      setScore({ name: file.name, data });
      
      // Add a welcome message
      const quickSuggestions: QuickSuggestion[] = [
        { label: "Natural", prompt: "Sing this naturally" },
        { label: "Softly", prompt: "Sing this softly" },
        { label: "Energetic", prompt: "Give me a strong, energetic version" },
      ];

      appendMessage({
        id: crypto.randomUUID(),
        role: 'assistant',
        content: `Score loaded. How would you like me to sing it?`,
        suggestions: quickSuggestions,
      });
      
    } catch (err: any) {
      console.error("Upload/render error:", err);
      setError(err?.message || "Upload failed.");
      setScore(null);
      setScoreSummary(null);
      setUploadStep('idle');
    } finally {
      clearInterval(progressInterval);
      setUploading(false);
    }
  };

  const sendMessage = async (content: string) => {
    if (!content.trim() || !sessionId || creditsLocked) return;
    setStatus("Thinking...");
    setError(null);
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      content,
    });

    try {
      const response = await chat(sessionId, content);
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
        // Route based on job_kind from message or default to vocal
        if (assistantMessage.jobKind === "backing_track") {
          void loadTrackAudio(response.audio_url, "backing");
        } else {
          void loadTrackAudio(response.audio_url, "vocal");
        }
        if (pendingSelection) {
          setPendingSelection(false);
        }
      }
      if (response.type === "chat_progress") {
        assistantMessage.progressUrl = response.progress_url;
        assistantMessage.jobId = response.job_id;
        assistantMessage.isProgress = true;
        if (pendingSelection) {
          setPendingSelection(false);
        }
      }
      if ("current_score" in response && response.current_score) {
        await refreshScorePreview();
      }
      if ("warning" in response && response.warning) {
        setError(String(response.warning));
      }
      appendMessage(assistantMessage);
      if (response.type === "chat_progress") {
        setActiveProgress({
          messageId: assistantMessage.id,
          url: response.progress_url,
          jobId: response.job_id,
        });
      }
    } catch (err: any) {
      setError(err?.message || "Failed to send message.");
    } finally {
      setStatus(null);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || !sessionId || creditsLocked) return;
    const content = input.trim();
    setInput("");
    await sendMessage(content);
  };

  const handleSelectionSend = async () => {
    if (!selectedPartKey || !selectedVerse || creditsLocked) return;
    const selected = partOptions.find((option) => option.key === selectedPartKey);
    if (!selected) return;
    const partDescriptor = selected.part_name
      ? `the ${selected.part_name} part`
      : selected.part_id
        ? `part ${selected.part_id}`
        : `part ${selected.part_index + 1}`;
    const message = `Please sing ${partDescriptor}, verse ${selectedVerse}.`;
    setPendingSelection(false);
    await sendMessage(message);
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
    if (creditsLocked) return;
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) handleUpload(file);
  };

  const handleJoinWaitlist = (source: WaitlistSource) => {
    setWaitlistSource(source);
    setShowWaitlistModal(true);
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
          <img src="/logo-hackaton.png" alt="SightSinger logo" className="brand-icon" />
        </div>
        <div className="header-actions">
          <UserMenu onJoinWaitlist={() => handleJoinWaitlist("studio_menu")} />
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}
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

      <main className={clsx("studio-layout", !score && "chat-only")}>
        {/* Left Panel - Chat */}
        <section
          className={clsx("chat-panel-left", isDragging && "drag-active")}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="chat-stream" ref={chatStreamRef} onScroll={handleChatScroll}>
            {messages.length === 0 && !uploading && uploadStep === 'idle' && (
              <div className="empty-state-landing">
                <div className="landing-content">
                  <h1 className="landing-title">SightSinger</h1>
                  <p className="landing-subtitle">Drop me the score. Say a few words. I'll sing it for you.</p>
                  <div className="suggestion-chips">
                    <button className="suggestion-chip" onClick={() => sendMessage("Sing this with warmth and emotion")}>Sing with warmth</button>
                    <button className="suggestion-chip" onClick={() => sendMessage("Give me a fast, energetic version")}>Fast & energetic</button>
                    <button className="suggestion-chip" onClick={() => sendMessage("Make it sound like a lullaby")}>Like a lullaby</button>
                    <button className="suggestion-chip" onClick={() => sendMessage("Sing with gospel choir energy")}>Gospel energy</button>
                    <button className="suggestion-chip" onClick={() => sendMessage("Whispered, intimate performance")}>Whispered & intimate</button>
                  </div>
                </div>
              </div>
            )}
            {uploading && (
              <div className="upload-progress">
                <div className="upload-step">
                  <div className={`step-indicator ${uploadStep === 'uploading' ? 'active' : uploadStep !== 'idle' ? 'complete' : ''}`}>
                    {uploadStep !== 'uploading' && uploadStep !== 'idle' ? '✓' : '1'}
                  </div>
                  <span className={uploadStep === 'uploading' ? 'active' : ''}>Uploading file</span>
                  {uploadStep === 'uploading' && <div className="step-progress-bar"><div className="step-progress-fill" style={{ width: `${uploadProgress}%` }} /></div>}
                </div>
                <div className="upload-step">
                  <div className={`step-indicator ${uploadStep === 'parsing' ? 'active' : uploadStep === 'analyzing' || uploadStep === 'ready' ? 'complete' : ''}`}>
                    {uploadStep === 'analyzing' || uploadStep === 'ready' ? '✓' : '2'}
                  </div>
                  <span className={uploadStep === 'parsing' ? 'active' : ''}>Parsing score</span>
                  {uploadStep === 'parsing' && <div className="step-spinner" />}
                </div>
                <div className="upload-step">
                  <div className={`step-indicator ${uploadStep === 'analyzing' ? 'active' : uploadStep === 'ready' ? 'complete' : ''}`}>
                    {uploadStep === 'ready' ? '✓' : '3'}
                  </div>
                  <span className={uploadStep === 'analyzing' ? 'active' : ''}>Analyzing structure</span>
                  {uploadStep === 'analyzing' && <div className="step-spinner" />}
                </div>
                <div className="upload-step">
                  <div className={`step-indicator ${uploadStep === 'ready' ? 'active' : ''}`}>4</div>
                  <span className={uploadStep === 'ready' ? 'active' : ''}>Ready to sing</span>
                </div>
              </div>
            )}
            
            {messages.map((msg, index) => {
              const isLatestMessage = index === messages.length - 1;
              const styleOptionsUnlocked = backingReplacementCount >= 1;
              return (
              <div
                key={msg.id}
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
                    const shouldHideProgressText = Boolean(msg.isProgress && !msg.audioUrl);
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
                          const attemptExpanded = Boolean(expandedThoughts[attemptKey]);
                          const attemptMessage = attempt.message?.trim() ?? "";
                          const attemptThought = attempt.thought_summary?.trim() ?? "";
                          return (
                            <div key={attemptKey} className="attempt-block">
                              <div className="attempt-label">Attempt {attempt.attempt_number}</div>
                              {attemptMessage ? (
                                <ReactMarkdown className="chat-markdown" remarkPlugins={[remarkGfm]}>
                                  {attemptMessage}
                                </ReactMarkdown>
                              ) : null}
                              {attemptThought ? (
                                <div className="thought-summary">
                                  <button
                                    type="button"
                                    className="thought-summary-toggle"
                                    onClick={() => toggleThoughtSummary(attemptKey)}
                                    aria-expanded={attemptExpanded}
                                  >
                                    <span
                                      className={clsx(
                                        "thought-summary-caret",
                                        attemptExpanded && "expanded"
                                      )}
                                      aria-hidden="true"
                                    >
                                      ▾
                                    </span>
                                    <span>Thought summary</span>
                                  </button>
                                  {attemptExpanded ? (
                                    <ReactMarkdown
                                      className="chat-markdown thought-summary-content"
                                      remarkPlugins={[remarkGfm]}
                                    >
                                      {attemptThought}
                                    </ReactMarkdown>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          );
                        })}
                        {!shouldHideProgressText && trailingContent ? (
                          <ReactMarkdown className="chat-markdown" remarkPlugins={[remarkGfm]}>
                            {trailingContent}
                          </ReactMarkdown>
                        ) : null}
                      </>
                    );
                  })()
                ) : (
                  !msg.isProgress ? <p>{msg.content}</p> : null
                )}
                {msg.suggestions && msg.suggestions.length > 0 && (
                  <div className="quick-suggestion-row">
                    {msg.suggestions.map((option) => (
                      <button
                        key={`${msg.id}-${option.label}`}
                        type="button"
                        className="quick-suggestion-btn"
                        onClick={() => void sendMessage(option.prompt)}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                )}
                {msg.isProgress && !msg.audioUrl && (
                  <div className="generation-progress">
                    <div className="generation-progress-bar">
                      <div 
                        className="generation-progress-fill" 
                        style={{ width: `${msg.progressValue ? Math.max(10, Math.min(95, msg.progressValue * 100)) : 15}%` }}
                      />
                    </div>
                    <div className="generation-steps">
                      <span className={msg.progressValue && msg.progressValue > 0.2 ? 'complete' : 'active'}>Understanding</span>
                      <span className={msg.progressValue && msg.progressValue > 0.5 ? 'complete' : msg.progressValue && msg.progressValue > 0.2 ? 'active' : ''}>Composing</span>
                      <span className={msg.progressValue && msg.progressValue > 0.8 ? 'complete' : msg.progressValue && msg.progressValue > 0.5 ? 'active' : ''}>Synthesizing</span>
                      <span className={msg.progressValue && msg.progressValue >= 1 ? 'complete' : msg.progressValue && msg.progressValue > 0.8 ? 'active' : ''}>Rendering</span>
                    </div>
                  </div>
                )}
                {msg.showSelector && canShowSelector && (
                  <div className="selection-panel">
                    <div className="selection-grid">
                      <label className="selection-field">
                        <span className="selection-label">Part</span>
                        <select
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
                        onClick={handleSelectionSend}
                        disabled={!selectedPartKey || !selectedVerse}
                      >
                        Use selection
                      </button>
                      <span className="selection-hint">Or type your request below.</span>
                    </div>
                  </div>
                )}
                {msg.audioUrl && !score && (
                  <div className="audio-card">
                    <div className="audio-row">
                      <audio
                        ref={(element) => {
                          if (element) {
                            audioRefs.current[msg.id] = element;
                          } else {
                            delete audioRefs.current[msg.id];
                          }
                        }}
                        className="audio-player"
                        controls
                        src={msg.audioUrl}
                        onError={() => {
                          void handleAudioPlaybackError(msg.id, msg.progressUrl);
                        }}
                      />
                      <button
                        type="button"
                        className="audio-download-button"
                        aria-label="Download audio"
                        title="Download audio"
                        onClick={() => {
                          void handleAudioDownload(msg.id, msg.audioUrl, msg.progressUrl);
                        }}
                      >
                        <Download size={16} aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                )}
                {styleOptionsUnlocked && msg.audioUrl && score && isLatestMessage && (
                  <div className="audio-card">
                    <p className="audio-ready-message">
                      🎵 Audio ready - use the player in the studio panel
                    </p>
                    <div className="backing-style-row">
                      <span className="backing-style-label">Try other style:</span>
                      {[
                        { label: "Bossa Nova", prompt: "New backing in Bossa Nova" },
                        { label: "Rock", prompt: "New backing in Rock" },
                        { label: "Epic Orchestra", prompt: "New backing in Epic Orchestra" },
                      ].map((style) => (
                        <button
                          key={style.label}
                          type="button"
                          className="backing-style-btn"
                          onClick={() => {
                            // Clear current backing track
                            if (audioObjectUrlsRef.current.backing) {
                              URL.revokeObjectURL(audioObjectUrlsRef.current.backing);
                            }
                            audioObjectUrlsRef.current.backing = null;
                            setBackingUrl(null);
                            setBackingDownloadUrl(null);
                            // Send message to request new backing track with style
                            void sendMessage(style.prompt);
                            setBackingReplacementCount(prev => prev + 1);
                          }}
                        >
                          {style.label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
            })}
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
            <label className="upload-button">
              <UploadCloud size={18} />
              <span>{uploading ? "Uploading..." : "Upload Score"}</span>
              <input
                type="file"
                accept=".xml,.mxl,.musicxml,application/vnd.recordare.musicxml,application/vnd.recordare.musicxml+xml"
                disabled={!sessionId || uploading || creditsLocked}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) handleUpload(file);
                }}
              />
            </label>
            <div className="input-row">
              <input
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="Ask the singer to interpret or render..."
                onKeyDown={(event) => {
                  if (event.key === "Enter") handleSend();
                }}
                disabled={!sessionId || creditsLocked}
              />
              <button
                onClick={handleSend}
                className="send-button"
                disabled={!input.trim() || !sessionId || creditsLocked}
              >
                <Send size={18} />
              </button>
            </div>
          </div>
        </section>

        {/* Right Panel - Studio View - Only show after file upload */}
        {score && (
        <section className="studio-panel">
          {/* DAW Timeline Interface */}
          <div className="daw-container">
            {/* Transport Bar */}
            <div className="transport-bar">
              <div className="transport-controls">
                <button
                  type="button"
                  className={clsx("transport-btn", "play-btn", isPlaying && "active")}
                  onClick={togglePlay}
                  disabled={!vocalUrl && !backingUrl}
                  aria-label={isPlaying ? "Pause" : "Play"}
                >
                  {isPlaying ? <Pause size={18} /> : <Play size={18} />}
                </button>
                <button
                  type="button"
                  className="transport-btn reset-btn"
                  onClick={resetToBeginning}
                  disabled={!vocalUrl && !backingUrl}
                  aria-label="Reset to beginning"
                  title="Reset to beginning"
                >
                  <RotateCcw size={18} />
                </button>
              </div>
              <div className="time-display">
                <span className="current-time">{formatTime(currentTime)}</span>
                <span className="time-separator">/</span>
                <span className="total-time">{formatTime(duration)}</span>
              </div>
              <div className="master-volume">
                <Volume2 size={16} />
                <input
                  type="range"
                  min="0"
                  max="100"
                  value={vocalTrackState.volume}
                  onChange={handleVocalVolumeChange}
                  className="volume-slider"
                />
              </div>
            </div>

            {/* Timeline Ruler */}
            <div className="timeline-ruler">
              <div className="ruler-spacer" />
              <div className="ruler-markers">
                {Array.from({ length: 24 }).map((_, i) => (
                  <div key={i} className="ruler-mark">
                    <span className="ruler-number">{i + 1}</span>
                    <div className="ruler-ticks">
                      <div className="tick-major" />
                      <div className="tick-minor" />
                      <div className="tick-minor" />
                      <div className="tick-minor" />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Track Lane 1 - Vocal Track */}
            <div className="track-lane">
              <div className="track-header-col">
                <div className="track-number">1</div>
                <div className="track-name">Vocal Track</div>
                <div className="track-controls">
                  <button
                    type="button"
                    className={clsx("track-btn", "mute-btn", vocalTrackState.muted && "active")}
                    onClick={toggleVocalMute}
                    aria-label="Mute"
                  >
                    M
                  </button>
                  <button
                    type="button"
                    className={clsx("track-btn", "solo-btn", vocalTrackState.solo && "active")}
                    onClick={toggleVocalSolo}
                    aria-label="Solo"
                  >
                    S
                  </button>
                  <button
                    type="button"
                    className={clsx("track-btn", "rec-btn", vocalTrackState.recording && "active")}
                    onClick={() => setVocalTrackState(prev => ({ ...prev, recording: !prev.recording }))}
                    aria-label="Record"
                  >
                    R
                  </button>
                </div>
                <div className="track-volume">
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={vocalTrackState.volume}
                    onChange={handleVocalVolumeChange}
                    className="track-volume-slider"
                  />
                </div>
              </div>
              <div className="track-canvas-col">
                {vocalUrl ? (
                  <div className="audio-clip">
                    <div className="audio-clip-header">
                      <span className="audio-clip-name">Vocal</span>
                      <span className="audio-clip-duration">{formatTime(duration)}</span>
                    </div>
                    <div className="waveform-container" ref={vocalWaveformRef} />
                  </div>
                ) : (
                  <div className="track-empty">
                    <span>Singing audio will appear here</span>
                  </div>
                )}
                {/* Playhead */}
                <div 
                  className="playhead" 
                  style={{ left: `${(currentTime / (duration || 1)) * 100}%` }}
                />
              </div>
            </div>

            {/* Track Lane 2 - Backing Track */}
            <div className="track-lane">
              <div className="track-header-col">
                <div className="track-number">2</div>
                <div className="track-name">Backing Track</div>
                <div className="track-controls">
                  <button
                    type="button"
                    className={clsx("track-btn", "mute-btn", backingTrackState.muted && "active")}
                    onClick={toggleBackingMute}
                    aria-label="Mute"
                  >
                    M
                  </button>
                  <button
                    type="button"
                    className={clsx("track-btn", "solo-btn", backingTrackState.solo && "active")}
                    onClick={toggleBackingSolo}
                    aria-label="Solo"
                  >
                    S
                  </button>
                  <button
                    type="button"
                    className="track-btn replace-btn"
                    onClick={() => {
                      // Clear current backing track only
                      if (audioObjectUrlsRef.current.backing) {
                        URL.revokeObjectURL(audioObjectUrlsRef.current.backing);
                      }
                      audioObjectUrlsRef.current.backing = null;
                      setBackingUrl(null);
                      setBackingDownloadUrl(null);
                      // Prompt for a fresh backing
                      const message = "New backing track for this song";
                      void sendMessage(message);
                      setBackingReplacementCount(prev => prev + 1);
                    }}
                    aria-label="Replace backing track"
                    title="Replace backing track"
                  >
                    <Replace size={14} />
                  </button>
                </div>
                <div className="track-volume">
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={backingTrackState.volume}
                    onChange={handleBackingVolumeChange}
                    className="track-volume-slider"
                  />
                </div>
              </div>
              <div className="track-canvas-col">
                {backingUrl ? (
                  <div className="audio-clip backing">
                    <div className="audio-clip-header">
                      <span className="audio-clip-name">Backing</span>
                      <span className="audio-clip-duration">{formatTime(duration)}</span>
                    </div>
                    <div className="waveform-container" ref={backingWaveformRef} />
                  </div>
                ) : (
                  <div className="track-empty">
                    <span>Backing track will appear here</span>
                  </div>
                )}
                {/* Playhead */}
                <div 
                  className="playhead" 
                  style={{ left: `${(currentTime / (duration || 1)) * 100}%` }}
                />
              </div>
            </div>

          </div>

          {/* Score Preview (Bottom) */}
          <div className="score-lane">
            <div className="score-header">
              {score && (
                <div className="score-controls">
                  <button
                    type="button"
                    className="zoom-button fullscreen-btn"
                    onClick={() => setShowScoreLarge(true)}
                    aria-label="Fullscreen"
                    title="Fullscreen"
                  >
                    <Maximize2 size={14} />
                  </button>
                  <button
                    type="button"
                    className="zoom-button"
                    onClick={() => handleZoom(-0.1)}
                    aria-label="Zoom out"
                  >
                    <Minus size={14} />
                  </button>
                  <span className="zoom-value">{Math.round(zoomLevel * 100)}%</span>
                  <button
                    type="button"
                    className="zoom-button"
                    onClick={() => handleZoom(0.1)}
                    aria-label="Zoom in"
                  >
                    <Plus size={14} />
                  </button>
                </div>
              )}
            </div>
            <div className="score-canvas">
              <div ref={scoreRef} className="score-surface" />
              {!score && !uploading && (
                <div className="score-empty-state">
                  <span>Upload a MusicXML file to see the score</span>
                </div>
              )}
            </div>
          </div>
        </section>
        )}

        {/* Large Score Modal */}
        {showScoreLarge && score && (
          <div className="score-modal-overlay" onClick={() => setShowScoreLarge(false)}>
            <div className="score-modal" onClick={(e) => e.stopPropagation()}>
              <div className="score-modal-header">
                <h3>{score.name}</h3>
                <div className="score-modal-actions">
                  <div className="zoom-controls">
                    <button
                      type="button"
                      className="zoom-button"
                      onClick={() => setScoreLargeZoom(prev => Math.max(0.5, prev - 0.1))}
                      aria-label="Zoom out"
                    >
                      <Minus size={14} />
                    </button>
                    <span className="zoom-value">{Math.round(scoreLargeZoom * 100)}%</span>
                    <button
                      type="button"
                      className="zoom-button"
                      onClick={() => setScoreLargeZoom(prev => Math.min(2, prev + 0.1))}
                      aria-label="Zoom in"
                    >
                      <Plus size={14} />
                    </button>
                  </div>
                  <button
                    type="button"
                    className="close-modal-button"
                    onClick={() => setShowScoreLarge(false)}
                    aria-label="Close"
                  >
                    <X size={20} />
                  </button>
                </div>
              </div>
              <div className="score-modal-canvas">
                <div ref={scoreLargeRef} className="score-surface" />
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
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
    return JSON.stringify(details, null, 2);
  } catch {
    return String(details);
  }
}

function extractAttemptMessages(details: unknown): AttemptMessage[] | undefined {
  if (!details || typeof details !== "object") return undefined;
  const raw = (details as { attempt_messages?: unknown }).attempt_messages;
  if (!Array.isArray(raw)) return undefined;
  const entries: AttemptMessage[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const attemptNumber = Number((entry as { attempt_number?: unknown }).attempt_number);
    if (!Number.isFinite(attemptNumber)) continue;
    const message = (entry as { message?: unknown }).message;
    const thought = (entry as { thought_summary?: unknown }).thought_summary;
    entries.push({
      attempt_number: attemptNumber,
      message: typeof message === "string" ? message : undefined,
      thought_summary: typeof thought === "string" ? thought : undefined,
    });
  }
  return entries.length > 0 ? entries : undefined;
}
