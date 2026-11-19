import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError, shareSong, unshareSong } from "../api";
import "./ShareSongModal.css";

export type ShareState = { enabled: boolean; url: string | null };

interface ShareSongModalProps {
  songId: string;
  title: string;
  /** The link, when the Song's sharing is already on: the box opens on it without a request. */
  url: string | null;
  onClose: () => void;
  onShareChange: (songId: string, share: ShareState) => void;
}

type View =
  | { kind: "loading" }
  | { kind: "notice"; accepting: boolean }
  | { kind: "link"; url: string; copied: boolean }
  | { kind: "confirm-off"; url: string; turningOff: boolean }
  | { kind: "error"; message: string };

/**
 * Share a Song: the one-time notice, then the link with Copy and Turn off.
 * Turning a link off ends it for good; sharing again makes a new one.
 */
export default function ShareSongModal({ songId, title, url, onClose, onShareChange }: ShareSongModalProps) {
  const [view, setView] = useState<View>(url ? { kind: "link", url, copied: false } : { kind: "loading" });
  const onShareChangeRef = useRef(onShareChange);
  onShareChangeRef.current = onShareChange;

  const turnOn = async (noticeAccepted: boolean) => {
    try {
      const result = await shareSong(songId, noticeAccepted);
      if (!result.url) throw new Error("The share link is missing.");
      onShareChangeRef.current(songId, { enabled: true, url: result.url });
      setView({ kind: "link", url: result.url, copied: false });
    } catch (err) {
      if (err instanceof ApiError && err.code === "share_notice_required") {
        setView({ kind: "notice", accepting: false });
        return;
      }
      setView({ kind: "error", message: err instanceof Error ? err.message : "Couldn't share this song." });
    }
  };

  useEffect(() => {
    if (!url) void turnOn(false);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // Opened once per Song.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [songId]);

  const copy = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link);
      setView({ kind: "link", url: link, copied: true });
    } catch {
      setView({ kind: "error", message: "Couldn't copy the link. Select it and copy it instead." });
    }
  };

  const turnOff = async (link: string) => {
    setView({ kind: "confirm-off", url: link, turningOff: true });
    try {
      await unshareSong(songId);
      onShareChangeRef.current(songId, { enabled: false, url: null });
      onClose();
    } catch (err) {
      setView({ kind: "error", message: err instanceof Error ? err.message : "Couldn't turn sharing off." });
    }
  };

  return (
    <div className="share-modal-overlay" onClick={onClose}>
      <div
        className="share-modal"
        role="dialog"
        aria-modal="true"
        aria-label={view.kind === "notice" ? "Before you share" : "Share this song"}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="share-modal-header">
          <h2>{view.kind === "notice" ? "Before you share" : `Share "${title}"`}</h2>
          <button type="button" className="share-modal-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>
        {view.kind === "loading" ? (
          <p className="share-modal-text" role="status">Making the link…</p>
        ) : view.kind === "notice" ? (
          <>
            <p className="share-modal-text">
              Anyone with this link can listen to this song and see its score. They'll also see changes you make
              later. Share only music you have the right to share. The voices' terms of use apply to shared
              recordings.
            </p>
            <div className="share-modal-actions">
              <button type="button" className="btn-secondary-inline" onClick={onClose}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary-inline"
                disabled={view.accepting}
                onClick={() => {
                  setView({ kind: "notice", accepting: true });
                  void turnOn(true);
                }}
              >
                Share
              </button>
            </div>
          </>
        ) : view.kind === "error" ? (
          <p className="share-modal-text share-modal-error" role="alert">{view.message}</p>
        ) : (
          <>
            <p className="share-modal-text">Anyone with this link can listen and follow the score. They can't edit or download.</p>
            <div className="share-modal-link-row">
              <input
                className="share-modal-link"
                aria-label="Share link"
                readOnly
                value={view.url}
                onFocus={(event) => event.currentTarget.select()}
              />
              <button type="button" className="btn-primary-inline" onClick={() => void copy(view.url)}>
                {view.kind === "link" && view.copied ? "Copied" : "Copy"}
              </button>
            </div>
            {view.kind === "confirm-off" ? (
              <div className="share-modal-confirm" role="alertdialog" aria-label="Turn off sharing?">
                <p>Anyone with the link will see "This link was turned off". Sharing again makes a new link.</p>
                <div className="share-modal-actions">
                  <button
                    type="button"
                    className="btn-secondary-inline"
                    disabled={view.turningOff}
                    onClick={() => setView({ kind: "link", url: view.url, copied: false })}
                  >
                    Keep sharing
                  </button>
                  <button
                    type="button"
                    className="btn-secondary-inline danger"
                    disabled={view.turningOff}
                    onClick={() => void turnOff(view.url)}
                  >
                    {view.turningOff ? "Turning off…" : "Turn off"}
                  </button>
                </div>
              </div>
            ) : (
              <div className="share-modal-actions">
                <button
                  type="button"
                  className="btn-secondary-inline danger"
                  onClick={() => setView({ kind: "confirm-off", url: view.url, turningOff: false })}
                >
                  Turn off sharing
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
