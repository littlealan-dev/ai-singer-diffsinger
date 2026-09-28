// Focused memo-dependency tests: no browser, server, synthesis, or React DOM.
// Execute the actual source pipeline, with a small memo/ref harness, so a
// mixer-state dependency accidentally added to it regresses these checks.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const app = readFileSync(new URL("../src/MainApp.tsx", import.meta.url), "utf8");
const start = app.indexOf("  const vocalSignatureUrlsRef =");
const end = app.indexOf("  const hasMountedPlayerRef =", start);
assert.ok(start >= 0 && end > start);
const pipeline = ts.transpileModule(app.slice(start, end), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const runPipeline = new Function(
  "vocalTracks", "instrumentalTracks", "midiConfigs", "useMemo", "useRef", "useEffect",
  "useAudioTracks", "useMidiTracks", "vocalBufferCache",
  `${pipeline}\nreturn { tracks, audioConfigs };`,
);

function createRenderer() {
  const slots = [];
  const vocalBufferCache = new Map();
  const midiTracks = [{ id: "piano", clips: [] }];
  let cursor = 0;
  const useMemo = (compute, deps) => {
    const index = cursor++;
    const cached = slots[index];
    if (!cached || deps.some((dep, i) => !Object.is(dep, cached.deps[i]))) {
      slots[index] = { deps, value: compute() };
    }
    return slots[index].value;
  };
  const useRef = (initial) => {
    const index = cursor++;
    return slots[index] ?? (slots[index] = { current: initial });
  };
  // Effects run after render in React; running them in place is equivalent
  // here because this render has already read everything they write.
  const useEffect = (effect, deps) => {
    const index = cursor++;
    const cached = slots[index];
    if (!cached || deps.some((dep, i) => !Object.is(dep, cached.deps[i]))) {
      slots[index] = { deps };
      effect();
    }
  };
  return (vocals, decoded, loading = false) => {
    cursor = 0;
    return runPipeline(vocals, [], [], useMemo, useRef, useEffect,
      () => ({ tracks: decoded, loading }),
      () => ({ tracks: midiTracks, loading: false }),
      vocalBufferCache);
  };
}

const vocal = {
  key: "P1", sourceJobId: "job-1", audioUrl: "/solfege.mp3", label: "Soprano", durationSeconds: 8,
  muted: false, solo: false, volume: 1,
};
// A decoded vocal as useAudioTracks returns it: its first clip carries the buffer.
const decodedTrack = (id) => ({ id, clips: [{ audioBuffer: { decodedFrom: id } }] });

test("vocal mixer edits preserve source configs and provider tracks identity", () => {
  const render = createRenderer();
  const decoded = [{ id: "solfege", clips: [] }];
  const initial = render([vocal], decoded);
  for (const adjustment of [{ muted: true }, { solo: true }, { volume: 0.25 }, {}]) {
    const next = render([{ ...vocal, ...adjustment }], decoded);
    assert.strictEqual(next.audioConfigs, initial.audioConfigs);
    assert.strictEqual(next.tracks, initial.tracks);
    assert.ok(!("trackKey" in next.audioConfigs[0]));
  }
});

test("a replacement vocal uses freshly decoded audio, not the previous take", () => {
  const render = createRenderer();
  const oldDecoded = [{ id: "solfege", clips: [] }];
  const initial = render([vocal], oldDecoded);
  const replacement = { ...vocal, sourceJobId: "job-2", audioUrl: "/lyrics.mp3", label: "V1" };
  const changed = render([replacement], oldDecoded);
  assert.notStrictEqual(changed.audioConfigs, initial.audioConfigs);
  assert.equal(changed.audioConfigs[0].src, "/lyrics.mp3");
  render([replacement], oldDecoded, true);
  const newDecoded = [{ id: "lyrics", clips: [] }];
  const ready = render([replacement], newDecoded);
  assert.strictEqual(ready.tracks[1], newDecoded[0]);
  const muted = render([{ ...replacement, muted: true }], newDecoded);
  assert.strictEqual(muted.tracks, ready.tracks);
});

test("adding a second vocal preserves the existing decoded track", () => {
  const render = createRenderer();
  const firstDecoded = [{ id: "first", clips: [] }];
  render([vocal], firstDecoded, true);
  const ready = render([vocal], firstDecoded);
  const second = { ...vocal, key: "P2", sourceJobId: "job-3", audioUrl: "/alto.mp3", label: "Alto" };
  render([vocal, second], firstDecoded);
  render([vocal, second], firstDecoded, true);
  const reloaded = [{ id: "first-redecoded", clips: [] }, { id: "second", clips: [] }];
  const added = render([vocal, second], reloaded);
  assert.equal(added.tracks.length, 3);
  assert.strictEqual(added.tracks[1], ready.tracks[1]);
  assert.strictEqual(added.tracks[2], reloaded[1]);
});

test("a decode filling the buffer cache does not rebuild configs or tracks", () => {
  const render = createRenderer();
  render([vocal], [], true);
  const decoded = [decodedTrack("first")];
  const ready = render([vocal], decoded);
  assert.equal(ready.audioConfigs[0].src, "/solfege.mp3");
  // The cache effect has now stored the buffer; the next render must not treat
  // that as a source change, or the provider would rebuild its engine.
  const after = render([vocal], decoded);
  assert.strictEqual(after.audioConfigs, ready.audioConfigs);
  assert.strictEqual(after.tracks, ready.tracks);
});

test("a re-signed URL for a cached vocal does not rebuild configs or tracks", () => {
  const render = createRenderer();
  const decoded = [decodedTrack("first")];
  render([vocal], decoded);
  const ready = render([vocal], decoded);
  const resigned = render([{ ...vocal, audioUrl: "/solfege.mp3?playback_token=new" }], decoded);
  assert.strictEqual(resigned.audioConfigs, ready.audioConfigs);
  assert.strictEqual(resigned.tracks, ready.tracks);
});

test("a new URL for a vocal that is not cached reloads it", () => {
  const render = createRenderer();
  const initial = render([vocal], [], true);
  const refreshed = render([{ ...vocal, audioUrl: "/solfege.mp3?playback_token=new" }], [], true);
  assert.notStrictEqual(refreshed.audioConfigs, initial.audioConfigs);
  assert.equal(refreshed.audioConfigs[0].src, "/solfege.mp3?playback_token=new");
});

test("a later rebuild configures an already decoded vocal from its buffer", () => {
  const render = createRenderer();
  const decoded = [decodedTrack("first")];
  render([vocal], decoded);
  render([vocal], decoded);
  const second = { ...vocal, key: "P2", sourceJobId: "job-3", audioUrl: "/alto.mp3", label: "Alto" };
  const added = render([vocal, second], decoded, true);
  // The first take's token may have expired by now: it must not be fetched.
  assert.deepEqual(added.audioConfigs[0].audioBuffer, { decodedFrom: "first" });
  assert.ok(!("src" in added.audioConfigs[0]));
  assert.equal(added.audioConfigs[1].src, "/alto.mp3");
});

test("adding a take keeps the tracks array until the new vocal decodes", () => {
  const render = createRenderer();
  const firstDecoded = [decodedTrack("first")];
  render([vocal], firstDecoded);
  const ready = render([vocal], firstDecoded);
  const second = { ...vocal, key: "P2", sourceJobId: "job-3", audioUrl: "/alto.mp3", label: "Alto" };
  // The configs change, then loading starts; the loader still reports the old
  // decoded list. A new array here would make the provider rebuild its engine.
  const configsChanged = render([vocal, second], firstDecoded);
  assert.strictEqual(configsChanged.tracks, ready.tracks);
  const loading = render([vocal, second], firstDecoded, true);
  assert.strictEqual(loading.tracks, ready.tracks);
  // Once decoded, the loader returns new objects for every vocal; the existing
  // take keeps its object, so the provider sees a pure append.
  const decoded = render([vocal, second], [decodedTrack("first-again"), decodedTrack("second")]);
  assert.notStrictEqual(decoded.tracks, ready.tracks);
  assert.equal(decoded.tracks.length, ready.tracks.length + 1);
  ready.tracks.forEach((track, index) => assert.strictEqual(decoded.tracks[index], track));
});

test("replacing a take in place still yields a new tracks array", () => {
  const render = createRenderer();
  const oldDecoded = [decodedTrack("old")];
  render([vocal], oldDecoded);
  const ready = render([vocal], oldDecoded);
  const replacement = { ...vocal, sourceJobId: "job-2", audioUrl: "/lyrics.mp3" };
  render([replacement], oldDecoded);
  render([replacement], oldDecoded, true);
  const newDecoded = [decodedTrack("new")];
  const replaced = render([replacement], newDecoded);
  assert.equal(replaced.tracks.length, ready.tracks.length);
  assert.notStrictEqual(replaced.tracks, ready.tracks);
  assert.strictEqual(replaced.tracks[1], newDecoded[0]);
});

test("removing a take yields a new tracks array", () => {
  const render = createRenderer();
  const second = { ...vocal, key: "P2", sourceJobId: "job-3", audioUrl: "/alto.mp3", label: "Alto" };
  const both = [decodedTrack("first"), decodedTrack("second")];
  render([vocal, second], both);
  const ready = render([vocal, second], both);
  const removed = render([vocal], [both[0]]);
  assert.notStrictEqual(removed.tracks, ready.tracks);
  assert.equal(removed.tracks.length, ready.tracks.length - 1);
});
