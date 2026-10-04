import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./FeatureTour.css";

// Shown once: Skip or Done stores this, and the tour never opens again.
export const FEATURE_TOUR_DONE_KEY = "sightsinger.feature-tour-done";

type FeatureTourStep = {
  /** Matches a `data-tour` attribute on the control the step points at. */
  target: string;
  title: string;
  body: string;
};

export const FEATURE_TOUR_STEPS: FeatureTourStep[] = [
  {
    target: "solfege",
    title: "Solfege",
    body: "Choose how solfege is sung: movable-do (do follows the key) or fixed-do (do is always C), and the mode.",
  },
  {
    target: "repeats",
    title: "With Repeats",
    body: "Off by default: the next take sings the score as written. Turn it on to follow repeats, voltas, D.C. and D.S.",
  },
  {
    target: "chat-collapse",
    title: "Collapse the chat",
    body: "Hide the chat to give the score more room. Click again to bring it back.",
  },
  {
    target: "score-layout",
    title: "Page or horizontal view",
    body: "Read the score page by page, or as one continuous line.",
  },
  {
    target: "export-mix",
    title: "Download the mix",
    body: "Save your takes and instruments as one audio file. It records in real time, so keep this tab open until it finishes.",
  },
];

type Placement = { top: number; left: number; arrowLeft: number; side: "above" | "below" };

const GAP = 12;
const MARGIN = 12;

function tourDone(): boolean {
  try {
    return window.localStorage.getItem(FEATURE_TOUR_DONE_KEY) === "true";
  } catch {
    return false;
  }
}

function findTarget(step: FeatureTourStep): HTMLElement | null {
  const element = document.querySelector<HTMLElement>(`[data-tour="${step.target}"]`);
  if (!element) return null;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 ? element : null;
}

/** A short, one-time tour of the studio's controls: Next, Back and Skip. */
export default function FeatureTour() {
  const [open, setOpen] = useState(() => !tourDone());
  const [index, setIndex] = useState(0);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  // A step whose control is not on screen is passed over in the direction of travel.
  const directionRef = useRef<1 | -1>(1);

  const finish = useCallback(() => {
    try {
      window.localStorage.setItem(FEATURE_TOUR_DONE_KEY, "true");
    } catch {
      // The tour still closes; it may show again on the next visit.
    }
    setOpen(false);
  }, []);

  const go = useCallback(
    (direction: 1 | -1) => {
      const next = index + direction;
      if (next >= FEATURE_TOUR_STEPS.length) {
        finish();
        return;
      }
      directionRef.current = direction;
      setPlacement(null);
      setIndex(Math.max(0, next));
    },
    [finish, index]
  );

  useLayoutEffect(() => {
    if (!open) return;
    const step = FEATURE_TOUR_STEPS[index];
    const target = findTarget(step);
    if (!target) {
      const next = index + directionRef.current;
      if (next < 0 || next >= FEATURE_TOUR_STEPS.length) finish();
      else setIndex(next);
      return;
    }
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
    target.classList.add("feature-tour-target");

    const place = () => {
      const bubble = bubbleRef.current;
      if (!bubble) return;
      const rect = target.getBoundingClientRect();
      const width = bubble.offsetWidth;
      const height = bubble.offsetHeight;
      const below = rect.bottom + GAP + height <= window.innerHeight - MARGIN;
      const top = below ? rect.bottom + GAP : Math.max(MARGIN, rect.top - GAP - height);
      const center = rect.left + rect.width / 2;
      const left = Math.min(
        Math.max(MARGIN, center - width / 2),
        Math.max(MARGIN, window.innerWidth - MARGIN - width)
      );
      setPlacement({ top, left, arrowLeft: center - left, side: below ? "below" : "above" });
    };
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      target.classList.remove("feature-tour-target");
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [finish, index, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") finish();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finish, open]);

  if (!open || typeof document === "undefined") return null;
  const step = FEATURE_TOUR_STEPS[index];
  const last = index === FEATURE_TOUR_STEPS.length - 1;
  return createPortal(
    <div
      ref={bubbleRef}
      className="feature-tour-bubble"
      data-side={placement?.side ?? "below"}
      role="dialog"
      aria-label="Feature tour"
      data-testid="feature-tour"
      style={{
        top: placement?.top ?? 0,
        left: placement?.left ?? 0,
        visibility: placement ? "visible" : "hidden",
      }}
    >
      <span
        className="feature-tour-arrow"
        style={{ left: placement?.arrowLeft ?? 0 }}
        aria-hidden="true"
      />
      <span className="feature-tour-count">
        {index + 1} / {FEATURE_TOUR_STEPS.length}
      </span>
      <strong className="feature-tour-title">{step.title}</strong>
      <p className="feature-tour-body">{step.body}</p>
      <div className="feature-tour-actions">
        <button type="button" className="feature-tour-skip" onClick={finish}>
          Skip
        </button>
        <span className="feature-tour-spacer" />
        <button type="button" onClick={() => go(-1)} disabled={index === 0}>
          Back
        </button>
        <button type="button" className="feature-tour-next" onClick={() => go(1)}>
          {last ? "Done" : "Next"}
        </button>
      </div>
    </div>,
    document.body
  );
}
