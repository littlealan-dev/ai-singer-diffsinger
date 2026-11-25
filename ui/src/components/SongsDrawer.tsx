import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError, deleteSong, listSongs, type SongRow } from "../api";
import ShareSongModal, { shareBlockedReason, type ShareState } from "../share/ShareSongModal";
import "./SongsDrawer.css";

interface SongsDrawerProps {
  open: boolean;
  onClose: () => void;
  /** The Song shown in the page, if any. */
  currentSongId: string | null;
  /** False while the page can't leave its Song: a reply is being awaited. */
  canOpen: boolean;
  onOpen: (songId: string) => void;
  onDeleted: (songId: string) => void;
  /** Whether this user can share. */
  shareAvailable: boolean;
  onShareChange: (songId: string, share: ShareState) => void;
}

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function lastActivityLabel(isoTime: string, now: number): string {
  const time = Date.parse(isoTime);
  if (Number.isNaN(time)) return "";
  const elapsed = Math.max(0, now - time);
  if (elapsed < MINUTE_MS) return "Just now";
  if (elapsed < HOUR_MS) return `${Math.floor(elapsed / MINUTE_MS)} min ago`;
  if (elapsed < DAY_MS) {
    const hours = Math.floor(elapsed / HOUR_MS);
    return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  }
  if (elapsed < 7 * DAY_MS) {
    const days = Math.floor(elapsed / DAY_MS);
    return `${days} ${days === 1 ? "day" : "days"} ago`;
  }
  return new Date(time).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function sungPartsLabel(count: number): string {
  if (count <= 0) return "No sung parts yet";
  return `${count} sung ${count === 1 ? "part" : "parts"}`;
}

/** The user's Songs, newest first, each with Open and Delete. */
export default function SongsDrawer({
  open,
  onClose,
  currentSongId,
  canOpen,
  onOpen,
  onDeleted,
  shareAvailable,
  onShareChange,
}: SongsDrawerProps) {
  const [songs, setSongs] = useState<SongRow[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [rowNotice, setRowNotice] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [sharing, setSharing] = useState<SongRow | null>(null);
  const requestRef = useRef(0);
  // The parent re-renders often; the list is loaded once per opening all the same.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const refresh = useCallback(async () => {
    const request = ++requestRef.current;
    setListError(null);
    try {
      const rows = await listSongs();
      if (requestRef.current === request) setSongs(rows);
    } catch (err: any) {
      if (requestRef.current === request) setListError(err?.message || "Couldn't load your songs.");
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setSongs(null);
    setRowNotice(null);
    setConfirmingId(null);
    void refresh();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      requestRef.current += 1;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, refresh]);

  const confirmDelete = async (song: SongRow) => {
    setConfirmingId(null);
    setRowNotice(null);
    setDeletingId(song.id);
    try {
      await deleteSong(song.id);
      setSongs((current) => current?.filter((row) => row.id !== song.id) ?? current);
      onDeleted(song.id);
    } catch (err: any) {
      if (err instanceof ApiError && err.code === "work_running") {
        setRowNotice(`"${song.title}" is still working, so it can't be deleted yet.`);
        void refresh();
      } else {
        setRowNotice(err?.message || "Couldn't delete the song. Please try again.");
      }
    } finally {
      setDeletingId(null);
    }
  };

  if (!open) return null;
  const now = Date.now();

  return (
    <div className="songs-drawer-overlay" onClick={onClose}>
      <aside
        className="songs-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="My songs"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="songs-drawer-header">
          <h2>My songs</h2>
          <button type="button" className="songs-drawer-close" onClick={onClose} aria-label="Close My songs">
            <X size={18} />
          </button>
        </header>
        {rowNotice ? (
          <p className="songs-drawer-notice" role="alert">{rowNotice}</p>
        ) : null}
        {listError ? (
          <div className="songs-drawer-empty" role="alert">
            <p>{listError}</p>
            <button type="button" className="btn-secondary-inline" onClick={() => void refresh()}>
              Try again
            </button>
          </div>
        ) : songs === null ? (
          <p className="songs-drawer-empty" role="status">Loading your songs…</p>
        ) : songs.length === 0 ? (
          <p className="songs-drawer-empty">No songs yet. Upload a score or load a demo song to start one.</p>
        ) : (
          <ul className="songs-drawer-list">
            {songs.map((song) => {
              const working = song.status === "working";
              const isCurrent = song.id === currentSongId;
              const shareBlocked = shareBlockedReason({
                share: { available: shareAvailable, enabled: Boolean(song.shared) },
                hasSungTrack: song.track_count > 0,
              });
              return (
                <li key={song.id} className="songs-drawer-row" data-testid="song-row" aria-current={isCurrent || undefined}>
                  <div className="songs-drawer-row-copy">
                    <span className="songs-drawer-row-title">{song.title}</span>
                    <span className="songs-drawer-row-meta">
                      {working ? <span className="songs-drawer-working">Working…</span> : null}
                      <span>{lastActivityLabel(song.last_active_at, now)}</span>
                      <span>{sungPartsLabel(song.track_count)}</span>
                      {song.shared ? <span className="songs-drawer-shared">Shared</span> : null}
                    </span>
                  </div>
                  {confirmingId === song.id ? (
                    <div className="songs-drawer-row-actions confirm">
                      <span className="songs-drawer-confirm-text">Delete this song and its takes?</span>
                      <button type="button" className="btn-secondary-inline danger" onClick={() => void confirmDelete(song)}>
                        Delete
                      </button>
                      <button type="button" className="btn-secondary-inline" onClick={() => setConfirmingId(null)}>
                        Keep
                      </button>
                    </div>
                  ) : (
                    <div className="songs-drawer-row-actions">
                      {isCurrent ? (
                        <span className="songs-drawer-open-now">Open now</span>
                      ) : (
                        <button
                          type="button"
                          className="btn-secondary-inline"
                          disabled={!canOpen}
                          title={canOpen ? undefined : "Wait for the reply first"}
                          onClick={() => onOpen(song.id)}
                        >
                          Open
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn-secondary-inline"
                        disabled={shareBlocked !== null}
                        title={shareBlocked ?? undefined}
                        onClick={() => setSharing(song)}
                      >
                        Share
                      </button>
                      <button
                        type="button"
                        className="btn-secondary-inline"
                        disabled={working || deletingId === song.id}
                        title={working ? "This song is still working" : undefined}
                        onClick={() => {
                          setRowNotice(null);
                          setConfirmingId(song.id);
                        }}
                      >
                        {deletingId === song.id ? "Deleting…" : "Delete"}
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </aside>
      {sharing ? (
        <div onClick={(event) => event.stopPropagation()}>
          <ShareSongModal
            songId={sharing.id}
            title={sharing.title}
            url={null}
            onClose={() => setSharing(null)}
            onShareChange={(songId, share) => {
              setSongs((current) =>
                current?.map((row) => (row.id === songId ? { ...row, shared: share.enabled } : row)) ?? current
              );
              onShareChange(songId, share);
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
