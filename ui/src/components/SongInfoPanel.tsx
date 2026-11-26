import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import {
  ApiError,
  fetchWorkspace,
  SONG_LICENSES,
  type SongInfoEdit,
  type SongInfoFields,
  type SongLicense,
  type SongRow,
  updateSongInfo,
} from "../api";
import { songInfoSaved } from "../analytics";
import { marketingUrl } from "../constants";
import "./SongInfoPanel.css";

const TEXT_FIELDS: Array<{ key: keyof SongInfoEdit; label: string; limit: number }> = [
  { key: "subtitle", label: "Subtitle", limit: 200 },
  { key: "composer", label: "Composer", limit: 200 },
  { key: "lyricist", label: "Lyrics by", limit: 200 },
  { key: "arranger", label: "Arranger", limit: 200 },
  { key: "credits", label: "Credits", limit: 500 },
  { key: "score_copyright", label: "Score copyright", limit: 300 },
  { key: "producer", label: "Produced by", limit: 120 },
];
const TITLE_LIMIT = 120;
const DESCRIPTION_LIMIT = 2000;

type Draft = Omit<SongInfoFields, "voices">;

interface SongInfoPanelProps {
  songId: string;
  /** The info the page has; the panel reads the Song again for the current voices. */
  info: SongInfoFields;
  onClose: () => void;
  onSaved: (row: SongRow) => void;
}

/** The Song's credits, license and description, which the Share page and the zip show. */
export default function SongInfoPanel({ songId, info, onClose, onSaved }: SongInfoPanelProps) {
  const [loaded, setLoaded] = useState<SongInfoFields>(info);
  const [draft, setDraft] = useState<Draft>(info);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const touchedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    fetchWorkspace(songId)
      .then((workspace) => {
        const fresh = workspace.song.info;
        if (cancelled || !fresh) return;
        setLoaded(fresh);
        // Keep what the owner has started typing; otherwise show the fresh values.
        if (!touchedRef.current) setDraft(fresh);
      })
      .catch(() => {
        // The page's copy stays.
      });
    return () => {
      cancelled = true;
    };
  }, [songId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const set = (key: keyof Draft, value: string) => {
    touchedRef.current = true;
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const save = async () => {
    const title = draft.title.trim();
    if (!title) {
      setError("A song needs a title.");
      return;
    }
    const changes: { title?: string; info?: SongInfoEdit } = {};
    if (title !== loaded.title) changes.title = title;
    const edits: SongInfoEdit = {};
    for (const { key } of TEXT_FIELDS) {
      if ((draft[key] ?? "") !== (loaded[key] ?? "")) (edits as Record<string, string>)[key] = (draft[key] as string) ?? "";
    }
    if (draft.description !== loaded.description) edits.description = draft.description;
    if (draft.license !== loaded.license) edits.license = draft.license;
    if (Object.keys(edits).length) changes.info = edits;
    if (!changes.title && !changes.info) {
      onClose();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = await updateSongInfo(songId, changes);
      songInfoSaved(
        Object.keys(edits).length + Number(changes.title !== undefined),
        saved.info?.license ?? draft.license,
        changes.title !== undefined,
        Boolean((saved.info?.description ?? draft.description).trim()),
      );
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiError && err.code === "invalid_song_info"
          ? "A field is too long. Shorten it and save again."
          : "Couldn't save the song info. Try again."
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="song-info-overlay" onClick={onClose}>
      <aside
        className="song-info-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Song info"
        data-testid="song-info-panel"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="song-info-header">
          <h2>Song info</h2>
          <button type="button" className="song-info-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>
        <form
          className="song-info-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label className="song-info-field">
            <span>Title</span>
            <input value={draft.title} maxLength={TITLE_LIMIT} onChange={(event) => set("title", event.target.value)} />
          </label>
          {TEXT_FIELDS.map(({ key, label, limit }) => (
            <label key={key} className="song-info-field">
              <span>{label}</span>
              <input
                value={(draft[key] as string | undefined) ?? ""}
                maxLength={limit}
                onChange={(event) => set(key, event.target.value)}
              />
            </label>
          ))}
          <div className="song-info-field">
            <span>Voices</span>
            <p className="song-info-voices" data-testid="song-info-voices">
              {loaded.voices.length ? loaded.voices.join(", ") : "No sung parts yet"}
              {" · "}
              <a href={marketingUrl("/credits-and-ai-voices")} target="_blank" rel="noreferrer">
                Credits and AI voices
              </a>
            </p>
          </div>
          <div className="song-info-field">
            <label className="song-info-field">
              <span>License</span>
              <select
                className="selection-select"
                value={draft.license}
                aria-describedby="song-info-license-note"
                onChange={(event) => set("license", event.target.value as SongLicense)}
              >
                {(Object.keys(SONG_LICENSES) as SongLicense[]).map((value) => (
                  <option key={value} value={value}>
                    {SONG_LICENSES[value].label}
                  </option>
                ))}
              </select>
            </label>
            <small id="song-info-license-note">
              This license covers your recording. The song itself keeps its own copyright.
            </small>
          </div>
          <div className="song-info-field">
            <label className="song-info-field">
              <span>Description</span>
              <textarea
                rows={5}
                value={draft.description}
                maxLength={DESCRIPTION_LIMIT}
                onChange={(event) => set("description", event.target.value)}
              />
            </label>
            <small className="song-info-count" aria-live="polite">
              {draft.description.length} / {DESCRIPTION_LIMIT}
            </small>
          </div>
          {error ? (
            <p className="song-info-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="song-info-actions">
            <button type="button" className="btn-secondary-inline" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="selection-send" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </aside>
    </div>
  );
}
