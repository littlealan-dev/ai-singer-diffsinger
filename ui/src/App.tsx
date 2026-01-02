import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { UploadCloud, Send, Sparkles, Minus, Plus } from "lucide-react";
import clsx from "clsx";
import { chat, createSession, uploadScore, type ChatResponse } from "./api";

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
};

type ScorePayload = {
  name: string;
  data: string | ArrayBuffer;
};

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [score, setScore] = useState<ScorePayload | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [splitPct, setSplitPct] = useState(40);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [scoreReady, setScoreReady] = useState(false);

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
      .load(score.data as string | ArrayBuffer)
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

  const headerSubtitle = useMemo(() => {
    if (!sessionId) return "Initializing session...";
    if (score) return `Session ${sessionId.slice(0, 6)} · ${score.name}`;
    return `Session ${sessionId.slice(0, 6)} · Upload a score to begin`;
  }, [sessionId, score]);

  const appendMessage = (message: Message) => {
    setMessages((prev) => [...prev, message]);
  };

  const handleUpload = async (file: File) => {
    if (!sessionId) return;
    setUploading(true);
    setError(null);
    try {
      await uploadScore(sessionId, file);
      const data =
        file.name.toLowerCase().endsWith(".mxl")
          ? await file.arrayBuffer()
          : await file.text();
      setScore({ name: file.name, data });
    } catch (err: any) {
      setError(err?.message || "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || !sessionId) return;
    const content = input.trim();
    setInput("");
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
      if (response.type === "chat_audio") {
        assistantMessage.audioUrl = response.audio_url;
        setAudioUrl(response.audio_url);
      }
      appendMessage(assistantMessage);
    } catch (err: any) {
      setError(err?.message || "Failed to send message.");
    } finally {
      setStatus(null);
    }
  };

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
        <div className="brand">
          <Sparkles className="brand-icon" />
          <div>
            <h1>AI Singer</h1>
            <p>{headerSubtitle}</p>
          </div>
        </div>
        <div className="status-pill">{status ?? "Ready"}</div>
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
            <span className="chat-subtitle">Local history only</span>
          </div>
          <div className="chat-stream">
            {messages.length === 0 && (
              <div className="empty-state">
                <p>Drop a MusicXML file here to begin.</p>
              </div>
            )}
            {messages.map((msg, index) => (
              <div
                key={msg.id}
                className={clsx("chat-bubble", msg.role, "reveal")}
                style={{ animationDelay: `${index * 40}ms` }}
              >
                <p>{msg.content}</p>
                {msg.audioUrl && (
                  <audio className="audio-player" controls src={msg.audioUrl} />
                )}
              </div>
            ))}
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
