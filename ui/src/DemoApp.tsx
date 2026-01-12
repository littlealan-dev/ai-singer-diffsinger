import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { Sparkles, UploadCloud, Minus, Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import clsx from "clsx";

type Role = "user" | "assistant";

type Message = {
  id: string;
  role: Role;
  content: string;
  audioUrl?: string;
  showSelector?: boolean;
  isProgress?: boolean;
};

const DEMO_SCORE_URL = "/landing/demo/scores/amazing-grace.mxl";
const DEMO_AUDIO_URLS = {
  soprano: "/landing/demo/audio/amazing-grace-soprano.mp3",
  tenor: "/landing/demo/audio/amazing-grace-tenor.mp3",
};

const DEMO_PROMPTS = [
  "Can you sing this in a female voice?",
  "Can you sing the soprano part, verse 1?",
  "Can you sing it in a softer voice?",
];

const DEMO_STEPS = [
  "Got it, getting ready to sing...",
  "Warming up the voice...",
  "Capturing the take...",
];

export default function DemoApp() {
  const navigate = useNavigate();
  const [messages, setMessages] = useState<Message[]>([]);
  const [status, setStatus] = useState<string>("Demo mode");
  const [scoreLoaded, setScoreLoaded] = useState(false);
  const [scoreReady, setScoreReady] = useState(false);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [selectedPart, setSelectedPart] = useState<string>("soprano");
  const [selectedVerse, setSelectedVerse] = useState<string>("1");
  const [selectorMessageId, setSelectorMessageId] = useState<string | null>(null);
  const [pendingSelection, setPendingSelection] = useState(false);
  const [showPromptOptions, setShowPromptOptions] = useState(false);
  const [progressMessageId, setProgressMessageId] = useState<string | null>(null);
  const [splitPct] = useState(40);
  const timersRef = useRef<number[]>([]);

  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const scoreRef = useRef<HTMLDivElement | null>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);

  const splitStyle = useMemo(
    () => ({ "--split": `${splitPct}%` }) as CSSProperties,
    [splitPct]
  );

  const schedule = (fn: () => void, delay: number) => {
    const timer = window.setTimeout(fn, delay);
    timersRef.current.push(timer);
  };

  const clearTimers = () => {
    timersRef.current.forEach((timer) => window.clearTimeout(timer));
    timersRef.current = [];
  };

  useEffect(() => {
    return () => {
      clearTimers();
    };
  }, []);

  useEffect(() => {
    if (!scoreRef.current || !scoreLoaded) return;
    setScoreReady(false);
    const osmd = new OpenSheetMusicDisplay(scoreRef.current, {
      autoResize: true,
      drawTitle: true,
      followCursor: false,
      renderSingleHorizontalStaffline: false,
    });
    osmdRef.current = osmd;
    osmd
      .load(DEMO_SCORE_URL)
      .then(() => {
        osmd.zoom = zoomLevel;
        osmd.render();
        setScoreReady(true);
      })
      .catch(() => {
        setStatus("Score preview failed.");
      });
  }, [scoreLoaded]);

  useEffect(() => {
    if (!scoreReady || !osmdRef.current) return;
    osmdRef.current.zoom = zoomLevel;
    osmdRef.current.render();
  }, [zoomLevel, scoreReady]);

  useEffect(() => {
    if (!chatStreamRef.current) return;
    chatStreamRef.current.scrollTop = chatStreamRef.current.scrollHeight;
  }, [messages, pendingSelection]);

  const addMessage = (message: Omit<Message, "id">) => {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setMessages((prev) => [...prev, { ...message, id }]);
    return id;
  };

  const handleUseDemo = () => {
    clearTimers();
    setMessages([]);
    setStatus("Uploading demo score...");
    setScoreLoaded(false);
    setScoreReady(false);
    setPendingSelection(false);
    setSelectorMessageId(null);
    setProgressMessageId(null);
    setShowPromptOptions(false);

    schedule(() => {
      setScoreLoaded(true);
      setStatus("Score ready");
      setShowPromptOptions(true);
    }, 800);
  };

  const handlePrompt = (prompt: string) => {
    if (!scoreLoaded || pendingSelection) return;
    setShowPromptOptions(false);
    addMessage({ role: "user", content: prompt });
    const selectorId = addMessage({
      role: "assistant",
      content: "Absolutely. Choose a part and verse to continue.",
      showSelector: true,
    });
    setSelectorMessageId(selectorId);
    setPendingSelection(true);
  };

  const handleSelectionSend = () => {
    if (!pendingSelection || !selectorMessageId) return;
    setMessages((prev) =>
      prev.map((msg) =>
        msg.id === selectorMessageId ? { ...msg, showSelector: false } : msg
      )
    );
    const partLabel = selectedPart === "tenor" ? "Tenor" : "Soprano";
    addMessage({
      role: "user",
      content: `${partLabel}, verse ${selectedVerse}.`,
    });
    setStatus("Synthesizing...");
    setPendingSelection(false);
    const progressId = addMessage({
      role: "assistant",
      content: "",
      isProgress: true,
    });
    setProgressMessageId(progressId);
    DEMO_STEPS.forEach((step, index) => {
      schedule(() => {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id !== progressId) return msg;
            const nextContent = msg.content ? `${msg.content}\n${step}` : step;
            return { ...msg, content: nextContent, isProgress: true };
          })
        );
      }, 600 * (index + 1));
    });
    schedule(() => {
      const audioUrl =
        selectedPart === "tenor"
          ? DEMO_AUDIO_URLS.tenor
          : DEMO_AUDIO_URLS.soprano;
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === progressId
            ? {
                ...msg,
                content: `${msg.content}\nYour take is ready.`,
                audioUrl,
                isProgress: false,
              }
            : msg
        )
      );
      setStatus("Audio ready");
    }, 600 * (DEMO_STEPS.length + 1));
  };

  const handleZoom = (delta: number) => {
    setZoomLevel((prev) => Math.max(0.5, Math.min(1.5, prev + delta)));
  };

  return (
    <div className="app-shell demo-app">
      <header className="app-header">
        <div className="brand" onClick={() => navigate("/")} style={{ cursor: "pointer" }}>
          <Sparkles className="brand-icon" />
          <div>
            <h1>SightSinger.ai</h1>
            <p>Scripted demo — no backend calls</p>
          </div>
        </div>
        <div className="status-pill">{status}</div>
      </header>

      <main className="split-grid" style={splitStyle}>
        <section className={clsx("chat-panel", "demo-panel")}>
          <div className="chat-header">
            <h2>Studio Chat</h2>
            <span className="chat-subtitle">Preset prompts only</span>
          </div>
          <div className="chat-stream" ref={chatStreamRef}>
            {messages.length === 0 && (
              <button
                type="button"
                className="empty-state demo-empty-action"
                onClick={handleUseDemo}
              >
                <UploadCloud size={20} />
                <p>Select the demo score to begin.</p>
              </button>
            )}
            {showPromptOptions && (
              <div className="chat-bubble assistant demo-options">
                <p>Pick a prompt to hear a take.</p>
                <div className="demo-option-grid">
                  {DEMO_PROMPTS.map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      className="demo-option-button"
                      onClick={() => handlePrompt(prompt)}
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={clsx(
                  "chat-bubble",
                  msg.role,
                  msg.isProgress && "progress-bubble",
                  msg.audioUrl && "audio-bubble",
                  "reveal"
                )}
              >
                <p>{msg.content}</p>
                {msg.isProgress && !msg.audioUrl && (
                  <div className="thinking-dots" aria-label="Processing">
                    <span />
                    <span />
                    <span />
                  </div>
                )}
                {msg.showSelector && (
                  <div className="selection-panel">
                    <div className="selection-grid">
                      <label className="selection-field">
                        <span className="selection-label">Part</span>
                        <select
                          className="selection-select"
                          value={selectedPart}
                          onChange={(event) => setSelectedPart(event.target.value)}
                        >
                          <option value="soprano">Soprano</option>
                          <option value="tenor">Tenor</option>
                        </select>
                      </label>
                      <label className="selection-field">
                        <span className="selection-label">Verse</span>
                        <select
                          className="selection-select"
                          value={selectedVerse}
                          onChange={(event) => setSelectedVerse(event.target.value)}
                        >
                          <option value="1">Verse 1</option>
                        </select>
                      </label>
                    </div>
                    <div className="selection-actions">
                      <button
                        type="button"
                        className="selection-send"
                        onClick={handleSelectionSend}
                      >
                        Use selection
                      </button>
                      <span className="selection-hint">Demo options are fixed.</span>
                    </div>
                  </div>
                )}
                {msg.audioUrl && (
                  <audio className="audio-player" controls src={msg.audioUrl} />
                )}
              </div>
            ))}
          </div>
          <div className="chat-input demo-chat-input">
            <button
              type="button"
              className="demo-reset-button"
              onClick={() => {
                clearTimers();
                setMessages([]);
                setStatus("Demo mode");
                setScoreLoaded(false);
                setScoreReady(false);
                setPendingSelection(false);
                setSelectorMessageId(null);
                setProgressMessageId(null);
                setShowPromptOptions(false);
              }}
            >
              Reset demo
            </button>
          </div>
        </section>

        <div className="split-handle" aria-hidden="true" />

        <section className="score-panel">
          <div className="score-header">
            <h2>Score Preview</h2>
            <div className="score-controls">
              <span className="chat-subtitle">
                Demo score only {scoreReady ? "· Ready" : ""}
              </span>
              <div className="zoom-controls">
                <button
                  type="button"
                  className="zoom-button"
                  onClick={() => handleZoom(-0.1)}
                  aria-label="Zoom out"
                  disabled={!scoreLoaded}
                >
                  <Minus size={16} />
                </button>
                <span className="zoom-value">{Math.round(zoomLevel * 100)}%</span>
                <button
                  type="button"
                  className="zoom-button"
                  onClick={() => handleZoom(0.1)}
                  aria-label="Zoom in"
                  disabled={!scoreLoaded}
                >
                  <Plus size={16} />
                </button>
              </div>
            </div>
          </div>
          <div className="score-canvas">
            <div ref={scoreRef} className="score-surface" />
            {!scoreLoaded && (
              <div className="score-placeholder">
                <p>Click “Use demo score” to render Amazing Grace.</p>
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
