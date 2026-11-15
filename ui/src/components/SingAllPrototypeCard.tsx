import { useEffect, useMemo, useRef, useState, type SyntheticEvent } from "react";
import clsx from "clsx";
import "./SingAllPrototypeCard.css";

// UI prototype only (docs/specs/sing-all-quote-ui-mockups.md): every part, voice,
// duration and price below is fake. Nothing here calls the API or reads a store.

type SingMode = "lyrics" | "solfege" | "both";
type MixChoice = "part" | "full" | "both";

type PartRow = {
  id: string;
  name: string;
  split: boolean;
  include: boolean;
  voice: string;
};


const FAKE_SCORE_TITLE = "Amazing Grace";
const FAKE_VOICES = ["Qixuan", "LIEE", "Printto", "Hoshino", "Apollo"];
// Whether the score has instrumental tracks; false hides the Instrumentals rows.
const FAKE_HAS_INSTRUMENTALS = true;
const FAKE_SECONDS = { withRepeats: 58.8, writtenOrder: 29.4 };
const FAKE_VERSES = 3;
const FAKE_BALANCE = 42;
const FAKE_INSTRUMENTAL_CREDITS = 1;
// Stand-in prices, so the total needs no "TBD": splitting is 1 credit per job and
// each mix file 1 credit.
const FAKE_SPLIT_CREDITS = 1;
const FAKE_MIX_CREDITS = 1;
// Hidden for screenshots (Alan, 2026-10-24); set to true to bring back the free-plan view.
const SHOW_QUICK_PLAN_TOGGLE = false;
const SECONDS_PER_VOCAL_CREDIT = 30;

const INITIAL_PARTS: PartRow[] = [
  { id: "s", name: "Soprano", split: true, include: true, voice: "Qixuan" },
  { id: "a", name: "Alto", split: true, include: true, voice: "LIEE" },
  { id: "t", name: "Tenor", split: true, include: true, voice: "Printto" },
  { id: "b", name: "Bass", split: true, include: false, voice: "Apollo" },
];

const MODE_LABEL: Record<SingMode, string> = { lyrics: "Lyrics", solfege: "Solfège", both: "Both" };

// Lyrics / solfège and the verse apply to the whole job, not to each part.
function modesOf(mode: SingMode): Array<"lyrics" | "solfege"> {
  if (mode === "both") return ["lyrics", "solfege"];
  return [mode];
}

function describeSinging(mode: SingMode, verse: number): string {
  return modesOf(mode)
    .map((sung) => (sung === "lyrics" ? `Lyrics (verse ${verse})` : "Solfège"))
    .join(" + ");
}

function describePart(part: PartRow): string {
  return part.include ? `${part.name} · ${part.voice}` : `${part.name} not included`;
}

// A superscript (i) that explains a term on hover, or on tap on touch screens.
// It sits inside pill buttons, so it is a focusable span, and a tap on it does not pick the pill.
function InfoTip({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  const toggle = (event: SyntheticEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setShow((current) => !current);
  };
  return (
    <span className="sing-all-info" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      <span
        role="button"
        tabIndex={0}
        className="sing-all-info-btn"
        aria-label="More information"
        aria-expanded={show}
        onClick={toggle}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") toggle(event);
        }}
      >
        i
      </span>
      {show && (
        <span role="tooltip" className="sing-all-info-tip">
          {text}
        </span>
      )}
    </span>
  );
}

// Quick mode's defaults (docs/specs/sing-all-quick-mode-proposal.md): the whole
// score, sung on words, verse 1 with repeats, one full mix, no style notes.
const QUICK_DEFAULTS = {
  parts: INITIAL_PARTS.map((part) => ({ ...part, include: true })),
  mode: "lyrics" as SingMode,
  verse: 1,
  mixes: "full" as MixChoice,
  withRepeats: true,
  styleNote: "",
};

function formatDuration(seconds: number): string {
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}
// The Sing All card: the defaults and the quote first, with the settings folded
// under Customise, inside the chat bubble so the score preview stays visible.
export default function SingAllPrototypeCard() {
  const [parts, setParts] = useState<PartRow[]>(QUICK_DEFAULTS.parts);
  const [mode, setMode] = useState<SingMode>(QUICK_DEFAULTS.mode);
  const [verse, setVerse] = useState(QUICK_DEFAULTS.verse);
  const [mixes, setMixes] = useState<MixChoice>(QUICK_DEFAULTS.mixes);
  const [withRepeats, setWithRepeats] = useState(QUICK_DEFAULTS.withRepeats);
  const [styleNote, setStyleNote] = useState(QUICK_DEFAULTS.styleNote);
  const [freePlan, setFreePlan] = useState(false);
  const [customising, setCustomising] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [totalChange, setTotalChange] = useState<number | null>(null);
  const previousTotal = useRef<number | null>(null);
  const [confirmed, setConfirmed] = useState(false);

  const updatePart = (id: string, patch: Partial<PartRow>) =>
    setParts((current) => current.map((part) => (part.id === id ? { ...part, ...patch } : part)));

  const quote = useMemo(() => {
    const included = parts.filter((part) => part.include);
    const seconds = withRepeats ? FAKE_SECONDS.withRepeats : FAKE_SECONDS.writtenOrder;
    const takes = included.flatMap((part) => modesOf(mode).map((sung) => ({ part, mode: sung })));
    const creditsPerTake = Math.ceil(seconds / SECONDS_PER_VOCAL_CREDIT);
    const vocalCredits = takes.length * creditsPerTake;
    const modesUsed = Array.from(new Set(takes.map((take) => take.mode)));
    const partDominant = mixes === "part" || mixes === "both" ? takes.length : 0;
    const fullMixes = mixes === "full" || mixes === "both" ? modesUsed.length : 0;
    const total = vocalCredits + (takes.length && FAKE_HAS_INSTRUMENTALS ? FAKE_INSTRUMENTAL_CREDITS : 0);
    const splits = included.filter((part) => part.split);
    const quickTotal =
      total + (splits.length ? FAKE_SPLIT_CREDITS : 0) + (partDominant + fullMixes) * FAKE_MIX_CREDITS;
    return {
      included,
      splits,
      seconds,
      takes,
      vocalCredits,
      modesUsed,
      partDominant,
      fullMixes,
      total,
      balanceAfter: FAKE_BALANCE - total,
      quickTotal,
      quickBalanceAfter: FAKE_BALANCE - quickTotal,
    };
  }, [parts, mode, mixes, withRepeats]);

  const shownTotal = quote.quickTotal;

  // In the customise state, a short-lived "+4 cr" badge shows what the last change cost.
  useEffect(() => {
    const previous = previousTotal.current;
    previousTotal.current = shownTotal;
    if (previous === null || previous === shownTotal) return;
    setTotalChange(shownTotal - previous);
    const timer = window.setTimeout(() => setTotalChange(null), 2500);
    return () => window.clearTimeout(timer);
  }, [shownTotal]);

  const totalLabel = `${quote.quickTotal} cr`;

  if (confirmed) {
    return (
      <div className="sing-all-card sing-all-confirmed" data-testid="sing-all-card">
        <div className="sing-all-title">Sing All · confirmed (prototype)</div>
        <p className="sing-all-muted">
          Nothing was sent: this is a UI prototype with fake data. {describeSinging(mode, verse)} ·{" "}
          {quote.takes.length} takes · {totalLabel}.
        </p>
        <ul className="sing-all-summary-list">
          {parts.map((part) => (
            <li key={part.id}>{describePart(part)}</li>
          ))}
        </ul>
      </div>
    );
  }

  const planToggle = (
    <label className="sing-all-radio sing-all-proto-toggle">
      <input type="checkbox" checked={freePlan} onChange={(event) => setFreePlan(event.target.checked)} />
      <span>Prototype: view as a free plan</span>
    </label>
  );

  const resetToDefaults = () => {
    setParts(QUICK_DEFAULTS.parts);
    setMode(QUICK_DEFAULTS.mode);
    setVerse(QUICK_DEFAULTS.verse);
    setMixes(QUICK_DEFAULTS.mixes);
    setWithRepeats(QUICK_DEFAULTS.withRepeats);
    setStyleNote(QUICK_DEFAULTS.styleNote);
  };
  const changedFromDefaults =
    JSON.stringify({ parts, mode, verse, mixes, withRepeats, styleNote }) !==
    JSON.stringify(QUICK_DEFAULTS);

  // Plain words: lyrics are the default and go unsaid; solfège and other mixes are named
  // only once chosen. Repeats are always stated, since they change the length.
  const partCount = quote.included.length;
  const singingWords = mode === "lyrics" ? null : mode === "solfege" ? "solfège (do-re-mi)" : "lyrics + solfège";
  const summaryBits = [
    `${partCount} vocal part${partCount === 1 ? "" : "s"}`,
    singingWords,
    mode !== "solfege" ? `verse ${verse} of ${FAKE_VERSES}` : null,
    withRepeats ? "with repeats" : "no repeats",
    mixes === "full" ? null : mixes === "part" ? "part-dominant mixes" : "full + part-dominant mixes",
    formatDuration(quote.seconds),
  ].filter(Boolean);
  const voicesLine = quote.included.map((part) => part.voice).join(", ");

  // Tasks in the order the job runs them: split, sing, instrumentals, mix.
  const quickQuote = (
    <table className="sing-all-table sing-all-quote-table sing-all-task-table">
      <thead>
        <tr>
          <th>Task</th>
          <th className="sing-all-num">Credits</th>
        </tr>
      </thead>
      <tbody>
        {quote.splits.length > 0 && (
          <tr>
            <td>
              Split the parts
              <InfoTip text="Some parts share a staff in the score; they are separated before singing." />
            </td>
            <td className="sing-all-num">{FAKE_SPLIT_CREDITS} cr</td>
          </tr>
        )}
        <tr>
          <td>
            Sing {partCount} part{partCount === 1 ? "" : "s"}
            <span className="sing-all-muted sing-all-inline-note">
              {" "}
              {formatDuration(quote.seconds)} each{mode === "both" ? ", on words and on do-re-mi" : ""}
            </span>
          </td>
          <td className="sing-all-num">{quote.vocalCredits} cr</td>
        </tr>
        {FAKE_HAS_INSTRUMENTALS && quote.takes.length > 0 && (
          <tr>
            <td>Instrumentals</td>
            <td className="sing-all-num">{FAKE_INSTRUMENTAL_CREDITS} cr</td>
          </tr>
        )}
        {quote.fullMixes > 0 && (
          <tr>
            <td>
              {quote.fullMixes === 1 ? "Full mix" : `Full mixes (${quote.fullMixes})`}
              <InfoTip text="Every part sung together, with the instrumentals: the whole song in one file." />
            </td>
            <td className="sing-all-num">{quote.fullMixes * FAKE_MIX_CREDITS} cr</td>
          </tr>
        )}
        {quote.partDominant > 0 && (
          <tr>
            <td>Part-dominant mixes ({quote.partDominant})</td>
            <td className="sing-all-num">{quote.partDominant * FAKE_MIX_CREDITS} cr</td>
          </tr>
        )}
        <tr className="sing-all-total">
          <td>
            Total <span className="sing-all-muted sing-all-inline-note">balance {FAKE_BALANCE} → {quote.quickBalanceAfter}</span>
          </td>
          <td className="sing-all-num">{totalLabel}</td>
        </tr>
      </tbody>
    </table>
  );

  const singItButton = (
    <button
      type="button"
      className="selection-send"
      disabled={!quote.takes.length}
      onClick={() => {
        if (freePlan) return;
        setConfirmed(true);
      }}
    >
      {freePlan ? "Upgrade to sing" : `Sing it · ${quote.quickTotal} cr`}
    </button>
  );

  // While Customise is open, Apply folds the card back to the summary; "Sing it" is
  // only on the summary, so the user always sees the quote before paying.
  const applyButton = (
    <button
      type="button"
      className="selection-send"
      onClick={() => {
        setCustomising(false);
        setDetailsOpen(false);
      }}
    >
      Apply
    </button>
  );

  const customiseRows = (
    <div className="sing-all-rows">
      <div className="sing-all-row-block">
        <div className="sing-all-row">
          <span className="sing-all-row-label">Parts & voices</span>
          <span className="sing-all-muted">
            {quote.included.length} of {parts.length} parts
          </span>
        </div>
        <div className="sing-all-parts-box">
          {parts.map((part) => (
            <div key={part.id} className={clsx("sing-all-part-line", !part.include && "sing-all-row-off")}>
              <label className="sing-all-part-name">
                <input
                  type="checkbox"
                  checked={part.include}
                  onChange={(event) => updatePart(part.id, { include: event.target.checked })}
                />
                {part.name}
              </label>
              <select
                className="selection-select sing-all-select"
                value={part.voice}
                disabled={!part.include}
                aria-label={`${part.name} voice`}
                onChange={(event) => updatePart(part.id, { voice: event.target.value })}
              >
                {FAKE_VOICES.map((voice) => (
                  <option key={voice} value={voice}>
                    {voice}
                  </option>
                ))}
              </select>
            </div>
          ))}
          {FAKE_HAS_INSTRUMENTALS && (
            <div className="sing-all-part-line sing-all-muted">Instrumentals · always included</div>
          )}
        </div>
      </div>

      <div className="sing-all-row">
        <span className="sing-all-row-label">Sing in</span>
        <div className="sing-all-segmented" role="group" aria-label="Sing in">
          {(["lyrics", "solfege", "both"] as SingMode[]).map((value) => (
            <button
              key={value}
              type="button"
              className={clsx(mode === value && "active")}
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
            >
              {MODE_LABEL[value]}
            </button>
          ))}
        </div>
      </div>

      <div className="sing-all-row">
        <span className="sing-all-row-label">Verse</span>
        {mode === "solfege" ? (
          <span className="sing-all-muted">— (solfège has no words)</span>
        ) : (
          <select
            className="selection-select sing-all-select"
            value={verse}
            aria-label="Verse"
            onChange={(event) => setVerse(Number(event.target.value))}
          >
            {Array.from({ length: FAKE_VERSES }, (_, index) => index + 1).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="sing-all-row">
        <span className="sing-all-row-label">Repeats</span>
        <span className="sing-all-row-value">
          <span className="sing-all-segmented" role="group" aria-label="Repeats">
            {(
              [
                [true, "On"],
                [false, "Off"],
              ] as Array<[boolean, string]>
            ).map(([value, label]) => (
              <button
                key={label}
                type="button"
                className={clsx(withRepeats === value && "active")}
                aria-pressed={withRepeats === value}
                onClick={() => setWithRepeats(value)}
              >
                {label}
              </button>
            ))}
          </span>
          <span className="sing-all-muted">{formatDuration(quote.seconds)}</span>
        </span>
      </div>

      <div className="sing-all-row sing-all-pill-row">
        <span className="sing-all-row-label">Mixes</span>
        <div className="sing-all-segmented" role="group" aria-label="Mixes">
          {(
            [
              ["full", "Full mix", "Every part sung together, with the instrumentals."],
              ["part", "Part-dominant", "One file per part: that part loud over the instrumentals, other voices muted."],
              ["both", "Both", null],
            ] as Array<[MixChoice, string, string | null]>
          ).map(([value, label, tip]) => (
            <button
              key={value}
              type="button"
              className={clsx(mixes === value && "active")}
              aria-pressed={mixes === value}
              onClick={() => setMixes(value)}
            >
              {label}
              {tip && <InfoTip text={tip} />}
            </button>
          ))}
        </div>
      </div>

      <div className="sing-all-row-block">
        <div className="sing-all-row">
          <span className="sing-all-row-label">Style notes</span>
          <span className="sing-all-muted">optional</span>
        </div>
        <textarea
          className="sing-all-style-input"
          rows={2}
          value={styleNote}
          placeholder='e.g. "Warm and soft. Bass: a strong, masculine voice."'
          onChange={(event) => setStyleNote(event.target.value)}
        />
      </div>
    </div>
  );

  // One card, two states. Summary: a summary line and the full quote. Customise: the same
  // card opens into compact setting rows, and the quote folds into a sticky total bar.
  return (
    <div className="sing-all-card sing-all-inline" data-testid="sing-all-card">
      <div className="sing-all-title">
        <span className="sing-all-step">Sing All</span>
        <span className="sing-all-badge">prototype</span>
      </div>
      {/* Customise always sits at the right of the song title; a long title wraps instead. */}
      <div className="sing-all-song-row">
        <h3 className="sing-all-step-title">{FAKE_SCORE_TITLE}</h3>
        <div className="sing-all-song-actions">
          {changedFromDefaults && (
            <button type="button" className="sing-all-link" onClick={resetToDefaults}>
              Reset
            </button>
          )}
          <button
            type="button"
            className="sing-all-link sing-all-summary-toggle"
            aria-expanded={customising}
            onClick={() => {
              setCustomising((current) => !current);
              setDetailsOpen(false);
            }}
          >
            {customising ? "▾" : "▸"} Customise
          </button>
        </div>
      </div>

      {customising ? (
        customiseRows
      ) : (
        <>
          <p className="sing-all-summary-line">{summaryBits.join(" · ")}</p>
          <p className="sing-all-muted">Voices: {voicesLine || "none"}</p>
          {quickQuote}
        </>
      )}

      {customising ? (
        <div className="sing-all-sticky-footer sing-all-total-bar">
          {detailsOpen && <div className="sing-all-details-tray">{quickQuote}</div>}
          <div className="sing-all-total-line">
            <span>
              Total <strong>{totalLabel}</strong>
              {totalChange !== null && (
                <span className={clsx("sing-all-change", totalChange < 0 && "down")}>
                  {totalChange > 0 ? "+" : "−"}
                  {Math.abs(totalChange)} cr
                </span>
              )}
            </span>
            <button
              type="button"
              className="sing-all-link"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((current) => !current)}
            >
              {detailsOpen ? "▾ hide details" : "▸ details"}
            </button>
            {applyButton}
          </div>
        </div>
      ) : (
        <>
          {SHOW_QUICK_PLAN_TOGGLE && planToggle}
          <div className="sing-all-actions">
            <span />
            {singItButton}
          </div>
        </>
      )}
    </div>
  );
}
