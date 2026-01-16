import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { UploadCloud, Send, Sparkles, Minus, Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";
import {
  chat,
  createSession,
  fetchScoreXml,
  fetchProgress,
  uploadScore,
  type ProgressResponse,
  type ScoreSummary,
} from "./api";
import CreditsHeader from "./components/CreditsHeader";
import { UserMenu } from "./components/UserMenu";

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
  showSelector?: boolean;
  progressUrl?: string;
  isProgress?: boolean;
  progressValue?: number;
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
  const [activeProgress, setActiveProgress] = useState<{
    messageId: string;
    url: string;
  } | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);

  const splitStyle = useMemo(
    () => ({ "--split": `${splitPct}%` }) as CSSProperties,
    [splitPct]
  );

  const layoutRef = useRef<HTMLDivElement | null>(null);
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const dragStateRef = useRef<{
    containerLeft: number;
    containerWidth: number;
  } | null>(null);

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
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== activeProgress.messageId) return msg;
          return {
            ...msg,
            content: appendProgressMessage(msg.content, nextMessage),
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
        if (payload.status === "done") {
          setActiveProgress(null);
        }
        if (payload.status === "error") {
          setActiveProgress(null);
          setError(payload.error || payload.message || "Synthesis failed.");
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
    container.scrollTop = container.scrollHeight;
  }, [messages, status]);

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

  const handleUpload = async (file: File) => {
    if (!sessionId) return;
    setUploading(true);
    setError(null);
    try {
      const uploadResponse = await uploadScore(sessionId, file);
      const summary = uploadResponse.score_summary ?? null;
      setScoreSummary(summary);
      setPendingSelection(shouldPromptSelection(summary));
      setSelectorShown(false);
      const nextPartOptions = buildPartOptions(summary);
      const nextVerseOptions = buildVerseOptions(summary);
      setSelectedPartKey(nextPartOptions[0]?.key ?? null);
      setSelectedVerse(nextVerseOptions[0] ?? null);
      const data = await fetchScoreXml(sessionId);
      setScore({ name: file.name, data });
    } catch (err: any) {
      setError(err?.message || "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const sendMessage = async (content: string) => {
    if (!content.trim() || !sessionId) return;
    setStatus("Thinking...");
    setError(null);
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      content,
    });

    try {
      const response = await chat(sessionId, content);
      const assistantMessage: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: response.message,
      };
      if (response.type === "chat_text" && pendingSelection && !selectorShown) {
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
      appendMessage(assistantMessage);
      if (response.type === "chat_progress") {
        setActiveProgress({ messageId: assistantMessage.id, url: response.progress_url });
      }
    } catch (err: any) {
      setError(err?.message || "Failed to send message.");
    } finally {
      setStatus(null);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || !sessionId) return;
    const content = input.trim();
    setInput("");
    await sendMessage(content);
  };

  const handleSelectionSend = async () => {
    if (!selectedPartKey || !selectedVerse) return;
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
    if (!isDragging) setIsDragging(true);
  };

  const handleDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node)) return;
    setIsDragging(false);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
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

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand" onClick={() => navigate("/")} style={{ cursor: 'pointer' }}>
          <Sparkles className="brand-icon" />
          <div>
            <h1>SightSinger.ai</h1>
            <p>{headerSubtitle}</p>
          </div>
        </div>
        <div className="header-actions">
          <CreditsHeader />
          <div className="status-pill">{status ?? "Ready"}</div>
          <UserMenu />
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}

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
          <div className="chat-stream" ref={chatStreamRef}>
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
                <p>{msg.content}</p>
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
                  <audio className="audio-player" controls src={msg.audioUrl} />
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
            <label className="upload-button">
              <UploadCloud size={18} />
              <span>{uploading ? "Uploading..." : "Upload Score"}</span>
              <input
                type="file"
                accept=".xml,.mxl"
                disabled={!sessionId || uploading}
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
                disabled={!sessionId}
              />
              <button
                onClick={handleSend}
                className="send-button"
                disabled={!input.trim() || !sessionId}
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
              <span className="chat-subtitle">
                Latest upload only {audioUrl ? "· Audio ready" : ""}
              </span>
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
    </div>
  );
}
