import { useCallback, useEffect, useMemo, useRef, useState, type SyntheticEvent } from "react";
import clsx from "clsx";
import {
  ApiError,
  fetchSingAllDownload,
  isSingAllQuote,
  requestSingAllQuote,
  startSingAll,
  type SingAllChoices,
  type SingAllControls,
  type SingAllMixes,
  type SingAllMode,
  type SingAllNote,
  type SingAllProgress,
  type SingAllQuote,
  type SingAllQuoteBlocked,
  type SingAllRow,
} from "../api";
import "./SingAllCard.css";

// The Sing All card (docs/specs/sing-all-core-step3-spec.md, S3, and its API contract).
// Before "Sing it" it shows a quote from POST /sing-all/quote; after, the job's progress,
// which the page keeps up to date and passes in.

type SingAllCardProps = {
  songId: string;
  songTitle: string;
  // From the chat reply that opened the card. Absent for a card shown from the history.
  open?: { language: string; solfegeOnly: boolean };
  // Set once "Sing it" has started a job, or for a card shown from the history.
  jobId?: string | null;
  job?: SingAllProgress | null;
  onStarted: (jobId: string, progress: SingAllProgress | null) => void;
  onLoadJob: (jobId: string) => void;
  onOpenSong: (songId: string) => void;
  onTopUp: () => void;
  onUpgrade: () => void;
  onStop: (songId: string) => Promise<void>;
};

const MODE_LABEL: Record<SingAllMode, string> = { lyrics: "Lyrics", solfege: "Solfège", both: "Both" };
const CONTROL_LABEL: Record<string, string> = {
  airiness: "airiness",
  clarity: "clarity",
  gender: "gender",
  intensity: "intensity",
};

function formatDuration(seconds: number): string {
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

function formatMinutes(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return `${minutes} min`;
}

function signed(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded)}`;
}

function ordinal(value: number): string {
  const tens = value % 100;
  if (tens >= 11 && tens <= 13) return `${value}th`;
  return `${value}${{ 1: "st", 2: "nd", 3: "rd" }[value % 10] ?? "th"}`;
}

export function languageName(code: string, label?: string | null): string {
  if (label) return label;
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

export function noteText(note: SingAllNote): string {
  const part = note.partName || "This part";
  switch (note.code) {
    case "cannot_split_without_lyrics":
      return `This score has no lyrics, so ${part}'s shared-staff parts can't be split yet.`;
    case "cannot_split_evidence_too_large":
      return `${part} is too long to split here, so it isn't included.`;
    case "no_sound_chosen":
      return `${part} has no instrument yet: choose one in the player.`;
    case "repeats_written_order":
      return "The repeats can't be followed, so it's sung in written order.";
    default:
      return part === "This part" ? "Something in this score needs a look." : `${part} needs a look.`;
  }
}

function rowNote(row: SingAllRow): string | null {
  switch (row.note) {
    case "no_words":
      return `${row.name} has no words, so it's sung in solfège (do-re-mi).`;
    case "likely_instrument":
      return "looks like an instrument";
    case "undecided":
      return "not sure this is a voice part";
    case "voice_out_of_range":
      return "this voice may not reach every note";
    default:
      return null;
  }
}

function styleLine(changes: SingAllControls): string {
  const bits: string[] = [];
  if (changes.voiceColor) bits.push(changes.voiceColor);
  for (const name of ["airiness", "clarity", "gender", "intensity"] as const) {
    const value = changes[name];
    if (typeof value === "number" && value !== 0) bits.push(`${CONTROL_LABEL[name]} ${signed(value)}`);
  }
  return bits.join(" · ");
}

// A superscript (i) that explains a term on hover, or on tap on touch screens.
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

type QuoteState =
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "blocked"; blocked: SingAllQuoteBlocked }
  | { kind: "ready"; quote: SingAllQuote };

export default function SingAllCard(props: SingAllCardProps) {
  if (props.jobId) {
    return <SingAllJobView {...props} jobId={props.jobId} />;
  }
  return <SingAllQuoteView {...props} />;
}

function SingAllQuoteView({
  songId,
  songTitle,
  open,
  onStarted,
  onOpenSong,
  onTopUp,
  onUpgrade,
}: SingAllCardProps) {
  const defaults = useMemo<SingAllChoices>(
    () => ({ language: open?.language ?? "en", solfegeOnly: open?.solfegeOnly ?? false }),
    [open?.language, open?.solfegeOnly]
  );
  const [choices, setChoices] = useState<SingAllChoices>(defaults);
  const [state, setState] = useState<QuoteState>({ kind: "loading" });
  const [pending, setPending] = useState(false);
  const [customising, setCustomising] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [styleDraft, setStyleDraft] = useState("");
  const [appliedNote, setAppliedNote] = useState("");
  const [quoteChangedLine, setQuoteChangedLine] = useState(false);
  // Whether the user changed anything since the defaults: it shows "Reset".
  const [userChanged, setUserChanged] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [totalChange, setTotalChange] = useState<number | null>(null);
  const previousTotal = useRef<number | null>(null);
  const requestSeq = useRef(0);

  // Every change asks for a new quote; the last one asked for wins.
  const askQuote = useCallback(
    async (next: SingAllChoices) => {
      const seq = ++requestSeq.current;
      setChoices(next);
      setPending(true);
      try {
        const response = await requestSingAllQuote(songId, next);
        if (seq !== requestSeq.current) return;
        setState(isSingAllQuote(response) ? { kind: "ready", quote: response } : { kind: "blocked", blocked: response });
      } catch {
        if (seq !== requestSeq.current) return;
        setState({ kind: "failed" });
      } finally {
        if (seq === requestSeq.current) setPending(false);
      }
    },
    [songId]
  );

  useEffect(() => {
    void askQuote(defaults);
  }, [askQuote, defaults]);

  const quote = state.kind === "ready" ? state.quote : null;
  const shownTotal = quote?.total ?? null;
  useEffect(() => {
    const previous = previousTotal.current;
    previousTotal.current = shownTotal;
    if (previous === null || shownTotal === null || previous === shownTotal) return;
    setTotalChange(shownTotal - previous);
    const timer = window.setTimeout(() => setTotalChange(null), 2500);
    return () => window.clearTimeout(timer);
  }, [shownTotal]);

  // A new quote for the rows and options as quoted, with this change, and no style note
  // unless the patch has one (only "Apply" sends it). The card shows the change at once.
  const change = (patch: Partial<SingAllChoices>, { quoteChanged = false } = {}) => {
    if (!quote) return;
    setQuoteChangedLine(quoteChanged);
    const { rows, styleNote: _note, ...options } = patch;
    setState({
      kind: "ready",
      quote: {
        ...quote,
        options: { ...quote.options, ...options },
        rows: rows
          ? quote.rows.map((row) => ({ ...row, ...rows.find((picked) => picked.rowId === row.rowId) }))
          : quote.rows,
      },
    });
    void askQuote({
      language: quote.options.language,
      solfegeOnly: quote.options.solfegeOnly,
      mode: quote.options.mode,
      verse: quote.options.verse,
      repeats: quote.options.repeats,
      ...(quote.options.mixes ? { mixes: quote.options.mixes } : {}),
      rows: quote.rows.map(({ rowId, include, voice, voicePicked }) => ({ rowId, include, voice, voicePicked })),
      ...patch,
    });
  };
  const customise = (patch: Partial<SingAllChoices>) => {
    setUserChanged(true);
    change(patch);
  };
  const changeRow = (rowId: string, patch: Partial<SingAllRow>) => {
    if (!quote) return;
    customise({
      rows: quote.rows.map(({ rowId: id, include, voice, voicePicked }) =>
        id === rowId ? { rowId: id, include, voice, voicePicked, ...patch } : { rowId: id, include, voice, voicePicked }
      ),
    });
  };
  const apply = () => {
    setCustomising(false);
    setDetailsOpen(false);
    const note = styleDraft.trim();
    setAppliedNote(note);
    if (note) setUserChanged(true);
    if (quote) change({ styleNote: note || undefined });
  };
  const reset = () => {
    setStyleDraft("");
    setAppliedNote("");
    setQuoteChangedLine(false);
    setUserChanged(false);
    void askQuote(defaults);
  };

  const singIt = async () => {
    if (!quote || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const progress = await startSingAll(quote.quoteId);
      // Until the S3 backend lands, the answer is the job record; only its id is relied on.
      onStarted(progress.jobId, "counts" in progress ? progress : null);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : undefined;
      if (code === "quote_changed") {
        change({ styleNote: appliedNote || undefined }, { quoteChanged: true });
      } else if (code === "sing_all_already_running" && err instanceof ApiError) {
        setState({
          kind: "blocked",
          blocked: {
            blocked: "already_running",
            activeJobId: String(err.detail.activeJobId ?? ""),
            activeSongId: String(err.detail.activeSongId ?? ""),
          },
        });
      } else if (code === "not_enough_credits" || code === "free_plan" || code === "nothing_to_sing" || code === "style_unavailable") {
        change({ styleNote: appliedNote || undefined });
      } else {
        setStartError("Couldn't start. Try again.");
      }
    } finally {
      setStarting(false);
    }
  };

  const header = (
    <>
      <div className="sing-all-title">
        <span className="sing-all-step">Sing All</span>
      </div>
      <div className="sing-all-song-row">
        <h3 className="sing-all-step-title">{songTitle}</h3>
        {quote && (
          <div className="sing-all-song-actions">
            {userChanged && (
              <button type="button" className="sing-all-link" onClick={reset}>
                Reset
              </button>
            )}
            <button
              type="button"
              className="sing-all-link sing-all-summary-toggle"
              aria-expanded={customising}
              onClick={() => {
                if (!customising) setStyleDraft(appliedNote);
                setCustomising((current) => !current);
                setDetailsOpen(false);
              }}
            >
              {customising ? "▾" : "▸"} Customise
            </button>
          </div>
        )}
      </div>
    </>
  );

  if (state.kind === "loading") {
    return (
      <div className="sing-all-card sing-all-inline" data-testid="sing-all-card">
        {header}
        <p className="sing-all-muted" role="status">
          Reading the score…
        </p>
      </div>
    );
  }
  if (state.kind === "failed") {
    return (
      <div className="sing-all-card sing-all-inline" data-testid="sing-all-card">
        {header}
        <p>The quote couldn't be made.</p>
        <div className="sing-all-actions">
          <span />
          <button type="button" className="selection-send" disabled={pending} onClick={() => void askQuote(choices)}>
            Try again
          </button>
        </div>
      </div>
    );
  }
  if (state.kind === "blocked") {
    const blocked = state.blocked;
    return (
      <div className="sing-all-card sing-all-inline" data-testid="sing-all-card">
        {header}
        {blocked.blocked === "already_running" ? (
          <>
            <p>A Sing All is already running. Only one can run at a time.</p>
            {blocked.activeSongId && blocked.activeSongId !== songId && (
              <div className="sing-all-actions">
                <span />
                <button type="button" className="selection-send" onClick={() => onOpenSong(blocked.activeSongId)}>
                  Open its song
                </button>
              </div>
            )}
          </>
        ) : (
          <p>This language can't be sung now. Ask in the chat to start Sing All again.</p>
        )}
      </div>
    );
  }

  const q = state.quote;
  const voiceNames = new Map(q.voices.map((voice) => [voice.id, voice.name]));
  const voiceName = (id: string) => voiceNames.get(id) ?? id;
  const included = q.rows.filter((row) => row.include);
  const singing = q.lines.find((line) => line.kind === "singing");
  const seconds = singing && singing.kind === "singing" ? singing.secondsEach : 0;
  const balanceLine = q.lines.find((line) => line.kind === "balance");
  const languageLabel = languageName(
    q.options.language,
    q.options.languages.find((language) => language.code === q.options.language)?.label
  );
  const verseCount = q.options.verses.length;
  const summaryBits = [
    `${included.length} vocal part${included.length === 1 ? "" : "s"}`,
    q.options.mode === "lyrics" ? null : q.options.mode === "solfege" ? "solfège (do-re-mi)" : "lyrics + solfège",
    languageLabel,
    q.options.mode !== "solfege" && verseCount > 1 ? `verse ${q.options.verse} of ${verseCount}` : null,
    q.options.repeats ? "with repeats" : "no repeats",
    seconds ? formatDuration(seconds) : null,
  ].filter(Boolean);
  const notEnough = q.blocked === "not_enough_credits";
  const rowNotes = included.map((row) => (row.note === "no_words" ? rowNote(row) : null)).filter(Boolean);
  const styleRows = included
    .map((row) => ({ row, line: q.style.changes[row.rowId] ? styleLine(q.style.changes[row.rowId]) : "" }))
    .filter(({ line }) => line);

  const quoteTable = (
    <table className="sing-all-table sing-all-quote-table sing-all-task-table">
      <thead>
        <tr>
          <th>Task</th>
          <th className="sing-all-num">Credits</th>
        </tr>
      </thead>
      <tbody>
        {q.lines.map((line) => {
          switch (line.kind) {
            case "preparing":
              return (
                <tr key="preparing">
                  <td>
                    Split the parts
                    <InfoTip text="Some parts share a staff in the score; they are separated before singing." />
                  </td>
                  <td className="sing-all-num">included</td>
                </tr>
              );
            case "singing":
              return (
                <tr key="singing">
                  <td>
                    Sing {line.parts} part{line.parts === 1 ? "" : "s"}
                    <span className="sing-all-muted sing-all-inline-note">
                      {" "}
                      {formatDuration(line.secondsEach)} each
                      {q.options.mode === "both" ? ", on words and on do-re-mi" : ""}
                    </span>
                  </td>
                  <td className="sing-all-num">{line.credits} cr</td>
                </tr>
              );
            case "instrumentals":
              return (
                <tr key="instrumentals">
                  <td>Instrumentals</td>
                  <td className="sing-all-num">{line.credits} cr</td>
                </tr>
              );
            case "mixes":
              return (
                <tr key="mixes">
                  <td>
                    Mixes
                    <span className="sing-all-muted sing-all-inline-note">
                      {" "}
                      {line.files} file{line.files === 1 ? "" : "s"}
                    </span>
                    <InfoTip text="Balanced MP3 mixes of the sung parts, in the zip you download when it's done." />
                  </td>
                  <td className="sing-all-num">{line.credits} cr</td>
                </tr>
              );
            case "total":
              return (
                <tr key="total" className={clsx("sing-all-total", notEnough && "sing-all-short")}>
                  <td>
                    Total{" "}
                    {balanceLine && balanceLine.kind === "balance" && (
                      <span className="sing-all-muted sing-all-inline-note">
                        balance {balanceLine.before} → {balanceLine.after}
                      </span>
                    )}
                  </td>
                  <td className="sing-all-num">{line.credits} cr</td>
                </tr>
              );
            default:
              return null;
          }
        })}
      </tbody>
    </table>
  );

  const actionButton =
    q.blocked === "free_plan" ? (
      <button type="button" className="selection-send" onClick={onUpgrade}>
        Upgrade to sing
      </button>
    ) : notEnough ? (
      <button type="button" className="selection-send" onClick={onTopUp}>
        Top up
      </button>
    ) : q.blocked === "style_unavailable" ? (
      <button
        type="button"
        className="selection-send"
        disabled={pending}
        onClick={() => change({ styleNote: appliedNote || undefined })}
      >
        Try again
      </button>
    ) : (
      <button
        type="button"
        className="selection-send"
        data-testid="sing-all-sing-it"
        disabled={pending || starting || q.blocked === "nothing_to_sing"}
        onClick={() => void singIt()}
      >
        {starting ? "Starting…" : `Sing it · ${q.total} cr`}
      </button>
    );

  const languages = q.options.languages;
  // With no instrument sounding, part mixes would repeat the solo vocals: only the full mix is offered.
  // A backend from before step 4 sends no mix counts, and the card offers no mixes.
  const mixFiles = q.options.mixFiles ?? null;
  const allFiles = mixFiles?.all ?? 0;
  const fullFiles = mixFiles?.full ?? 0;
  const partMixesOffered = allFiles !== fullFiles;
  const quotedMixes = q.options.mixes ?? "all";
  const shownMixes: SingAllMixes = !partMixesOffered && quotedMixes === "all" ? "full" : quotedMixes;
  const mixChoices: Array<[SingAllMixes, string]> = partMixesOffered
    ? [
        ["all", `Full and part mixes (${allFiles})`],
        ["full", `Full mix only (${fullFiles})`],
        ["none", "None"],
      ]
    : [
        ["full", `Full mix (${fullFiles})`],
        ["none", "None"],
      ];
  const customiseRows = (
    <div className="sing-all-rows">
      <div className="sing-all-row">
        <span className="sing-all-row-label">Language</span>
        <select
          className="selection-select sing-all-select"
          value={q.options.language}
          aria-label="Language"
          disabled={q.options.solfegeOnly || pending}
          onChange={(event) => customise({ language: event.target.value })}
        >
          {languages.map((language) => (
            <option key={language.code} value={language.code}>
              {languageName(language.code, language.label)}
            </option>
          ))}
        </select>
      </div>

      <div className="sing-all-row-block">
        <div className="sing-all-row">
          <span className="sing-all-row-label">Parts & voices</span>
          <span className="sing-all-muted">
            {included.length} of {q.rows.length} parts
          </span>
        </div>
        <div className="sing-all-parts-box">
          {q.rows.map((row) => {
            const note = row.note && row.note !== "no_words" ? rowNote(row) : null;
            return (
              <div key={row.rowId} className={clsx("sing-all-part-line", !row.include && "sing-all-row-off")}>
                <label className="sing-all-part-name">
                  <input
                    type="checkbox"
                    checked={row.include}
                    disabled={pending}
                    onChange={(event) => changeRow(row.rowId, { include: event.target.checked })}
                  />
                  <span>
                    {row.name}
                    {note && <span className="sing-all-muted sing-all-inline-note"> · {note}</span>}
                  </span>
                </label>
                <select
                  className="selection-select sing-all-select"
                  value={row.voice}
                  disabled={!row.include || pending}
                  aria-label={`${row.name} voice`}
                  onChange={(event) => changeRow(row.rowId, { voice: event.target.value, voicePicked: "user" })}
                >
                  {!voiceNames.has(row.voice) && <option value={row.voice}>{row.voice}</option>}
                  {q.voices.map((voice) => (
                    <option key={voice.id} value={voice.id}>
                      {voice.name}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
          {q.lines.some((line) => line.kind === "instrumentals") && (
            <div className="sing-all-part-line sing-all-muted">Instrumentals · always included</div>
          )}
        </div>
      </div>

      <div className="sing-all-row">
        <span className="sing-all-row-label">Sing in</span>
        <div className="sing-all-segmented" role="group" aria-label="Sing in">
          {(["lyrics", "solfege", "both"] as SingAllMode[]).map((value) => (
            <button
              key={value}
              type="button"
              className={clsx(q.options.mode === value && "active")}
              aria-pressed={q.options.mode === value}
              disabled={pending || (q.options.solfegeOnly && value !== "solfege")}
              onClick={() => customise({ mode: value })}
            >
              {MODE_LABEL[value]}
            </button>
          ))}
        </div>
      </div>

      <div className="sing-all-row">
        <span className="sing-all-row-label">Verse</span>
        {q.options.mode === "solfege" ? (
          <span className="sing-all-muted">— (solfège has no words)</span>
        ) : verseCount > 1 ? (
          <select
            className="selection-select sing-all-select"
            value={q.options.verse}
            aria-label="Verse"
            disabled={pending}
            onChange={(event) => customise({ verse: event.target.value })}
          >
            {q.options.verses.map((verse) => (
              <option key={verse} value={verse}>
                {verse}
              </option>
            ))}
          </select>
        ) : (
          <span className="sing-all-muted">{q.options.verse}</span>
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
                className={clsx(q.options.repeats === value && "active")}
                aria-pressed={q.options.repeats === value}
                disabled={pending || (value && !q.options.repeatsAvailable)}
                onClick={() => customise({ repeats: value })}
              >
                {label}
              </button>
            ))}
          </span>
          {seconds > 0 && <span className="sing-all-muted">{formatDuration(seconds)}</span>}
        </span>
      </div>

      {mixFiles && (
        <div className="sing-all-row">
          <span className="sing-all-row-label">Mixes</span>
          <div className="sing-all-segmented" role="group" aria-label="Mixes">
            {mixChoices.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={clsx(shownMixes === value && "active")}
                aria-pressed={shownMixes === value}
                disabled={pending}
                onClick={() => customise({ mixes: value })}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="sing-all-row-block">
        <div className="sing-all-row">
          <span className="sing-all-row-label">Style notes</span>
          <span className="sing-all-muted">optional</span>
        </div>
        <textarea
          className="sing-all-style-input"
          rows={2}
          value={styleDraft}
          aria-label="Style notes"
          placeholder='e.g. "Warm and soft. Bass: a strong, masculine voice."'
          onChange={(event) => setStyleDraft(event.target.value)}
        />
      </div>
    </div>
  );

  return (
    <div className="sing-all-card sing-all-inline" data-testid="sing-all-card" aria-busy={pending}>
      {header}
      {customising ? (
        customiseRows
      ) : (
        <>
          {quoteChangedLine && <p className="sing-all-notice">The score changed, so here is the new quote.</p>}
          <p className="sing-all-summary-line">{summaryBits.join(" · ")}</p>
          <p className="sing-all-muted">
            Voices: {included.map((row) => `${row.name} · ${voiceName(row.voice)}`).join(", ") || "none"}
          </p>
          {q.style.note && (
            <div className="sing-all-style-lines">
              <p className="sing-all-muted">Style: “{q.style.note}”</p>
              {styleRows.map(({ row, line }) => (
                <p key={row.rowId} className="sing-all-muted">
                  {row.name} · {voiceName(row.voice)} · {line}
                </p>
              ))}
            </div>
          )}
          {q.blocked === "style_unavailable" && <p className="sing-all-warning">The style couldn't be applied.</p>}
          {[...rowNotes, ...q.notes.map(noteText)].map((text) => (
            <p key={text} className="sing-all-muted">
              {text}
            </p>
          ))}
          {quoteTable}
        </>
      )}

      {customising ? (
        <div className="sing-all-sticky-footer sing-all-total-bar">
          {detailsOpen && <div className="sing-all-details-tray">{quoteTable}</div>}
          <div className="sing-all-total-line">
            <span>
              Total <strong>{q.total} cr</strong>
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
            <button type="button" className="selection-send" disabled={pending} onClick={apply}>
              Apply
            </button>
          </div>
        </div>
      ) : (
        <>
          {startError && <p className="sing-all-warning">{startError}</p>}
          <div className="sing-all-actions">
            <span />
            {actionButton}
          </div>
        </>
      )}
    </div>
  );
}

const STATUS_HEADLINE: Record<SingAllProgress["status"], string> = {
  running: "Singing your song",
  completed: "Done: every part is sung",
  partial: "Done, but some parts couldn't be sung",
  failed: "It couldn't be sung",
  cancelled: "Stopped",
};

const END_REASON: Record<string, string> = {
  score_replaced: "The score was replaced.",
  quote_changed: "The score changed after the quote.",
  split_failed: "The parts couldn't be split.",
  solfege_failed: "The solfège couldn't be added.",
  nothing_to_sing: "There was nothing left to sing.",
  instrumentals_failed: "The instrumentals couldn't be prepared.",
  stopped: "You stopped it.",
  stuck: "It stopped making progress.",
};

const TAKE_MARK: Record<string, string> = { queued: "·", running: "…", done: "✓", failed: "✗", stopped: "■" };

function SingAllJobView({
  songTitle,
  jobId,
  job,
  onLoadJob,
  onStop,
}: SingAllCardProps & { jobId: string }) {
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    if (!job) onLoadJob(jobId);
  }, [job, jobId, onLoadJob]);

  if (!job) {
    return (
      <div className="sing-all-card sing-all-inline" data-testid="sing-all-card">
        <div className="sing-all-title">
          <span className="sing-all-step">Sing All</span>
        </div>
        <h3 className="sing-all-step-title">{songTitle}</h3>
        <p className="sing-all-muted" role="status">
          Starting…
        </p>
      </div>
    );
  }

  const running = job.status === "running";
  const { splits, takes } = job.counts;
  const solfege = job.choices.mode !== "lyrics";
  const stages: Array<{ key: string; label: string; state: "done" | "now" | "later" }> = [];
  const order = ["reading", "splitting", "solfege", "singing", "packaging", "done"];
  const at = order.indexOf(job.stage);
  const stateOf = (stage: string): "done" | "now" | "later" => {
    const index = order.indexOf(stage);
    return index < at ? "done" : index === at ? "now" : "later";
  };
  stages.push({ key: "reading", label: "Reading the score", state: stateOf("reading") });
  if (splits.total > 0) {
    stages.push({
      key: "splitting",
      label: `Splitting the vocal parts (${splits.done} of ${splits.total})`,
      state: stateOf("splitting"),
    });
  }
  if (solfege) stages.push({ key: "solfege", label: "Adding the solfège", state: stateOf("solfege") });
  const queued = job.render.state === "waiting" && job.render.placeInLine;
  stages.push({
    key: "singing",
    label: queued
      ? `Queued for singing · ${ordinal(job.render.placeInLine as number)} in line`
      : `Singing (${takes.done} of ${takes.total})`,
    state: stateOf("singing"),
  });
  // Packaging: the mixes, then the zip. A job with no mixes chosen only packs the zip.
  // A backend from before step 4 sends no mixes choice, and its jobs have no packaging.
  const pack = job.package ?? null;
  const packaging = stateOf("packaging");
  const packs = job.choices.mixes !== undefined;
  const mixing = packs && job.choices.mixes !== "none" && (!pack || pack.mixes.total > 0);
  const mixingNow = mixing && (!pack || pack.state === "mixing");
  if (mixing) {
    stages.push({
      key: "mixing",
      label: pack ? `Mixing (${pack.mixes.done} of ${pack.mixes.total})` : "Mixing",
      state: packaging === "now" ? (mixingNow ? "now" : "done") : packaging,
    });
  }
  if (packs) {
    stages.push({
      key: "zipping",
      label: "Packing the zip",
      state: packaging === "now" ? (mixingNow ? "later" : "now") : packaging,
    });
  }

  const download = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const { url } = await fetchSingAllDownload(job.jobId);
      window.location.assign(url);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : undefined;
      setDownloadError(
        code === "zip_unavailable" || code === "job_not_found"
          ? "The zip isn't available."
          : "Couldn't get the zip. Try again."
      );
    } finally {
      setDownloading(false);
    }
  };

  const stop = async () => {
    setStopping(true);
    try {
      await onStop(job.songId);
    } finally {
      setStopping(false);
      setConfirmStop(false);
    }
  };

  return (
    <div className="sing-all-card sing-all-inline" data-testid="sing-all-card" data-status={job.status}>
      <div className="sing-all-title">
        <span className="sing-all-step">Sing All</span>
      </div>
      <h3 className="sing-all-step-title">{job.title || songTitle}</h3>
      <p className="sing-all-summary-line">{STATUS_HEADLINE[job.status]}</p>

      {running && (
        <ol className="sing-all-stages">
          {stages.map((stage) => (
            <li key={stage.key} className={clsx("sing-all-stage", `is-${stage.state}`)}>
              {stage.label}
            </li>
          ))}
        </ol>
      )}

      <ul className="sing-all-job-rows">
        {job.rows.map((row) => (
          <li key={row.rowId}>
            <span>
              {row.name} · {row.voiceName}
            </span>
            <span className="sing-all-take-marks">
              {row.takes.map((take) => (
                <span
                  key={take.mode}
                  className={clsx("sing-all-take-mark", `is-${take.status}`)}
                  title={`${MODE_LABEL[take.mode]}: ${take.status}`}
                >
                  {job.choices.mode === "both" ? `${MODE_LABEL[take.mode]} ` : ""}
                  {TAKE_MARK[take.status] ?? "·"}
                </span>
              ))}
            </span>
          </li>
        ))}
      </ul>

      {job.notes.map((note) => (
        <p key={`${note.code}:${note.partId}`} className="sing-all-muted">
          {noteText(note)}
        </p>
      ))}

      {running ? (
        <>
          <p className="sing-all-muted">
            {formatMinutes(job.elapsedS)} so far
            {job.remainingS ? ` · about ${formatMinutes(job.remainingS)} left` : ""}
          </p>
          <p className="sing-all-muted">
            {job.emailOnEnd
              ? "You can close this page. We'll email you when it's done."
              : "You can close this page. Your song will be here when you come back."}
          </p>
          {job.cancelRequested ? (
            <p className="sing-all-muted">Stopping…</p>
          ) : confirmStop ? (
            <div className="sing-all-confirm" role="alertdialog" aria-label="Stop Sing All?">
              <p>Stop Sing All? Parts already sung are kept and charged. The rest is refunded.</p>
              <div className="sing-all-actions">
                <button type="button" className="sing-all-link" onClick={() => setConfirmStop(false)}>
                  Keep singing
                </button>
                <button type="button" className="selection-send" disabled={stopping} onClick={() => void stop()}>
                  Stop
                </button>
              </div>
            </div>
          ) : (
            <div className="sing-all-actions">
              <span />
              <button type="button" className="sing-all-link" onClick={() => setConfirmStop(true)}>
                Stop
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          {job.endReason && job.status !== "completed" && (
            <p className="sing-all-muted">{END_REASON[job.endReason] ?? "It stopped early."}</p>
          )}
          {pack?.state === "failed" && (
            <p className="sing-all-muted">
              {job.choices.mixes === "none"
                ? "The zip couldn't be made. Your parts are in the player."
                : "The mixes and the zip couldn't be made. The mixes were refunded; your parts are in the player."}
            </p>
          )}
          {pack?.state === "done" && (
            <div className="sing-all-actions">
              {downloadError ? <p className="sing-all-warning">{downloadError}</p> : <span />}
              <button
                type="button"
                className="selection-send"
                data-testid="sing-all-download"
                disabled={downloading}
                onClick={() => void download()}
              >
                {downloading ? "Preparing…" : "Download zip"}
              </button>
            </div>
          )}
          {job.receipt.length > 0 && (
            <table className="sing-all-table sing-all-receipt">
              <thead>
                <tr>
                  <th>Receipt</th>
                  <th className="sing-all-num">Credits</th>
                </tr>
              </thead>
              <tbody>
                {job.receipt.map((item) => (
                  <tr key={item.itemId} className={clsx(item.outcome === "refunded" && "sing-all-refunded")}>
                    <td>
                      {item.type === "instrumental"
                        ? "Instrumentals"
                        : item.type === "mixes"
                          ? `Mixes (${item.files ?? 0} file${item.files === 1 ? "" : "s"})`
                          : item.type === "split"
                            ? `Split ${item.name ?? "a part"}`
                            : `${item.name ?? "A part"}${item.mode === "solfege" ? " (solfège)" : ""}`}
                    </td>
                    <td className="sing-all-num">
                      {item.outcome === "included"
                        ? "included"
                        : item.outcome === "refunded"
                          ? `${item.credits} cr refunded`
                          : `${item.credits} cr`}
                    </td>
                  </tr>
                ))}
                <tr className="sing-all-total">
                  <td>
                    Charged
                    {job.credits.refunded > 0 && (
                      <span className="sing-all-muted sing-all-inline-note"> · {job.credits.refunded} cr refunded</span>
                    )}
                  </td>
                  <td className="sing-all-num">{job.credits.charged} cr</td>
                </tr>
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
