// A background tab's notice: a short chime and a mark on the tab's title until the user
// comes back, as a chat site does when a message arrives while you're on another tab.

const TITLE_MARK = "● ";

let context: AudioContext | null = null;

// Browsers let a page play sound only after the user has interacted with it. The context
// is made on the first click or key press, so a later chime can play from a hidden tab.
function unlock() {
  if (context) return;
  try {
    context = new AudioContext();
  } catch {
    return;
  }
  window.removeEventListener("pointerdown", unlock, true);
  window.removeEventListener("keydown", unlock, true);
}

if (typeof window !== "undefined") {
  window.addEventListener("pointerdown", unlock, true);
  window.addEventListener("keydown", unlock, true);
}

function chime() {
  if (!context) return;
  const ctx = context;
  void ctx.resume().then(() => {
    // Two soft notes, a fifth apart.
    [880, 1318.5].forEach((frequency, index) => {
      const start = ctx.currentTime + index * 0.16;
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.18, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.35);
      oscillator.connect(gain).connect(ctx.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.4);
    });
  }).catch(() => {
    // No sound is allowed yet: the title mark still shows.
  });
}

function clearMark() {
  if (document.hidden) return;
  if (document.title.startsWith(TITLE_MARK)) document.title = document.title.slice(TITLE_MARK.length);
  document.removeEventListener("visibilitychange", clearMark);
}

/** Chime and mark the tab's title, only when the tab is hidden. Returns whether it did. */
export function noticeInBackground(): boolean {
  if (!document.hidden) return false;
  chime();
  if (!document.title.startsWith(TITLE_MARK)) document.title = TITLE_MARK + document.title;
  document.addEventListener("visibilitychange", clearMark);
  return true;
}
