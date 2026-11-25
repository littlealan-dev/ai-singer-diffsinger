import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import "./FeatureTour.css";

// Shown once: Skip or Done stores this, and the tour never opens again.
export const FEATURE_TOUR_DONE_KEY = "sightsinger.feature-tour-done";
const SING_ALL_TOUR_KEY = "sightsinger.sing-all-tour-done";

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
    body: "Change the solfege settings here. E.g. Movable-do or fixed-do.",
  },
  {
    target: "repeats",
    title: "With Repeats",
    body: "Turn it on to follow repeats, voltas, D.C., D.S. etc. Off by default.",
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

const SING_ALL_TOUR_STEPS: FeatureTourStep[] = [
  {
    target: "my-songs",
    title: "My songs",
    body: "Open your saved songs and pick up where you left off.",
  },
  {
    target: "share-song",
    title: "Share",
    body: "Create a link so others can listen and follow the score.",
  },
  {
    target: "sing-all",
    title: "Sing All",
    body: "Open Sing All to generate the vocal parts together.",
  },
  {
    target: "sing-all-customise",
    title: "Customise",
    body: "Choose the parts, AI voices and singing options for this run.",
  },
  {
    target: "sing-all-action",
    title: "Start singing",
    body: "Review the credit total, then choose Sing it. If your plan requires an upgrade, choose Upgrade to sing.",
  },
  {
    target: "sing-all-zip",
    title: "Download zip",
    body: "Download the completed vocal takes and any mixes from above the score preview.",
  },
];

type Placement = { top: number; left: number; arrowLeft: number; side: "above" | "below" };
type TourAnchor = { step: FeatureTourStep; element: HTMLElement };

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

function findVisibleAnchor(seen: string[]): TourAnchor | null {
  for (const step of SING_ALL_TOUR_STEPS) {
    if (seen.includes(step.target)) continue;
    for (const element of document.querySelectorAll<HTMLElement>(`[data-tour="${step.target}"]`)) {
      const rect = element.getBoundingClientRect();
      const left = Math.max(0, rect.left);
      const right = Math.min(window.innerWidth, rect.right);
      const top = Math.max(0, rect.top);
      const bottom = Math.min(window.innerHeight, rect.bottom);
      if (left >= right || top >= bottom) continue;
      const front = document
        .elementsFromPoint((left + right) / 2, (top + bottom) / 2)
        .find((node) => !node.closest(".feature-tour-bubble"));
      if (front && element.contains(front)) return { step, element };
    }
  }
  return null;
}

function measureBubble(target: HTMLElement, bubble: HTMLDivElement): Placement {
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
  return { top, left, arrowLeft: Math.min(Math.max(MARGIN, center - left), width - MARGIN), side: below ? "below" : "above" };
}

function TourBubble({
  step, placement, bubbleRef, label, header, children,
}: {
  step: FeatureTourStep;
  placement: Placement | null;
  bubbleRef: RefObject<HTMLDivElement>;
  label: string;
  header: ReactNode;
  children: ReactNode;
}) {
  return createPortal(
    <div
      ref={bubbleRef}
      className="feature-tour-bubble"
      data-side={placement?.side ?? "below"}
      role="dialog"
      aria-label={label}
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
      {header}
      <strong className="feature-tour-title">{step.title}</strong>
      <p className="feature-tour-body">{step.body}</p>
      <div className="feature-tour-actions">{children}</div>
    </div>,
    document.body
  );
}

function readSingAllTour(): string[] {
  try {
    const saved = window.localStorage.getItem(SING_ALL_TOUR_KEY);
    if (saved === "true") return SING_ALL_TOUR_STEPS.map(({ target }) => target);
    const seen = JSON.parse(saved ?? "[]");
    return Array.isArray(seen) ? SING_ALL_TOUR_STEPS.map(({ target }) => target).filter((id) => seen.includes(id)) : [];
  } catch {
    return [];
  }
}

export default function FeatureTour({ paused }: { paused: boolean }) {
  const [seen, setSeen] = useState(readSingAllTour);
  const [dismissed, setDismissed] = useState(false);
  // Undefined means the first scan has yet to decide which tour should open.
  const [anchor, setAnchor] = useState<TourAnchor | null>();
  const [placement, setPlacement] = useState<Placement | null>(null);
  const bubbleRef = useRef<HTMLDivElement>(null);
  const finished = seen.length === SING_ALL_TOUR_STEPS.length;
  const introSeen = SING_ALL_TOUR_STEPS.slice(0, 3).every(({ target }) => seen.includes(target));
  const active = !paused && anchor && !seen.includes(anchor.step.target) ? anchor : null;

  const acknowledge = useCallback((target?: string) => {
    const next = target
      ? [...new Set([...seen, ...readSingAllTour(), target])]
      : SING_ALL_TOUR_STEPS.map((step) => step.target);
    setSeen(next);
    if (!target) setDismissed(true);
    try {
      window.localStorage.setItem(SING_ALL_TOUR_KEY, next.length === SING_ALL_TOUR_STEPS.length ? "true" : JSON.stringify(next));
    } catch {
      // The current page can still advance the tips.
    }
  }, [seen]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === SING_ALL_TOUR_KEY) setSeen(readSingAllTour());
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  useLayoutEffect(() => {
    if (paused || finished) return;
    let frame = 0;
    let observed = new Set<HTMLElement>();
    const find = () => {
      frame = 0;
      const targets = new Set(document.querySelectorAll<HTMLElement>("[data-tour]"));
      for (const element of observed) {
        if (!targets.has(element)) visibility.unobserve(element);
      }
      for (const element of targets) {
        if (!observed.has(element)) visibility.observe(element);
      }
      observed = targets;
      const next = findVisibleAnchor(seen);
      setAnchor((current) => current?.element === next?.element ? current ?? null : next);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(find);
    };
    const visibility = new IntersectionObserver(schedule);
    const observer = new MutationObserver(schedule);
    find();
    // Only controls being added or removed: class changes, like the playback highlight's,
    // would rescan on every beat. Targets scrolling in or out come from `visibility`.
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", schedule);
    document.addEventListener("scroll", schedule, true);
    return () => {
      visibility.disconnect();
      observer.disconnect();
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      document.removeEventListener("scroll", schedule, true);
    };
  }, [finished, paused, seen]);

  useLayoutEffect(() => {
    if (!active) return;
    const { element, step } = active;
    element.classList.add("feature-tour-target");
    const place = () => {
      if (bubbleRef.current) setPlacement(measureBubble(element, bubbleRef.current));
    };
    const next = () => acknowledge(step.target);
    // Escape closes the tour only from inside the tip: Escape in a dialog or a rename is theirs.
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && event.target instanceof Node && bubbleRef.current?.contains(event.target)) {
        acknowledge();
      }
    };
    place();
    element.addEventListener("click", next);
    window.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      element.classList.remove("feature-tour-target");
      element.removeEventListener("click", next);
      window.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [acknowledge, active]);

  return (
    <>
      <LegacyFeatureTour paused={paused || dismissed || (!finished && (!introSeen || anchor !== null))} />
      {active && (
        <TourBubble
          step={active.step}
          placement={placement}
          bubbleRef={bubbleRef}
          label="Sing All and sharing tour"
          header={
            <button type="button" className="feature-tour-close" aria-label="Close tour" onClick={() => acknowledge()}>
              <X size={16} aria-hidden="true" />
            </button>
          }
        >
          <span className="feature-tour-spacer" />
          <button type="button" className="feature-tour-next" onClick={() => acknowledge(active.step.target)}>
            {seen.length === SING_ALL_TOUR_STEPS.length - 1 ? "Done" : "Next"}
          </button>
        </TourBubble>
      )}
    </>
  );
}

/** A short, one-time tour of the studio's controls: Next, Back and Skip. */
function LegacyFeatureTour({ paused }: { paused: boolean }) {
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
    if (!open || paused) return;
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
      setPlacement(measureBubble(target, bubble));
    };
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      target.classList.remove("feature-tour-target");
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [finish, index, open, paused]);

  useEffect(() => {
    if (!open || paused) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") finish();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finish, open, paused]);

  if (!open || paused || typeof document === "undefined") return null;
  const step = FEATURE_TOUR_STEPS[index];
  const last = index === FEATURE_TOUR_STEPS.length - 1;
  return (
    <TourBubble
      step={step}
      placement={placement}
      bubbleRef={bubbleRef}
      label="Feature tour"
      header={<span className="feature-tour-count">{index + 1} / {FEATURE_TOUR_STEPS.length}</span>}
    >
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
    </TourBubble>
  );
}
