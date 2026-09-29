// Focused memo-dependency tests: no browser, server, synthesis, or React DOM.
// Execute the actual vocal source pipeline of the score player, with a small
// memo/ref harness, so a regression in which take the player is given, or in
// when it hands the provider a new tracks array, fails here.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const app = readFileSync(new URL("../src/MainApp.tsx", import.meta.url), "utf8");
const start = app.indexOf("  const playerVocalTracks = useMemo(");
const end = app.indexOf("  const hasMountedPlayerRef =", start);
assert.ok(start >= 0 && end > start);
const pipeline = ts.transpileModule(app.slice(start, end), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const runPipeline = new Function(
  "vocalTracks", "decodedVocals", "instrumentalTracks", "midiConfigs", "useMemo", "useRef",
  "useAudioTracks", "useMidiTracks",
  `${pipeline}\nreturn { tracks, audioConfigs };`,
);

const midiTrack = { id: "piano", clips: [] };

function createRenderer() {
  const slots = [];
  // Like the real hook, the MIDI loader returns the same array until the MIDI changes.
  const midiTracks = [midiTrack];
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
  // vocals: the player's vocal tracks; decoded: job -> decoded buffer;
  // loaderOutput: what useAudioTracks currently returns, which can lag behind.
  return (vocals, decoded, loaderOutput, loading = false) => {
    cursor = 0;
    return runPipeline(vocals, decoded, [], [], useMemo, useRef,
      () => ({ tracks: loaderOutput, loading }),
      () => ({ tracks: midiTracks, loading: false }));
  };
}

const take = (jobId, overrides = {}) => ({
  key: "P1", sourceJobId: jobId, audioUrl: `/${jobId}.mp3`, label: "Soprano",
  durationSeconds: 8, muted: false, solo: false, volume: 1, ...overrides,
});
const buffer = (jobId) => ({ decodedFrom: jobId });
// A vocal as useAudioTracks builds it from a config: its clip holds that config's buffer.
const loaderTrack = (audioBuffer) => ({ id: "t", clips: [{ audioBuffer }] });

test("the player is only given decoded audio, never a URL to fetch", () => {
  const render = createRenderer();
  const a = buffer("A");
  const { audioConfigs } = render([take("A")], new Map([["A", a]]), [loaderTrack(a)]);
  assert.equal(audioConfigs.length, 1);
  assert.strictEqual(audioConfigs[0].audioBuffer, a);
  assert.ok(!("src" in audioConfigs[0]));
});

test("a take still decoding stays out of the player until its audio is ready", () => {
  const render = createRenderer();
  const pending = render([take("A")], new Map(), []);
  assert.deepEqual(pending.audioConfigs, []);
  assert.deepEqual(pending.tracks, [midiTrack]);
  const a = buffer("A");
  render([take("A")], new Map([["A", a]]), [], true);
  const ready = render([take("A")], new Map([["A", a]]), [loaderTrack(a)]);
  assert.equal(ready.tracks.length, 2);
  assert.strictEqual(ready.tracks[1].clips[0].audioBuffer, a);
});

test("vocal mixer edits preserve source configs and provider tracks identity", () => {
  const render = createRenderer();
  const a = buffer("A");
  const decoded = new Map([["A", a]]);
  const output = [loaderTrack(a)];
  const initial = render([take("A")], decoded, output);
  for (const adjustment of [{ muted: true }, { solo: true }, { volume: 0.25 }, {}]) {
    const next = render([take("A", adjustment)], decoded, output);
    assert.strictEqual(next.audioConfigs, initial.audioConfigs);
    assert.strictEqual(next.tracks, initial.tracks);
  }
});

test("a re-signed URL for a decoded take changes nothing", () => {
  const render = createRenderer();
  const a = buffer("A");
  const decoded = new Map([["A", a]]);
  const output = [loaderTrack(a)];
  const ready = render([take("A")], decoded, output);
  const resigned = render([take("A", { audioUrl: "/A.mp3?playback_token=new" })], decoded, output);
  assert.strictEqual(resigned.audioConfigs, ready.audioConfigs);
  assert.strictEqual(resigned.tracks, ready.tracks);
});

test("a replaced take plays its own audio, never the previous take's", () => {
  const render = createRenderer();
  const a = buffer("A");
  const b = buffer("B");
  const trackA = loaderTrack(a);
  const ready = render([take("A")], new Map([["A", a]]), [trackA]);
  // B replaces A for the same part and is decoded, but the loader still
  // exposes A's track while it processes the new configs.
  const decodedB = new Map([["B", b]]);
  const catchingUp = render([take("B")], decodedB, [trackA], true);
  assert.strictEqual(catchingUp.audioConfigs[0].audioBuffer, b);
  // Unchanged until the loader emits B: the provider keeps playing A.
  assert.strictEqual(catchingUp.tracks, ready.tracks);
  // Another rebuild while catching up must still configure B from B's audio.
  const relabeled = render([take("B", { label: "Voice (new)" })], decodedB, [trackA], true);
  assert.strictEqual(relabeled.audioConfigs[0].audioBuffer, b);
  const trackB = loaderTrack(b);
  const replaced = render([take("B", { label: "Voice (new)" })], decodedB, [trackB]);
  assert.notStrictEqual(replaced.tracks, ready.tracks);
  assert.strictEqual(replaced.tracks[1], trackB);
});

test("adding a take keeps the existing take and appends the new one", () => {
  const render = createRenderer();
  const a = buffer("A");
  const c = buffer("C");
  const trackA = loaderTrack(a);
  render([take("A")], new Map([["A", a]]), [trackA]);
  const ready = render([take("A")], new Map([["A", a]]), [trackA]);
  const alto = take("C", { key: "P2", label: "Alto" });
  // C is still decoding: nothing changes for the provider.
  const decoding = render([take("A"), alto], new Map([["A", a]]), [trackA]);
  assert.strictEqual(decoding.tracks, ready.tracks);
  // C decoded; the loader has not processed it yet.
  const bothDecoded = new Map([["A", a], ["C", c]]);
  const loading = render([take("A"), alto], bothDecoded, [trackA], true);
  assert.strictEqual(loading.tracks, ready.tracks);
  assert.strictEqual(loading.audioConfigs[0].audioBuffer, a);
  assert.strictEqual(loading.audioConfigs[1].audioBuffer, c);
  // The loader rebuilds every track object; the existing take keeps its object,
  // so the provider sees a pure append.
  const appended = render([take("A"), alto], bothDecoded, [loaderTrack(a), loaderTrack(c)]);
  assert.equal(appended.tracks.length, ready.tracks.length + 1);
  ready.tracks.forEach((track, index) => assert.strictEqual(appended.tracks[index], track));
  assert.strictEqual(appended.tracks[2].clips[0].audioBuffer, c);
});

test("removing a take yields a new, shorter tracks array", () => {
  const render = createRenderer();
  const a = buffer("A");
  const c = buffer("C");
  const alto = take("C", { key: "P2", label: "Alto" });
  const both = new Map([["A", a], ["C", c]]);
  const ready = render([take("A"), alto], both, [loaderTrack(a), loaderTrack(c)]);
  const removed = render([take("A")], new Map([["A", a]]), [loaderTrack(a)]);
  assert.notStrictEqual(removed.tracks, ready.tracks);
  assert.equal(removed.tracks.length, ready.tracks.length - 1);
});

test("clearing every take leaves only the instrumental tracks", () => {
  const render = createRenderer();
  const a = buffer("A");
  render([take("A")], new Map([["A", a]]), [loaderTrack(a)]);
  const cleared = render([], new Map(), []);
  assert.deepEqual(cleared.audioConfigs, []);
  assert.deepEqual(cleared.tracks, [midiTrack]);
});
