import { useEffect, useRef } from "react";
import "./NewSongOrReplaceModal.css";

interface NewSongOrReplaceModalProps {
  /** The score waiting for the choice. */
  scoreName: string;
  /** The open Song has work running, which Replace would stop. */
  working: boolean;
  /** The running work includes a Sing All, which Replace stops the same way as its Stop. */
  singAllRunning?: boolean;
  onNewSong: () => void;
  onReplace: () => void;
  onCancel: () => void;
}

/** Asked when a score is added to a Song that already has one. */
export default function NewSongOrReplaceModal({
  scoreName,
  working,
  singAllRunning = false,
  onNewSong,
  onReplace,
  onCancel,
}: NewSongOrReplaceModalProps) {
  const newSongButtonRef = useRef<HTMLButtonElement | null>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  useEffect(() => {
    // New song is the default: Enter chooses it.
    newSongButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancelRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="song-choice-overlay" onClick={onCancel}>
      <div
        className="song-choice-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="song-choice-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="song-choice-title">New song or replace?</h2>
        <p className="song-choice-file">{scoreName}</p>
        <div className="song-choice-options">
          <button
            type="button"
            ref={newSongButtonRef}
            className="song-choice-option primary"
            onClick={onNewSong}
          >
            <span className="song-choice-option-name">New song</span>
            <span className="song-choice-option-detail">
              {working
                ? "Open this score as a new song. This song stays as it is, and its work keeps running."
                : "Open this score as a new song. This song stays as it is."}
            </span>
          </button>
          <button type="button" className="song-choice-option" onClick={onReplace}>
            <span className="song-choice-option-name">Replace</span>
            <span className="song-choice-option-detail">
              {singAllRunning
                ? "Put this score in this song. The chat stays, the player's tracks are cleared, and Sing All is stopped. Parts already sung are kept and charged. The rest is refunded."
                : working
                  ? "Put this score in this song. The chat stays, the player's tracks are cleared, and the running work is stopped."
                  : "Put this score in this song. The chat stays and the player's tracks are cleared."}
            </span>
          </button>
        </div>
        <button type="button" className="song-choice-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
