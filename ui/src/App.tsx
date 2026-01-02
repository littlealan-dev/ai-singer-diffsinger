import { useEffect, useMemo, useRef, useState } from "react";
import { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { UploadCloud, Send, Sparkles } from "lucide-react";
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
    if (!scoreRef.current || !score) return;
    const osmd = new OpenSheetMusicDisplay(scoreRef.current, {
      autoResize: true,
      drawTitle: true,
      followCursor: false,
      renderSingleHorizontalStaffline: false,
    });
    osmdRef.current = osmd;
    osmd
      .load(score.data as string | ArrayBuffer)
      .then(() => osmd.render())
      .catch((err) => {
        setError(err?.message || "Failed to render the score.");
      });
  }, [score]);

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

      <main className="split-grid">
        <section className="chat-panel">
          <div className="chat-header">
            <h2>Studio Chat</h2>
            <span className="chat-subtitle">Local history only</span>
          </div>
          <div className="chat-stream">
            {messages.length === 0 && (
              <div className="empty-state">
                <p>Upload a MusicXML score to begin.</p>
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

        <section className="score-panel">
          <div className="score-header">
            <h2>Score Preview</h2>
            <span className="chat-subtitle">
              Latest upload only {audioUrl ? "· Audio ready" : ""}
            </span>
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
