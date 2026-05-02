import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { UploadCloud, Send, Sparkles, Minus, Plus, Download, ChevronsUpDown, Check } from "lucide-react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import {
  chat,
  createSession,
  fetchScoreXml,
  fetchProgress,
  uploadScore,
  type ChatSelection,
  type ProgressResponse,
  type ScoreSummary,
} from "./api";
import CreditsHeader from "./components/CreditsHeader";
import { UserMenu } from "./components/UserMenu";
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

const MOCK_VOICE_OPTIONS = [
  { name: "Hitsune Kumi", image: "/voicebanks/hitsune_kumi.png" },
  { name: "Katyusha", image: "/voicebanks/katyusha.webp" },
  { name: "Keiro Revenant", image: "/voicebanks/keiro_revenant.webp" },
  { name: "Liam Thorne", image: "/voicebanks/liam_thorne.webp" },
  { name: "Printto Magicbeat Indigo", image: "/voicebanks/printto_magicbeat_indigo.png" },
  { name: "Qixuan (绮萱)", image: "/voicebanks/qixuan.png" },
  { name: "SAiFA", image: "/voicebanks/saifa.webp" },
] as const;

const isInsufficientCreditError = (message: string): boolean =>
  /insufficient credits|requires ~?\d+ credits|out of credits/i.test(message);

export default function MainApp() {
  const navigate = useNavigate();
  const { user, isAuthenticated } = useAuth();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [score, setScore] = useState<ScorePayload | null>(null);
  const [scoreSummary, setScoreSummary] = useState<ScoreSummary | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [splitPct, setSplitPct] = useState(40);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [scoreReady, setScoreReady] = useState(false);
  const [selectedPartKey, setSelectedPartKey] = useState<string | null>(null);
  const [selectedVerse, setSelectedVerse] = useState<string | null>(null);
  const [pendingSelection, setPendingSelection] = useState(false);
  const [selectorShown, setSelectorShown] = useState(false);
  const [selectedVoiceName, setSelectedVoiceName] = useState<string>(MOCK_VOICE_OPTIONS[0].name);
  const [voiceMenuOpen, setVoiceMenuOpen] = useState(false);
  const [showWaitlistModal, setShowWaitlistModal] = useState(false);
  const [waitlistSource, setWaitlistSource] = useState<WaitlistSource>("studio_menu");
  const [showCreditsModal, setShowCreditsModal] = useState(false);
  const [showTrialExpiredModal, setShowTrialExpiredModal] = useState(false);
  const [paywallTrigger, setPaywallTrigger] = useState<PaywallTrigger | null>(null);
  const [paywallDetail, setPaywallDetail] = useState<string | null>(null);
  const [expandedThoughts, setExpandedThoughts] = useState<Record<string, boolean>>({});
  const [expandedDiagnostics, setExpandedDiagnostics] = useState<Record<string, boolean>>({});
  const [activeProgress, setActiveProgress] = useState<{
    messageId: string;
    url: string;
  } | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const audioRefreshPromisesRef = useRef<Record<string, Promise<string | null> | undefined>>({});
  const sessionInitPromiseRef = useRef<Promise<string> | null>(null);
  const activeUserIdRef = useRef<string | null>(user?.uid ?? null);
  const autoPaywallTriggersRef = useRef<Set<string>>(new Set());
  const pendingCheckoutStartedRef = useRef(false);
  const billingSyncInFlightRef = useRef(false);
  const lastBillingSyncAtRef = useRef(0);
  const checkoutReturnSyncStartedRef = useRef(false);

  const splitStyle = useMemo(
    () => ({ "--split": `${splitPct}%` }) as CSSProperties,
    [splitPct]
  );
  const {
    available,
    expiresAt,
    overdrafted,
    isExpired,
    loading: creditsLoading,
  } = useCredits();
  const billing = useBillingState();
  const creditsLocked = !creditsLoading && (overdrafted || isExpired || available <= 0);

  const {
      showAnnouncement,
      currentAnnouncement,
      markAsSeen
  } = useAnnouncements();

  const estimatedDuration = scoreSummary?.duration_seconds;
  const estimatedDurationLabel =
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? `Estimated duration: ${formatDuration(estimatedDuration)}`
      : null;

  const estimatedCost = 
    typeof estimatedDuration === "number" && estimatedDuration > 0
      ? Math.ceil(estimatedDuration / 30)
      : null;
  const estimatedCostLabel = estimatedCost !== null ? `Estimated cost per part: ${estimatedCost} credits` : null;
  const selectedVoice =
    MOCK_VOICE_OPTIONS.find((voice) => voice.name === selectedVoiceName) ?? MOCK_VOICE_OPTIONS[0];

  const layoutRef = useRef<HTMLDivElement | null>(null);
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const dragStateRef = useRef<{
    containerLeft: number;
    containerWidth: number;
  } | null>(null);

  useEffect(() => {
    activeUserIdRef.current = user?.uid ?? null;
  }, [user?.uid]);

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

  const openPaywall = (trigger: PaywallTrigger, detail?: string | null) => {
    setPaywallTrigger(trigger);
    setPaywallDetail(detail ?? null);
  };

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
    const sessionId = url.searchParams.get("session_id");
    const billingSync = url.searchParams.get("billing") === "sync";
    const returnedFromCheckout = checkoutStatus === "success" || Boolean(sessionId);
    const returnedFromPortal = billingSync || hasPendingBillingPortalSync();
    if (!returnedFromCheckout && !returnedFromPortal) return;

    checkoutReturnSyncStartedRef.current = true;
    const cleanupReturnUrl = () => {
      url.searchParams.delete("checkout");
      url.searchParams.delete("session_id");
      url.searchParams.delete("billing");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    };

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
    if (creditsLoading || billing.loading) return;
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
  }, [available, billing.loading, creditsLoading, isExpired, overdrafted, user?.uid]);

  useEffect(() => {
    if (billing.loading || !isAuthenticated || pendingCheckoutStartedRef.current) return;
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const queryPlan = params.get("checkoutPlan");
    const storedPlan = getStoredPendingCheckoutPlan();
    const planKey = isPaidPlanKey(queryPlan) ? queryPlan : storedPlan;
    const returnedFromHostedBilling =
      params.get("checkout") === "success" ||
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
    setScoreReady(false);
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
        setError(err?.message || "Failed to render the score.");
      });
  }, [score]);

  useEffect(() => {
    if (!scoreReady || !osmdRef.current) return;
    osmdRef.current.zoom = zoomLevel;
    osmdRef.current.render();
  }, [zoomLevel, scoreReady]);

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
          };
        })
      );
      if (nextAudioUrl) {
        setAudioUrl(nextAudioUrl);
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
      setAudioUrl((current) => (current ? nextAudioUrl : current));
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
      const downloadUrl = new URL(nextAudioUrl, window.location.origin);
      downloadUrl.searchParams.set("download", "1");
      const link = document.createElement("a");
      link.href = downloadUrl.toString();
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
    if (creditsLocked) {
      openPaywall("upload_blocked");
      return;
    }
    setUploading(true);
    setError(null);
    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const uploadResponse = await uploadScore(activeSessionId, file);
      const summary = uploadResponse.score_summary ?? null;
      setScoreSummary(summary);
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

  const sendMessage = async (content: string, selection?: ChatSelection) => {
    if (!content.trim()) return;
    if (creditsLocked) {
      openPaywall(selection ? "selection_blocked" : "chat_blocked");
      return;
    }
    setStatus("Thinking...");
    setError(null);
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      content,
    });

    try {
      const activeSessionId = sessionId ?? await ensureSession();
      const response = await chat(activeSessionId, content, selection);
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
        setAudioUrl(response.audio_url);
        if (pendingSelection) {
          setPendingSelection(false);
        }
      }
      if (response.type === "chat_progress") {
        assistantMessage.progressUrl = response.progress_url;
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
    }
  };

  const handleSend = async () => {
    if (!input.trim()) return;
    if (creditsLocked) {
      openPaywall("chat_blocked");
      return;
    }
    const content = input.trim();
    setInput("");
    await sendMessage(content);
  };

  const handleSelectionSend = async () => {
    if (!selectedPartKey || !selectedVerse) return;
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
    });
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
    <div className="app-shell">
      <header className="app-header">
        <div className="brand" onClick={handleBrandClick} style={{ cursor: "pointer" }}>
          <Sparkles className="brand-icon" />
          <div>
            <h1>SightSinger.app</h1>
            <p>{headerSubtitle}</p>
          </div>
        </div>
        <div className="header-actions">
          <CreditsHeader
            available={available}
            expiresAt={expiresAt}
            isExpired={isExpired}
            overdrafted={overdrafted}
            loading={creditsLoading}
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
      <BillingPaywallModal
        isOpen={paywallTrigger !== null}
        trigger={paywallTrigger ?? "billing_menu"}
        billing={billing}
        detail={paywallDetail}
        onClose={() => setPaywallTrigger(null)}
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
          <div className="chat-stream" ref={chatStreamRef} onScroll={handleChatScroll}>
            {messages.length === 0 && (
              <div className="empty-state">
                <p>Drop a MusicXML file here to begin.</p>
              </div>
            )}
            {messages.map((msg, index) => (
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
            <label
              className="upload-button"
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
                accept=".xml,.mxl"
                disabled={uploading || creditsLocked}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) handleUpload(file);
                }}
              />
            </label>
            <div className="input-row composer-row">
              <div className="voice-picker">
                {voiceMenuOpen ? (
                  <div className="voice-picker-menu" role="listbox" aria-label="Select AI voice">
                    {MOCK_VOICE_OPTIONS.map((voice) => {
                      const isSelected = voice.name === selectedVoice.name;
                      return (
                        <button
                          key={voice.name}
                          type="button"
                          className={clsx("voice-picker-option", { selected: isSelected })}
                          onClick={() => {
                            setSelectedVoiceName(voice.name);
                            setVoiceMenuOpen(false);
                          }}
                        >
                          <img
                            src={voice.image}
                            alt=""
                            className="voice-picker-option-avatar"
                            aria-hidden="true"
                          />
                          <span className="voice-picker-option-name">{voice.name}</span>
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
                  onClick={() => setVoiceMenuOpen((open) => !open)}
                >
                  <img
                    src={selectedVoice.image}
                    alt=""
                    className="voice-picker-trigger-avatar"
                    aria-hidden="true"
                  />
                  <span className="voice-picker-trigger-copy">
                    <span className="voice-picker-trigger-label">Voice</span>
                    <span className="voice-picker-trigger-name">{selectedVoice.name}</span>
                  </span>
                  <ChevronsUpDown size={16} aria-hidden="true" />
                </button>
              </div>
              <input
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="Ask the singer to interpret or render..."
                onKeyDown={(event) => {
                  if (event.key === "Enter") handleSend();
                }}
                disabled={creditsLocked}
              />
              <button
                onClick={handleSend}
                className="send-button"
                disabled={!input.trim()}
                aria-disabled={creditsLocked}
              >
                <Send size={18} />
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

        <section className="score-panel">
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
              <div className="zoom-controls">
                <button
                  type="button"
                  className="zoom-button"
                  onClick={() => handleZoom(-0.1)}
                  aria-label="Zoom out"
                  disabled={!score}
                >
                  <Minus size={16} />
                </button>
                <span className="zoom-value">{Math.round(zoomLevel * 100)}%</span>
                <button
                  type="button"
                  className="zoom-button"
                  onClick={() => handleZoom(0.1)}
                  aria-label="Zoom in"
                  disabled={!score}
                >
                  <Plus size={16} />
                </button>
              </div>
            </div>
          </div>
          <div className="score-canvas">
            <div ref={scoreRef} className="score-surface" />
            {!score && (
              <div className="score-placeholder">
                <p>Upload a MusicXML file to render the score here.</p>
              </div>
            )}
          </div>
        </section>
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
  const entries = raw
    .map((entry) => {
      if (!entry || typeof entry !== "object") return null;
      const attemptNumber = Number((entry as { attempt_number?: unknown }).attempt_number);
      if (!Number.isFinite(attemptNumber)) return null;
      const message = (entry as { message?: unknown }).message;
      const thought = (entry as { thought_summary?: unknown }).thought_summary;
      return {
        attempt_number: attemptNumber,
        message: typeof message === "string" ? message : undefined,
        thought_summary: typeof thought === "string" ? thought : undefined,
      } satisfies AttemptMessage;
    })
    .filter((entry): entry is AttemptMessage => entry !== null);
  return entries.length > 0 ? entries : undefined;
}
