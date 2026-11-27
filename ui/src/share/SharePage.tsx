import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import type { OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import { Link, useParams } from "react-router-dom";
import { ChevronDown, Minus, Pause, Play, Plus, Share2, Square, Volume2 } from "lucide-react";
import { getContext as getToneContext } from "tone";
import {
  ApiError,
  fetchShareAudio,
  fetchShareMidi,
  fetchSharePage,
  fetchShareScore,
  type PerformanceMeasureMapEntry,
  type SharePageData,
  SONG_LICENSES,
  type SongInfoFields,
} from "../api";
import { marketingUrl } from "../constants";
import { useAuth } from "../hooks/useAuth";
import { createPerformanceMeasureLookup, positionNativeScoreCursor } from "../scorePlayback";
import {
  ScorePlayerEngine,
  type ScorePlayerPlaybackControls,
} from "../player/ScorePlayerEngine";
import {
  HorizontalScoreRenderer,
  HorizontalViewIcon,
  PageScoreRenderer,
  PageViewIcon,
  countParallelScoreStaves,
  getHorizontalScoreIncrementalBatchSize,
  scrollScoreMeasureIntoView,
  type HorizontalScoreRendererHandle,
  type ScorePreviewLayout,
} from "../player/score";
import {
  DEFAULT_INSTRUMENTAL_BUS,
  instrumentalTrackKey,
  vocalsInBothModes,
  type InstrumentalTrackState,
  type MultiTrackAudioTrack,
  type VocalMode,
} from "../player/tracks";
import {
  shareControlUse, shareCtaClick, shareLinkCopied, shareListenProgress, sharePageUnavailable,
  sharePageView, sharePlay, type ShareControl,
} from "../analytics";
import { startRows, type ShareRows } from "./startRows";
import "./SharePage.css";

type Load =
  | { kind: "loading" }
  | { kind: "off" }
  | { kind: "busy" }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: SharePageData };

const decodeShareTake = async (url: string): Promise<AudioBuffer> =>
  getToneContext().rawContext.decodeAudioData(await fetchShareAudio(url));

/** A shared Song: the score and its sung parts, to listen to and follow. Nothing here edits or saves. */
export default function SharePage() {
  const { token = "" } = useParams();
  return <SharedLink key={token} token={token} />;
}

function SharedLink({ token }: { token: string }) {
  const { authReady, user } = useAuth();
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const pageReported = useRef(false);

  useEffect(() => {
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex, nofollow";
    document.head.append(meta);
    return () => meta.remove();
  }, []);

  useEffect(() => {
    // The owner is recognised by their sign-in, so wait until it is known.
    if (!authReady) return;
    let cancelled = false;
    fetchSharePage(token)
      .then((data) => {
        if (!cancelled) setLoad({ kind: "ready", data });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404 && err.code === "share_off") setLoad({ kind: "off" });
        else if (err instanceof ApiError && err.status === 429) setLoad({ kind: "busy" });
        else setLoad({ kind: "error", message: err instanceof Error ? err.message : "Couldn't open this song." });
      });
    return () => {
      cancelled = true;
    };
  }, [authReady, token]);

  useEffect(() => {
    if (load.kind === "ready") document.title = `${load.data.title} · SightSinger`;
    if (load.kind === "loading" || pageReported.current) return;
    pageReported.current = true;
    if (load.kind !== "ready") {
      sharePageUnavailable(load.kind);
      return;
    }
    const { player, info, is_owner: isOwner } = load.data;
    sharePageView({
      signedIn: Boolean(user),
      isOwner,
      layout: window.matchMedia("(min-width: 900px) and (orientation: landscape)").matches ? "laptop" : "portrait",
      parts: player.vocals.length,
      bothModes: vocalsInBothModes(player.vocals),
      hasInstruments: Boolean(player.midi_url),
      infoFields: info ? [
        info.subtitle, ...songInfoRows(info).map(([, value]) => value),
        info.score_copyright, info.description,
      ].filter(Boolean).length : 0,
      license: info?.license ?? "all_rights_reserved",
    });
  }, [load, user]);

  const ownSongId = load.kind === "ready" && load.data.is_owner ? load.data.song_id : null;

  return (
    <div className="share-page">
      <header className="share-page-header">
        <Link to="/" className="share-page-brand" onClick={() => shareCtaClick("brand")}>
          <span className="brand-banner-crop" aria-label="SightSinger">
            <img className="brand-banner" src="/content/images/logo-hackaton-white.png" alt="" />
          </span>
        </Link>
        <div className="share-page-header-actions">
          {/* One button for everyone. A visitor may not know SightSinger yet, so it opens the
              marketing site's home page rather than sign-in; a signed-in owner goes straight back
              to this Song. */}
          {ownSongId ? (
            <Link
              className="share-page-cta"
              to={`/app/song/${ownSongId}`}
              onClick={() => shareCtaClick("open_in_app")}
            >
              Sign in to sing your own song for free
            </Link>
          ) : (
            <a className="share-page-cta" href={marketingUrl("/")} onClick={() => shareCtaClick("marketing_home")}>
              Sign in to sing your own song for free
            </a>
          )}
        </div>
      </header>
      {load.kind === "loading" ? (
        <p className="share-page-message" role="status">Loading…</p>
      ) : load.kind === "off" ? (
        <p className="share-page-message">This link was turned off</p>
      ) : load.kind === "busy" ? (
        <p className="share-page-message" role="alert">Too many requests. Try again in a minute.</p>
      ) : load.kind === "error" ? (
        <p className="share-page-message" role="alert">{load.message}</p>
      ) : (
        <SharedSong data={load.data} />
      )}
    </div>
  );
}

function SharedSong({ data }: { data: SharePageData }) {
  const summary = data.score.summary;
  const [rows, setRows] = useState<ShareRows>(() => startRows(data.player, summary.performance_midi));
  const [modeShown, setModeShown] = useState<VocalMode>("lyrics");
  const [scoreXml, setScoreXml] = useState<string | null>(null);
  const [scoreError, setScoreError] = useState<string | null>(null);
  const [decoded, setDecoded] = useState<ReadonlyMap<string, AudioBuffer>>(() => new Map());
  const [vocalsRequested, setVocalsRequested] = useState(false);
  const [decoding, setDecoding] = useState(false);
  const [instrumentsArmed, setInstrumentsArmed] = useState(false);
  const [midiUrl, setMidiUrl] = useState<string | null>(null);
  const [controls, setControls] = useState<ScorePlayerPlaybackControls | null>(null);
  const [playing, setPlaying] = useState(false);
  const [engineLoading, setEngineLoading] = useState(false);
  const [playerError, setPlayerError] = useState<string | null>(null);
  const [volume, setVolume] = useState(1);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [seekBarHost, setSeekBarHost] = useState<HTMLDivElement | null>(null);
  const controlsRef = useRef<ScorePlayerPlaybackControls | null>(null);
  const assetsReadyRef = useRef(false);
  const playPendingRef = useRef(false);
  const midiPendingRef = useRef(false);
  const firstVocalKey = useRef<string>();
  const sentFirstTrackFlag = useRef(false);
  const playReported = useRef(false);
  const usedControls = useRef(new Set<ShareControl>());
  const listenedMarks = useRef(new Set<number>());
  const timelineDuration = useRef(0);
  const lastPlaybackPosition = useRef(0);
  const wasPlaying = useRef(false);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<HorizontalScoreRendererHandle | null>(null);
  const pageOsmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  // The page view by default, as in the app.
  const [layout, setLayout] = useState<ScorePreviewLayout>("page");
  const [zoomLevel, setZoomLevel] = useState(1);
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const lookupRef = useRef(createPerformanceMeasureLookup());
  const activeMeasureKeyRef = useRef<string | null>(null);

  const bothModes = vocalsInBothModes(rows.vocals);
  const shownVocalRows = useMemo(
    () => (bothModes ? rows.vocals.filter((row) => row.mode === modeShown) : rows.vocals),
    [bothModes, modeShown, rows.vocals]
  );
  const hasInstrumentRows = rows.instruments.length > 0 && Boolean(data.player.midi_url);
  const anyInstrumentOn = hasInstrumentRows && rows.instruments.some((row) => row.on);
  const [partsOpen, setPartsOpen] = useState(true);
  const listedRows = hasInstrumentRows ? [...shownVocalRows, ...rows.instruments] : shownVocalRows;
  const rowsOn = listedRows.filter((row) => row.on).length;

  const recordControl = useCallback((control: ShareControl) => {
    if (usedControls.current.has(control)) return;
    usedControls.current.add(control);
    shareControlUse(control);
  }, []);

  useEffect(() => {
    if (!seekBarHost) return;
    // The engine portals its range input into this host. Native input events
    // distinguish an explicit seek from the engine updating the displayed value.
    const recordSeek = () => recordControl("seek");
    seekBarHost.addEventListener("input", recordSeek);
    return () => seekBarHost.removeEventListener("input", recordSeek);
  }, [seekBarHost, recordControl]);

  useEffect(() => {
    let cancelled = false;
    fetchShareScore(data.score.url)
      .then((xml) => {
        if (!cancelled) setScoreXml(xml);
      })
      .catch((err: unknown) => {
        if (!cancelled) setScoreError(err instanceof Error ? err.message : "Couldn't load the score.");
      });
    return () => {
      cancelled = true;
    };
  }, [data.score.url]);

  // Start downloading on Play. Request the chosen mode's first track first
  // so passive page views never count as plays.
  useEffect(() => {
    if (!vocalsRequested) return;
    let cancelled = false;
    const vocals = [...data.player.vocals];
    const firstIndex = vocals.findIndex((vocal) => vocal.key === firstVocalKey.current);
    if (firstIndex > 0) vocals.unshift(...vocals.splice(firstIndex, 1));
    void Promise.allSettled(vocals.map(async (vocal, index) => {
      let url = vocal.audio_url;
      if (index === 0 && !data.is_owner && !sentFirstTrackFlag.current) {
        sentFirstTrackFlag.current = true;
        url += `${url.includes("?") ? "&" : "?"}first=1`;
      }
      return [vocal.key, await decodeShareTake(url)] as const;
    })).then((results) => {
      if (cancelled) return;
      const next = new Map<string, AudioBuffer>();
      results.forEach((result) => {
        if (result.status === "fulfilled") next.set(result.value[0], result.value[1]);
      });
      if (next.size < vocals.length) setPlayerError("Some sung parts couldn't be loaded.");
      setDecoded(next);
      setDecoding(false);
    });
    return () => {
      cancelled = true;
    };
  }, [data.is_owner, data.player.vocals, vocalsRequested]);

  // The instruments' MIDI, and with it the soundfont, load only once a Play needs them.
  useEffect(() => {
    if (!instrumentsArmed || !data.player.midi_url) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    fetchShareMidi(data.player.midi_url)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        midiPendingRef.current = false;
        setMidiUrl(objectUrl);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPlayerError(err instanceof Error ? err.message : "Couldn't load the instruments.");
        midiPendingRef.current = false;
        setInstrumentsArmed(false);
        if (playPendingRef.current && assetsReadyRef.current) startPlayback();
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // startPlayback reads refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrumentsArmed, data.player.midi_url]);

  const vocalTracks = useMemo<MultiTrackAudioTrack[]>(
    () =>
      shownVocalRows.map((row) => ({
        key: row.key,
        label: row.label,
        audioUrl: row.audio_url,
        sourceJobId: row.key,
        durationSeconds: row.duration_seconds ?? null,
        mode: row.mode,
        muted: !row.on,
        solo: false,
        volume: row.level,
      })),
    [shownVocalRows]
  );
  const instrumentalTracks = useMemo<InstrumentalTrackState[]>(
    () =>
      instrumentsArmed
        ? [
            ...rows.instruments.map(({ level, on, ...row }) => ({ ...row, muted: !on, solo: false, volume: level })),
            ...rows.silent.map((row) => ({ ...row, muted: true, solo: false })),
          ]
        : [],
    [instrumentsArmed, rows.instruments, rows.silent]
  );
  const instrumentalMidiTrackKeys = useMemo(
    () => (summary.performance_midi?.instrumental_parts ?? []).filter((part) => part.eligible).map(instrumentalTrackKey),
    [summary.performance_midi]
  );
  const measuresPerBatch = useMemo(
    () =>
      getHorizontalScoreIncrementalBatchSize(
        scoreXml ? countParallelScoreStaves(scoreXml, 1) : 1
      ),
    [scoreXml]
  );

  const startPlayback = useCallback(() => {
    const current = controlsRef.current;
    if (!current) return;
    playPendingRef.current = false;
    void current.play().then(() => setPlaying(true)).catch((err: unknown) => {
      setPlayerError(err instanceof Error ? err.message : "Couldn't start playback.");
    });
  }, []);

  const handleControlsChange = useCallback((next: ScorePlayerPlaybackControls | null) => {
    controlsRef.current = next;
    setControls((current) => (next ? current ?? next : null));
  }, []);
  const handleEngineLoading = useCallback(() => {
    assetsReadyRef.current = false;
    setEngineLoading(true);
  }, []);
  const handleEngineReady = useCallback(() => {
    assetsReadyRef.current = true;
    setEngineLoading(false);
    if (playPendingRef.current && !midiPendingRef.current) startPlayback();
  }, [startPlayback]);

  useEffect(() => {
    controls?.setMasterVolume(volume);
  }, [controls, volume]);

  const recordProgress = useCallback((seconds: number) => {
    const duration = timelineDuration.current;
    if (duration <= 0) return;
    for (const percent of [25, 50, 75, 100] as const) {
      if (seconds < duration * percent / 100 || listenedMarks.current.has(percent)) continue;
      listenedMarks.current.add(percent);
      shareListenProgress(percent);
    }
  }, []);

  const handleDuration = useCallback((seconds: number) => {
    timelineDuration.current = seconds;
  }, []);

  const handlePlaybackStateChange = useCallback((isPlaying: boolean) => {
    // The engine can reach the end between animation frames. Explicit pause
    // and stop clear wasPlaying before the engine reports the state change.
    const remaining = timelineDuration.current - lastPlaybackPosition.current;
    if (!isPlaying && wasPlaying.current && remaining <= 0.25) {
      recordProgress(timelineDuration.current);
    }
    wasPlaying.current = isPlaying;
    setPlaying(isPlaying);
  }, [recordProgress]);

  const handlePosition = useCallback((seconds: number) => {
    lastPlaybackPosition.current = seconds;
    recordProgress(seconds);
    const map = summary.performance_measure_map;
    const entries = data.player.repeats ? map?.expanded : map?.written;
    const measure: PerformanceMeasureMapEntry | null = lookupRef.current(entries, seconds);
    const horizontal = layoutRef.current === "horizontal";
    const renderer = horizontal ? rendererRef.current : null;
    const osmd = horizontal ? renderer?.osmd : pageOsmdRef.current;
    if (!measure || !osmd) return;
    const key = `${measure.played_measure_index}:${measure.source_measure_index}`;
    if (activeMeasureKeyRef.current === key) return;
    try {
      renderer?.ensureSourceMeasureRendered(measure.source_measure_index);
      if (!positionNativeScoreCursor(osmd, measure.source_measure_index)) return;
      activeMeasureKeyRef.current = key;
      const cursorElement = osmd.cursor.cursorElement;
      cursorElement.classList.add("score-active-measure-highlight");
      cursorElement.dataset.testid = "active-score-measure";
      const canvas = canvasRef.current;
      if (canvas) scrollScoreMeasureIntoView(canvas, cursorElement, layoutRef.current);
    } catch {
      // The cursor is visual only; it never stops playback.
    }
  }, [data.player.repeats, recordProgress, summary.performance_measure_map]);

  const handlePlay = () => {
    setPlayerError(null);
    if (!playReported.current) {
      playReported.current = true;
      const mode = bothModes ? modeShown : shownVocalRows[0]?.mode ?? "lyrics";
      sharePlay(mode, shownVocalRows.filter((row) => row.on).length, anyInstrumentOn);
    }

    // Resume in the user gesture, before waiting for the first audio response.
    void getToneContext().resume().catch(() => {});
    const loadVocals = !vocalsRequested && data.player.vocals.length > 0;
    if (loadVocals) {
      firstVocalKey.current = (shownVocalRows.find((row) => row.on) ?? shownVocalRows[0])?.key;
      setVocalsRequested(true);
      setDecoding(true);
    }
    const loadInstruments = anyInstrumentOn && !instrumentsArmed;
    if (loadInstruments) {
      midiPendingRef.current = true;
      setInstrumentsArmed(true);
    }
    if (!loadVocals && !loadInstruments && controlsRef.current && assetsReadyRef.current
        && !midiPendingRef.current) {
      startPlayback();
    } else {
      playPendingRef.current = true;
    }
  };
  const handlePause = () => {
    wasPlaying.current = false;
    playPendingRef.current = false;
    controlsRef.current?.pause();
    setPlaying(false);
  };
  const handleZoom = (delta: number) => {
    recordControl("zoom");
    const next = Math.min(2, Math.max(0.6, zoomLevel + delta));
    setZoomLevel(Math.round(next * 10) / 10);
  };

  const handleStop = () => {
    wasPlaying.current = false;
    playPendingRef.current = false;
    controlsRef.current?.stop();
    controlsRef.current?.setCurrentTime(0);
    handlePosition(0);
    setPlaying(false);
  };

  const toggleVocal = (key: string) => {
    recordControl("part_toggle");
    setRows((current) => ({
      ...current,
      vocals: current.vocals.map((row) => (row.key === key ? { ...row, on: !row.on } : row)),
    }));
  };
  const toggleInstrument = (key: string) => {
    recordControl("part_toggle");
    setRows((current) => ({
      ...current,
      instruments: current.instruments.map((row) => (row.key === key ? { ...row, on: !row.on } : row)),
    }));
    // An instrument turned on after the first Play loads the instruments then.
    if (playing && !instrumentsArmed) setInstrumentsArmed(true);
  };

  const copyLink = async () => {
    shareLinkCopied("share_page");
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const canPlay = !decoding && (shownVocalRows.some((row) => !vocalsRequested || decoded.has(row.key))
    || hasInstrumentRows);
  const loadingText = decoding
    ? "Loading the sung parts…"
    : instrumentsArmed && (engineLoading || !midiUrl)
      ? "Loading the instruments…"
      : null;

  return (
    <main className="share-song">
      <div className="share-song-title-row">
        <div className="share-song-heading">
          <h1 className="share-song-title">{data.title}</h1>
          {data.info?.subtitle ? <p className="share-song-subtitle">{data.info.subtitle}</p> : null}
        </div>
        <button type="button" className="btn-secondary-inline" onClick={() => void copyLink()}>
          <Share2 size={15} aria-hidden="true" /> {copied ? "Link copied" : "Share"}
        </button>
      </div>

      {/* One layout for both screens: on a landscape screen the tracks take the left third and
          the controls sit above the score; on a portrait one they stack as a single column. */}
      <div className="share-song-layout">
        <section className="share-song-controls" aria-label="Player">
          <div className="share-song-transport">
            <button
              type="button"
              className="score-action-button"
              onClick={playing ? handlePause : handlePlay}
              disabled={!playing && !canPlay}
              aria-label={playing ? "Pause" : "Play"}
              title={playing ? "Pause" : "Play"}
            >
              {playing ? <Pause size={16} /> : <Play size={16} />}
            </button>
            <button
              type="button"
              className="score-action-button"
              onClick={handleStop}
              disabled={!controls}
              aria-label="Stop"
              title="Stop"
            >
              <Square size={15} />
            </button>
            <div ref={setSeekBarHost} className="score-player-seek-host share-song-seek" />
            <label className="share-song-volume">
              <Volume2 size={15} aria-hidden="true" />
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={volume}
                aria-label="Volume"
                onChange={(event) => {
                  recordControl("volume");
                  setVolume(Number(event.target.value));
                }}
              />
            </label>
          </div>
          <div className="share-song-score-bar">
            {/* The app's zoom control: 60–200% in steps of 10%. */}
            <div className="zoom-controls">
              <button
                type="button"
                className="zoom-button"
                onClick={() => handleZoom(-0.1)}
                aria-label="Zoom out"
                disabled={!scoreXml || Boolean(scoreError)}
              >
                <Minus size={16} />
              </button>
              <span className="zoom-value">{Math.round(zoomLevel * 100)}%</span>
              <button
                type="button"
                className="zoom-button"
                onClick={() => handleZoom(0.1)}
                aria-label="Zoom in"
                disabled={!scoreXml || Boolean(scoreError)}
              >
                <Plus size={16} />
              </button>
            </div>
            <div className="score-layout-toggle" role="group" aria-label="Score preview layout">
              {(["page", "horizontal"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  className={clsx("score-layout-option", { selected: layout === option })}
                  aria-pressed={layout === option}
                  aria-label={option === "page" ? "Page view" : "Horizontal view"}
                  title={option === "page" ? "Page view" : "Horizontal view"}
                  disabled={!scoreXml}
                  onClick={() => {
                    if (option !== layout) recordControl("view_toggle");
                    activeMeasureKeyRef.current = null;
                    setLayout(option);
                  }}
                >
                  {option === "page" ? <PageViewIcon /> : <HorizontalViewIcon />}
                </button>
              ))}
            </div>
          </div>
          {loadingText || playerError || engineError ? (
            <div className="share-song-statuses">
              {loadingText ? <p className="share-song-status" role="status">{loadingText}</p> : null}
              {playerError || engineError ? (
                <p className="share-song-status error" role="alert">{playerError ?? engineError}</p>
              ) : null}
            </div>
          ) : null}
        </section>
        <section className="share-song-tracks" aria-label="Tracks">
          {bothModes ? (
            <div className="solfege-segmented-control vocal-mode-switch" role="group" aria-label="Sung in">
              {(["lyrics", "solfege"] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={modeShown === mode ? "selected" : undefined}
                  aria-pressed={modeShown === mode}
                  onClick={() => {
                    if (mode !== modeShown) recordControl("mode_switch");
                    handleStop();
                    setModeShown(mode);
                  }}
                >
                  {mode === "lyrics" ? "Lyrics" : "Solfège"}
                </button>
              ))}
            </div>
          ) : null}

          <button
            type="button"
            className="share-song-parts-toggle"
            aria-expanded={partsOpen}
            aria-controls="share-song-rows"
            onClick={() => {
              recordControl("parts_fold");
              setPartsOpen((open) => !open);
            }}
          >
            <ChevronDown size={16} aria-hidden="true" className="share-song-parts-chevron" />
            <span>Parts</span>
            {listedRows.length ? (
              <span className="share-song-parts-count">
                {rowsOn} of {listedRows.length} on
              </span>
            ) : null}
          </button>
          <ul id="share-song-rows" className="share-song-rows" aria-label="Parts" hidden={!partsOpen}>
            {shownVocalRows.length === 0 ? <li className="share-song-empty">No sung parts yet</li> : null}
            {shownVocalRows.map((row) => (
              <li key={row.key} className="share-song-row" data-testid={`share-row-${row.key}`}>
                <button
                  type="button"
                  className="share-song-row-switch"
                  aria-pressed={row.on}
                  aria-label={`${row.label}: ${row.on ? "on" : "off"}`}
                  onClick={() => toggleVocal(row.key)}
                >
                  {row.on ? "On" : "Off"}
                </button>
                <span className="share-song-row-label">{row.label}</span>
                {row.voice_name ? <span className="share-song-row-voice">{row.voice_name}</span> : null}
              </li>
            ))}
            {hasInstrumentRows
              ? rows.instruments.map((row) => (
                  <li key={row.key} className="share-song-row instrument" data-testid={`share-row-${row.key}`}>
                    <button
                      type="button"
                      className="share-song-row-switch"
                      aria-pressed={row.on}
                      aria-label={`${row.label}: ${row.on ? "on" : "off"}`}
                      onClick={() => toggleInstrument(row.key)}
                    >
                      {row.on ? "On" : "Off"}
                    </button>
                    <span className="share-song-row-label">{row.label}</span>
                  </li>
                ))
              : null}
          </ul>
          {data.info ? (
            <ShareSongInfo info={data.info} onFold={() => recordControl("info_fold")} />
          ) : data.credits.voices.length ? (
            // A backend from before Song Info.
            <p className="share-song-credits">
              Voices: {data.credits.voices.join(", ")} ·{" "}
              <a href={marketingUrl("/credits-and-ai-voices")} onClick={() => shareCtaClick("credits_page")}>Credits and AI voices</a>
            </p>
          ) : null}
        </section>
        <div
          ref={canvasRef}
          className={clsx("score-canvas share-song-score", { "horizontal-layout": layout === "horizontal" })}
        >
          {scoreXml && layout === "page" ? (
            <PageScoreRenderer
              scoreData={scoreXml}
              zoomLevel={zoomLevel}
              onOsmdChange={(osmd) => {
                pageOsmdRef.current = osmd;
                activeMeasureKeyRef.current = null;
              }}
              onReady={() => setScoreError(null)}
              onRenderError={() => setScoreError("Couldn't show the score.")}
            />
          ) : scoreXml ? (
            <HorizontalScoreRenderer
              scoreData={scoreXml}
              zoomLevel={zoomLevel}
              measuresPerBatch={measuresPerBatch}
              scrollContainerRef={canvasRef}
              onRendererChange={(renderer) => {
                rendererRef.current = renderer;
                activeMeasureKeyRef.current = null;
              }}
              onReady={() => setScoreError(null)}
              onRenderError={() => setScoreError("Couldn't show the score.")}
            />
          ) : null}
          {scoreError ? <p className="share-page-message" role="alert">{scoreError}</p> : null}
        </div>
      </div>

      <ScorePlayerEngine
        midiUrl={instrumentsArmed ? midiUrl : null}
        vocalTracks={vocalTracks}
        decodedVocals={decoded}
        vocalsDecoding={decoding}
        instrumentalTracks={instrumentalTracks}
        instrumentalMidiTrackKeys={instrumentalMidiTrackKeys}
        instrumentalBus={DEFAULT_INSTRUMENTAL_BUS}
        playbackRequestId={0}
        onControlsChange={handleControlsChange}
        onEngineLoading={handleEngineLoading}
        onEngineReady={handleEngineReady}
        onPlaybackStateChange={handlePlaybackStateChange}
        onPlaybackPositionChange={handlePosition}
        onTimelineDurationChange={handleDuration}
        onAdapterChange={noop}
        onError={setEngineError}
        seekBarHost={seekBarHost}
        seekDisabled={!controls}
      />
    </main>
  );
}

const noop = () => undefined;


function songInfoRows(info: SongInfoFields): Array<[string, string]> {
  return [
    ["Composer", info.composer],
    ["Lyrics by", info.lyricist],
    ["Arranger", info.arranger],
    ["Credits", info.credits ?? ""],
    ["Produced by", info.producer],
  ];
}

/** The Song's credits, license and description, under Parts; empty fields are left out. */
function ShareSongInfo({ info, onFold }: { info: SongInfoFields; onFold: () => void }) {
  const [open, setOpen] = useState(true);
  const license = SONG_LICENSES[info.license] ?? SONG_LICENSES.all_rights_reserved;
  return (
    <div className="share-song-info">
      <button
        type="button"
        className="share-song-parts-toggle"
        aria-expanded={open}
        aria-controls="share-song-info-body"
        onClick={() => {
          onFold();
          setOpen((current) => !current);
        }}
      >
        <ChevronDown size={16} aria-hidden="true" className="share-song-parts-chevron" />
        <span>Song info</span>
      </button>
      <div id="share-song-info-body" className="share-song-info-body" hidden={!open}>
        <dl className="share-song-info-list">
          {songInfoRows(info).filter(([, value]) => value).map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
          {info.voices.length ? (
            <div>
              <dt>Voices</dt>
              <dd>
                {info.voices.join(", ")} ·{" "}
                <a href={marketingUrl("/credits-and-ai-voices")} onClick={() => shareCtaClick("credits_page")}>Credits and AI voices</a>
              </dd>
            </div>
          ) : null}
          <div>
            <dt>License</dt>
            <dd>
              {license.url ? (
                <a href={license.url} target="_blank" rel="noreferrer license" onClick={() => shareCtaClick("license")}>
                  {license.label}
                </a>
              ) : (
                license.label
              )}
            </dd>
          </div>
          {info.score_copyright ? (
            <div>
              <dt>Score copyright</dt>
              <dd>{info.score_copyright}</dd>
            </div>
          ) : null}
        </dl>
        {info.description ? <p className="share-song-description">{info.description}</p> : null}
      </div>
    </div>
  );
}
