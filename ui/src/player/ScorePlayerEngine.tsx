// The score player engine: plays the decoded takes and the instrument MIDI together,
// with its seek bar. Shared by the app and the public share page.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createToneAdapter, SoundFontCache, type ToneAdapter } from "@waveform-playlist/playout";
import {
  WaveformPlaylistProvider,
  usePlaybackAnimation,
  usePlaylistControls,
  usePlaylistData,
} from "@waveform-playlist/browser";
import { useAudioTracks } from "@waveform-playlist/browser/tone";
import { useMidiTracks } from "@waveform-playlist/midi";
import { createPortal } from "react-dom";
import {
  dbToGain,
  instrumentalTrackForMidiTrack,
  type InstrumentalBusState,
  type InstrumentalTrackState,
  type MultiTrackAudioTrack,
} from "./tracks";

export const FLUID_R3_GM_SOUNDFONT_URL = "/soundfonts/FluidR3_GM.sf2";

export let fluidR3SoundFontCachePromise: Promise<SoundFontCache> | null = null;

export const loadFluidR3SoundFontCache = (): Promise<SoundFontCache> => {
  if (!fluidR3SoundFontCachePromise) {
    fluidR3SoundFontCachePromise = SoundFontCache.fromUrl(FLUID_R3_GM_SOUNDFONT_URL).catch((err) => {
      fluidR3SoundFontCachePromise = null;
      throw err;
    });
  }
  return fluidR3SoundFontCachePromise;
};

export type ScorePlayerPlaybackControls = {
  play: (startTime?: number) => Promise<void>;
  pause: () => void;
  stop: () => void;
  seekTo: (time: number) => void;
  setCurrentTime: (time: number) => void;
};

export type ScorePlayerEngineProps = {
  midiUrl: string | null;
  vocalTracks: MultiTrackAudioTrack[];
  /** Decoded audio of each take, keyed by its source job. */
  decodedVocals: ReadonlyMap<string, AudioBuffer>;
  /** True while any take in the player is still being downloaded and decoded. */
  vocalsDecoding: boolean;
  instrumentalTracks?: InstrumentalTrackState[];
  /** The row key of each instrumental MIDI track, in track order. */
  instrumentalMidiTrackKeys?: readonly string[];
  instrumentalBus: InstrumentalBusState;
  playbackRequestId: number;
  onControlsChange: (controls: ScorePlayerPlaybackControls | null) => void;
  onEngineLoading: () => void;
  onEngineReady: (requestId: number) => void;
  onPlaybackStateChange: (isPlaying: boolean) => void;
  onPlaybackPositionChange: (seconds: number) => void;
  onTimelineDurationChange: (seconds: number) => void;
  onAdapterChange: (adapter: ToneAdapter | null) => void;
  onError: (message: string | null) => void;
  seekBarHost: HTMLDivElement | null;
  seekDisabled: boolean;
};

export const formatPlaybackTime = (seconds: number): string => {
  const wholeSeconds = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const minutes = Math.floor(wholeSeconds / 60);
  return `${minutes}:${String(wholeSeconds % 60).padStart(2, "0")}`;
};

export const ScorePlayerEngineBridge = ({
  onControlsChange,
  loading,
  onReady,
  onPlaybackStateChange,
  onPlaybackPositionChange,
  onTimelineDurationChange,
}: Pick<ScorePlayerEngineProps, "onControlsChange"> & {
  loading: boolean;
  onReady: () => void;
  onPlaybackStateChange: (isPlaying: boolean) => void;
  onPlaybackPositionChange: (seconds: number) => void;
  onTimelineDurationChange: (seconds: number) => void;
}) => {
  const controls = usePlaylistControls();
  const { duration, isReady } = usePlaylistData();
  const { isPlaying, registerFrameCallback, unregisterFrameCallback } = usePlaybackAnimation();
  const liveControlsRef = useRef(controls);
  liveControlsRef.current = controls;
  const stableControlsRef = useRef<ScorePlayerPlaybackControls | null>(null);
  if (!stableControlsRef.current) {
    stableControlsRef.current = {
      play: (startTime) => liveControlsRef.current.play(startTime),
      pause: () => liveControlsRef.current.pause(),
      stop: () => liveControlsRef.current.stop(),
      seekTo: (time) => liveControlsRef.current.seekTo(time),
      setCurrentTime: (time) => liveControlsRef.current.setCurrentTime(time),
    };
  }
  useEffect(() => {
    onControlsChange(stableControlsRef.current);
    return () => onControlsChange(null);
  }, [onControlsChange]);
  // Incremental track additions use the provider's engine.addTrack() path and
  // do not emit the provider-level onReady callback. Notify the outer player
  // once the already-live engine and its source loaders are both ready.
  useEffect(() => {
    if (isReady && !loading) onReady();
  }, [isReady, loading, onReady]);
  useEffect(() => {
    onTimelineDurationChange(duration);
  }, [duration, onTimelineDurationChange]);
  // This is the provider's native playback lifecycle state. In particular, it
  // switches to false when its engine reaches the end of the timeline, which
  // keeps the outer transport icon in sync after natural completion.
  useEffect(() => {
    onPlaybackStateChange(isPlaying);
  }, [isPlaying, onPlaybackStateChange]);
  useEffect(() => {
    if (!isPlaying) return;
    const callbackId = "sightsinger-score-active-measure";
    registerFrameCallback(callbackId, ({ visualTime }) => onPlaybackPositionChange(visualTime));
    return () => unregisterFrameCallback(callbackId);
  }, [isPlaying, onPlaybackPositionChange, registerFrameCallback, unregisterFrameCallback]);
  return null;
};

export const ScorePlayerSeekBar = ({
  host,
  disabled,
  onPlaybackPositionChange,
}: {
  host: HTMLDivElement | null;
  disabled: boolean;
  onPlaybackPositionChange: (seconds: number) => void;
}) => {
  const controls = usePlaylistControls();
  const { duration } = usePlaylistData();
  const {
    isPlaying,
    currentTime,
    visualTimeRef,
    registerFrameCallback,
    unregisterFrameCallback,
  } = usePlaybackAnimation();
  const rangeRef = useRef<HTMLInputElement | null>(null);
  const timerRef = useRef<HTMLSpanElement | null>(null);

  const updateDisplay = useCallback((seconds: number) => {
    const normalizedDuration = Number.isFinite(duration) && duration > 0 ? duration : 0;
    const normalizedTime = Math.max(0, Math.min(seconds, normalizedDuration));
    if (rangeRef.current) {
      rangeRef.current.max = String(normalizedDuration);
      rangeRef.current.value = String(normalizedTime);
    }
    if (timerRef.current) {
      timerRef.current.textContent = `${formatPlaybackTime(normalizedTime)} / ${formatPlaybackTime(normalizedDuration)}`;
    }
  }, [duration]);

  useEffect(() => {
    updateDisplay(visualTimeRef.current ?? currentTime);
  }, [currentTime, duration, updateDisplay, visualTimeRef]);

  useEffect(() => {
    if (!isPlaying) return;
    const callbackId = "sightsinger-score-master-seek";
    registerFrameCallback(callbackId, ({ visualTime }) => updateDisplay(visualTime));
    return () => unregisterFrameCallback(callbackId);
  }, [isPlaying, registerFrameCallback, unregisterFrameCallback, updateDisplay]);

  if (!host) return null;
  return createPortal(
    <div className="score-player-seek" aria-label="Master playback position">
      <input
        ref={rangeRef}
        className="score-player-seek-slider"
        type="range"
        min="0"
        max={Math.max(0, duration)}
        step="0.01"
        defaultValue="0"
        disabled={disabled || duration <= 0}
        onChange={(event) => {
          const seconds = Number(event.currentTarget.value);
          controls.seekTo(seconds);
          updateDisplay(seconds);
          onPlaybackPositionChange(seconds);
        }}
        aria-label="Seek playback position"
      />
      <span ref={timerRef} className="score-player-seek-time" aria-live="off">
        {formatPlaybackTime(0)} / {formatPlaybackTime(duration)}
      </span>
    </div>,
    host
  );
};

export const ScorePlayerMixerBridge = ({
  midiTrackCount,
  vocalTracks,
  instrumentalTracks = [],
  instrumentalMidiTrackKeys = [],
  instrumentalBus,
  engineGeneration,
}: {
  midiTrackCount: number;
  vocalTracks: MultiTrackAudioTrack[];
  instrumentalTracks?: InstrumentalTrackState[];
  instrumentalMidiTrackKeys?: readonly string[];
  instrumentalBus: InstrumentalBusState;
  engineGeneration: number;
}) => {
  const controls = usePlaylistControls();
  const { isReady, trackStates } = usePlaylistData();
  const controlsRef = useRef(controls);
  controlsRef.current = controls;
  // The provider gives every track it adds to the engine, and every track of a
  // rebuilt engine, the mute, solo and volume from its own track states, which
  // keep only the last of several mixer edits made in one render. So apply the
  // rows' state again once the engine holds those tracks: after a rebuild
  // (engineGeneration), and after tracks are added to the live engine (such as
  // the MIDI arriving after a take), which the provider follows by growing its
  // track states.
  const engineTrackCount = trackStates.length;
  const vocalMixerSignature = vocalTracks
    .map((track) => `${track.key}\u0000${track.muted}\u0000${track.solo}\u0000${track.volume}`)
    .join("\u0001");
  const instMixerSignature = instrumentalTracks
    .map((track) => `${track.key}\u0000${track.muted}\u0000${track.solo}\u0000${track.volume}`)
    .join("\u0001");
  const instrumentalBusSignature = `${instrumentalBus.muted}\u0000${instrumentalBus.solo}\u0000${instrumentalBus.levelDb}`;

  useEffect(() => {
    if (!isReady) return;
    const currentControls = controlsRef.current;

    const hasIndividualInstrumentSolo = instrumentalTracks.some((track) => track.solo);
    const busGain = dbToGain(instrumentalBus.levelDb);
    for (let i = 0; i < midiTrackCount; i++) {
      const track = instrumentalTrackForMidiTrack(i, instrumentalMidiTrackKeys, instrumentalTracks);
      if (track) {
        const effectiveSolo = hasIndividualInstrumentSolo ? track.solo : instrumentalBus.solo;
        currentControls.setTrackMute(i, instrumentalBus.muted || track.muted);
        currentControls.setTrackSolo(i, effectiveSolo);
        currentControls.setTrackVolume(i, track.volume * busGain);
      } else {
        currentControls.setTrackMute(i, false);
        currentControls.setTrackSolo(i, false);
        currentControls.setTrackVolume(i, 1);
      }
    }

    // Apply vocal tracks mixer state
    vocalTracks.forEach((track, index) => {
      const trackIndex = midiTrackCount + index;
      currentControls.setTrackMute(trackIndex, track.muted);
      currentControls.setTrackSolo(trackIndex, track.solo);
      currentControls.setTrackVolume(trackIndex, track.volume);
    });
  }, [
    isReady,
    midiTrackCount,
    vocalMixerSignature,
    instMixerSignature,
    instrumentalBusSignature,
    instrumentalTracks,
    instrumentalMidiTrackKeys,
    instrumentalBus,
    vocalTracks,
    engineGeneration,
    engineTrackCount,
  ]);

  return null;
};

export const withoutKeysOutside = <T,>(
  map: ReadonlyMap<string, T>,
  live: ReadonlySet<string>
): ReadonlyMap<string, T> => {
  if ([...map.keys()].every((key) => live.has(key))) return map;
  return new Map([...map].filter(([key]) => live.has(key)));
};

export const ScorePlayerEngine = ({
  midiUrl,
  vocalTracks,
  decodedVocals,
  vocalsDecoding,
  instrumentalTracks = [],
  instrumentalMidiTrackKeys = [],
  instrumentalBus,
  playbackRequestId,
  onControlsChange,
  onEngineLoading,
  onEngineReady,
  onPlaybackStateChange,
  onPlaybackPositionChange,
  onTimelineDurationChange,
  onAdapterChange,
  onError,
  seekBarHost,
  seekDisabled,
}: ScorePlayerEngineProps) => {
  // WaveformPlaylistProvider treats callback identity changes as an engine
  // reconfiguration. Keep its callbacks stable so parent state updates (such
  // as setting the Play button to its active state) cannot dispose the MIDI
  // adapter while a note sequence is playing.
  const callbacksRef = useRef({ playbackRequestId, onEngineReady, onError, onAdapterChange });
  callbacksRef.current = { playbackRequestId, onEngineReady, onError, onAdapterChange };
  const handleProviderReady = useCallback(() => {
    const callbacks = callbacksRef.current;
    callbacks.onEngineReady(callbacks.playbackRequestId);
  }, []);
  const handleProviderError = useCallback((error: Error) => {
    callbacksRef.current.onError(error.message);
  }, []);
  // The provider calls onReady after every engine rebuild (a new sound, a
  // replaced take), but not after an incremental add. Count the rebuilds, so
  // the mixer bridge applies the rows' state to each new engine.
  const [engineGeneration, setEngineGeneration] = useState(0);
  const handleEngineRebuilt = useCallback(() => {
    setEngineGeneration((generation) => generation + 1);
  }, []);
  const adapterRef = useRef<ToneAdapter | null>(null);
  const soundFontCacheRef = useRef<SoundFontCache | null>(null);
  const createScorePlayerAdapter = useCallback(() => {
    // Match the provider's default factory.  In particular, a provider rebuild
    // after another vocal is added must create MIDI tracks with the already
    // loaded SoundFont, rather than silently falling back to PolySynth.
    const adapter = createToneAdapter({ soundFontCache: soundFontCacheRef.current ?? undefined });
    adapterRef.current = adapter;
    callbacksRef.current.onAdapterChange(adapter);
    return adapter;
  }, []);
  useEffect(() => () => {
    adapterRef.current = null;
    callbacksRef.current.onAdapterChange(null);
  }, []);
  const [soundFontCache, setSoundFontCache] = useState<SoundFontCache | null>(null);
  soundFontCacheRef.current = soundFontCache;
  const [soundFontLoadState, setSoundFontLoadState] = useState<
    "idle" | "loading" | "ready" | "fallback"
  >("idle");
  const shouldLoadSoundFont = Boolean(midiUrl && instrumentalTracks.length > 0);
  const soundFontLoading =
    shouldLoadSoundFont &&
    soundFontLoadState !== "ready" &&
    soundFontLoadState !== "fallback";
  const midiConfigs = useMemo(
    () => (midiUrl ? [{ src: midiUrl, name: "Score instruments" }] : []),
    [midiUrl]
  );
  // Each take is downloaded and decoded once, when its job completes, into
  // decodedVocals (see MainApp). The player only ever receives decoded audio:
  // useAudioTracks never fetches, so a take never needs its playback token
  // again, and a take still decoding joins the player when its audio is ready.
  // The mixer bridge maps vocals to provider tracks by position, so it gets
  // exactly this list too.
  const playerVocalTracks = useMemo(
    () => vocalTracks.filter((track) => decodedVocals.has(track.sourceJobId)),
    [decodedVocals, vocalTracks]
  );
  // Mixer changes are forwarded through usePlaylistControls below. Keeping
  // them out of the source configuration prevents useAudioTracks from
  // rebuilding its tracks, and the provider its engine, on every Mute/Solo/
  // volume edit.
  const vocalSourceSignature = playerVocalTracks
    .map((track) =>
      [track.key, track.sourceJobId, track.label, track.durationSeconds ?? ""].join("\u0000")
    )
    .join("\u0001");
  const vocalSources = useMemo(
    () =>
      playerVocalTracks.map((track) => ({
        key: `${track.key}\u0000${track.sourceJobId}`,
        trackKey: track.key,
        sourceJobId: track.sourceJobId,
        audioBuffer: decodedVocals.get(track.sourceJobId) as AudioBuffer,
        name: track.label,
        duration: track.durationSeconds ?? undefined,
        muted: track.muted,
        soloed: track.solo,
        volume: track.volume,
      })),
    // vocalSourceSignature deliberately excludes mixer-only state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [vocalSourceSignature]
  );
  const audioConfigs = useMemo(
    () =>
      vocalSources.map(
        ({ key: _key, trackKey: _trackKey, sourceJobId: _sourceJobId, ...config }) =>
          config
      ),
    [vocalSources]
  );
  const { tracks: midiTracks, loading: midiLoading, error: midiError } = useMidiTracks(
    midiConfigs,
    { sampleRate: 48_000 }
  );
  const { tracks: audioTracks, loading: audioLoading, error: audioError } = useAudioTracks(
    audioConfigs
  );
  const instrumentalProgramSignature = instrumentalTracks
    .map((track) => `${track.key}\u0000${track.soundfontBank}\u0000${track.presetKind}\u0000${track.gmProgram}`)
    .join("\u0001");
  const configuredMidiTracks = useMemo(
    () =>
      midiTracks.map((track, index) => {
        const instrumentalTrack = instrumentalTrackForMidiTrack(
          index,
          instrumentalMidiTrackKeys,
          instrumentalTracks
        );
        if (!instrumentalTrack) return track;

        let changed = false;
        const clips = track.clips.map((clip) => {
          if (!clip.midiNotes || clip.midiNotes.length === 0) return clip;
          if (clip.midiProgram === instrumentalTrack.gmProgram) return clip;
          changed = true;
          return { ...clip, midiProgram: instrumentalTrack.gmProgram };
        });
        return changed ? { ...track, clips } : track;
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [midiTracks, instrumentalProgramSignature, instrumentalMidiTrackKeys]
  );
  // useAudioTracks rebuilds every track object whenever its configs change,
  // and until it has processed the new configs it still exposes its previous
  // result. Keep each take's first track object, so the provider sees an
  // unchanged take as unchanged and appends a new take incrementally.
  // A loader track belongs to a take only if it was built from that take's
  // decoded buffer. While the loader catches up after a take is replaced, the
  // track at that position still carries the previous take's buffer: it stays
  // in place until the new one arrives, but is never recorded as the new take.
  const stableAudioTracksRef = useRef(new Map<string, (typeof audioTracks)[number]>());
  const previousStableAudioTracksRef = useRef<typeof audioTracks | null>(null);
  // Depend only on decoded audio and memoized sources. Mixer-only edits must
  // not create a new tracks array: the provider would rebuild its engine.
  const stableAudioTracks = useMemo(() => {
    const next = new Map<string, (typeof audioTracks)[number]>();
    const normalized = audioTracks.map((track, index) => {
      const source = vocalSources[index];
      if (!source || track?.clips?.[0]?.audioBuffer !== source.audioBuffer) return track;
      const resolved = stableAudioTracksRef.current.get(source.key) ?? track;
      next.set(source.key, resolved);
      return resolved;
    });
    // A take the loader has not re-emitted yet keeps its recorded object.
    vocalSources.forEach((source) => {
      const recorded = stableAudioTracksRef.current.get(source.key);
      if (recorded && !next.has(source.key)) next.set(source.key, recorded);
    });
    stableAudioTracksRef.current = next;
    // Adding a take re-runs this memo before the new vocal is processed while
    // the tracks are still the same objects. The provider only appends
    // incrementally when the array grows; a new array of the same length makes
    // it rebuild its whole engine. Return the previous array when every track
    // is the same object in the same place. Any real change (a replaced,
    // removed or reordered take, or a newly processed one) produces a different
    // object or length and still yields a new array.
    const previous = previousStableAudioTracksRef.current;
    if (
      previous &&
      previous.length === normalized.length &&
      normalized.every((track, index) => track === previous[index])
    ) {
      return previous;
    }
    previousStableAudioTracksRef.current = normalized;
    return normalized;
  }, [audioTracks, vocalSources]);
  const tracks = useMemo(
    () => [...configuredMidiTracks, ...stableAudioTracks],
    [configuredMidiTracks, stableAudioTracks]
  );
  // The player is still catching up while the tracks it holds differ from the
  // ones it should hold: a decoded take the audio loader has not built a track
  // for yet, a removed take still present, or MIDI loaded for an older URL.
  // These are values of this render, so the bridge's ready check sees them in
  // the same render it runs in; no later effect can contradict its answer.
  const takesNotYetInPlayer =
    !audioError &&
    (stableAudioTracks.length !== vocalSources.length ||
      vocalSources.some(
        (source, index) => stableAudioTracks[index]?.clips?.[0]?.audioBuffer !== source.audioBuffer
      ));
  const [loadedMidiUrl, setLoadedMidiUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!midiLoading) setLoadedMidiUrl(midiUrl ?? null);
    // Recorded once the loader has settled on its tracks for the current URL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [midiLoading, midiTracks]);
  const midiNotYetInPlayer = !midiError && (midiUrl ?? null) !== loadedMidiUrl;
  const engineLoading =
    midiLoading ||
    audioLoading ||
    soundFontLoading ||
    vocalsDecoding ||
    takesNotYetInPlayer ||
    midiNotYetInPlayer;
  const hasMountedPlayerRef = useRef(false);

  useEffect(() => {
    if (!shouldLoadSoundFont) {
      setSoundFontLoadState("idle");
      return;
    }

    let cancelled = false;
    setSoundFontLoadState((current) => (current === "ready" ? current : "loading"));
    void loadFluidR3SoundFontCache()
      .then((cache) => {
        if (cancelled) return;
        setSoundFontCache(cache);
        setSoundFontLoadState("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn("[score-player] FluidR3_GM.sf2 unavailable; falling back to default MIDI synth.", err);
        setSoundFontCache(null);
        setSoundFontLoadState("fallback");
      });

    return () => {
      cancelled = true;
    };
  }, [shouldLoadSoundFont]);

  useEffect(() => {
    onError(midiError ?? audioError);
  }, [audioError, midiError, onError]);

  // `useAudioTracks` and `useMidiTracks` can append decoded sources after the
  // provider's initial ready event. Keep the outer transport (and bounce
  // action) disabled for the whole loader lifetime, including the gap before a
  // loader starts on a new take or MIDI URL. Otherwise a recorder can start
  // just as the provider disposes/rebuilds its adapter for an incremental track
  // add. This effect only ever reports loading; the bridge alone reports ready,
  // so the two cannot run in an order that leaves the player marked loading.
  useEffect(() => {
    if (engineLoading) onEngineLoading();
  }, [engineLoading, onEngineLoading]);

  useEffect(() => {
    if (tracks.length !== 0 || !adapterRef.current) return;
    adapterRef.current = null;
    callbacksRef.current.onAdapterChange(null);
  }, [tracks.length]);

  // The documented integration mounts after the first complete set of sources.
  // Later vocal sources remain attached to the existing provider: its supported
  // incremental-add path calls PlaylistEngine.addTrack() without disposing
  // MIDI/audio tracks that are already playing.
  if (tracks.length === 0) {
    hasMountedPlayerRef.current = false;
    return null;
  }
  if (!hasMountedPlayerRef.current && (midiLoading || audioLoading || vocalsDecoding)) {
    return null;
  }
  hasMountedPlayerRef.current = true;

  return (
    <WaveformPlaylistProvider
      tracks={tracks}
      soundFontCache={soundFontCache ?? undefined}
      createAdapter={createScorePlayerAdapter}
      onReady={handleEngineRebuilt}
      onError={handleProviderError}
    >
      <ScorePlayerEngineBridge
        onControlsChange={onControlsChange}
        loading={engineLoading}
        onReady={handleProviderReady}
        onPlaybackStateChange={onPlaybackStateChange}
        onPlaybackPositionChange={onPlaybackPositionChange}
        onTimelineDurationChange={onTimelineDurationChange}
      />
      <ScorePlayerMixerBridge
        midiTrackCount={midiTracks.length}
        vocalTracks={playerVocalTracks}
        instrumentalTracks={instrumentalTracks}
        instrumentalMidiTrackKeys={instrumentalMidiTrackKeys}
        instrumentalBus={instrumentalBus}
        engineGeneration={engineGeneration}
      />
      <ScorePlayerSeekBar
        host={seekBarHost}
        disabled={seekDisabled}
        onPlaybackPositionChange={onPlaybackPositionChange}
      />
    </WaveformPlaylistProvider>
  );
};
