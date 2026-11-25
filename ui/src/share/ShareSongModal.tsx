import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError, fetchSongInfo, shareSong, SONG_LICENSES, type SongInfoFields, unshareSong } from "../api";
import { shareLinkCopied, shareLinkCreated, shareTurnedOff } from "../analytics";
import "./ShareSongModal.css";

// The API does not return a creation timestamp for existing links.
const createdLinks = new Map<string, number>();

export type ShareState = { enabled: boolean; url: string | null };

interface ShareSongModalProps {
  songId: string;
  title: string;
  /** The link, when the Song's sharing is already on: the box opens on it without a request. */
  url: string | null;
  /** The drawer can know a link exists before it has obtained its URL. */
  alreadyShared?: boolean;
  /** Info the open Song already has, while the dialog reads the current values. */
  initialInfo?: SongInfoFields;
  onClose: () => void;
  onShareChange: (songId: string, share: ShareState) => void;
  /** Opens the Song info panel; given only for the Song open on the page. */
  onEditInfo?: () => void;
}

/**
 * Why a Song's Share button is off, for its tooltip, or null when it can be shared.
 * A Song with a live link can always open it, to copy the link or turn it off.
 */
export function shareBlockedReason(
  song: { share?: { available: boolean; enabled: boolean } | null; hasSungTrack: boolean } | null
): string | null {
  if (!song) return "Upload a score and sing a part to share it";
  if (song.share && !song.share.available) return "Sharing isn't available for this account";
  if (song.share?.enabled) return null;
  if (!song.hasSungTrack) return "Sing a part to share this song";
  // The open Song's share state is read once it has a sung part.
  if (!song.share) return "Checking sharing…";
  return null;
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
export default function ShareSongModal({
  songId, title, url, alreadyShared = Boolean(url), initialInfo, onClose, onShareChange, onEditInfo,
}: ShareSongModalProps) {
  const [view, setView] = useState<View>(url ? { kind: "link", url, copied: false } : { kind: "loading" });
  const [songInfo, setSongInfo] = useState<{ songId: string; info: SongInfoFields | null } | null>(null);
  const linkReady = view.kind === "link" || view.kind === "confirm-off";
  const fetchedInfo = songInfo?.songId === songId ? songInfo : null;
  const info = fetchedInfo?.info ?? initialInfo;
  const onShareChangeRef = useRef(onShareChange);
  onShareChangeRef.current = onShareChange;

  const turnOn = async (noticeAccepted: boolean) => {
    try {
      const result = await shareSong(songId, noticeAccepted);
      if (!result.url) throw new Error("The share link is missing.");
      if (!alreadyShared && !createdLinks.has(result.url)) {
        createdLinks.set(result.url, Date.now());
        shareLinkCreated(noticeAccepted);
      }
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

  useEffect(() => {
    if (!linkReady) return;
    let cancelled = false;
    void fetchSongInfo(songId)
      .then((fresh) => {
        if (!cancelled) setSongInfo({ songId, info: fresh });
      })
      .catch(() => {
        if (!cancelled) setSongInfo({ songId, info: null });
      });
    return () => {
      cancelled = true;
    };
  }, [linkReady, songId]);

  const copy = async (link: string) => {
    shareLinkCopied("dialog");
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
      const createdAt = createdLinks.get(link);
      createdLinks.delete(link);
      shareTurnedOff(createdAt === undefined ? undefined : (Date.now() - createdAt) / 1000);
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
            <section className="share-modal-info" aria-label="Song info">
              <div className="share-modal-info-header">
                <h3>Song info</h3>
                {onEditInfo && (
                  <button type="button" className="btn-secondary-inline" onClick={onEditInfo}>
                    Edit
                  </button>
                )}
              </div>
              {info ? (
                <ReadOnlySongInfo info={info} />
              ) : (
                <p className="share-modal-text" role="status">
                  {fetchedInfo ? "Song info is unavailable." : "Loading song info…"}
                </p>
              )}
            </section>
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

function ReadOnlySongInfo({ info }: { info: SongInfoFields }) {
  const license = SONG_LICENSES[info.license] ?? SONG_LICENSES.all_rights_reserved;
  const fields = [
    ["Title", info.title],
    ["Subtitle", info.subtitle],
    ["Composer", info.composer],
    ["Lyrics by", info.lyricist],
    ["Arranger", info.arranger],
    ["Credits", info.credits],
    ["Score copyright", info.score_copyright],
    ["Produced by", info.producer],
    ["Voices", info.voices.join(", ") || "No sung parts yet"],
    ["License", license.label],
    ["Description", info.description],
  ];

  return (
    <dl className="share-modal-info-list">
      {fields.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value || "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
