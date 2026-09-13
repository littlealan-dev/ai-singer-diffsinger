// Focused memo-dependency tests: no browser, server, synthesis, or React DOM.
// Execute the actual source pipeline, with a small memo/ref harness, so a
// mixer-state dependency accidentally added to it regresses these checks.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const app = readFileSync(new URL("../src/MainApp.tsx", import.meta.url), "utf8");
const start = app.indexOf("  const vocalSourceSignature =");
const end = app.indexOf("  const hasMountedPlayerRef =", start);
assert.ok(start >= 0 && end > start);
const pipeline = ts.transpileModule(app.slice(start, end), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const runPipeline = new Function(
  "vocalTracks", "instrumentalTracks", "midiConfigs", "useMemo", "useRef",
  "useAudioTracks", "useMidiTracks",
  `${pipeline}\nreturn { tracks, audioConfigs };`,
);

function createRenderer() {
  const slots = [];
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
  return (vocals, decoded, loading = false) => {
    cursor = 0;
    return runPipeline(vocals, [], [], useMemo, useRef,
      () => ({ tracks: decoded, loading }),
      () => ({ tracks: midiTracks, loading: false }));
  };
}

const vocal = {
  key: "P1", audioUrl: "/solfege.mp3", label: "Soprano", durationSeconds: 8,
  muted: false, solo: false, volume: 1,
};

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
  const replacement = { ...vocal, audioUrl: "/lyrics.mp3", label: "V1" };
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
  const second = { ...vocal, key: "P2", audioUrl: "/alto.mp3", label: "Alto" };
  render([vocal, second], firstDecoded);
  render([vocal, second], firstDecoded, true);
  const reloaded = [{ id: "first-redecoded", clips: [] }, { id: "second", clips: [] }];
  const added = render([vocal, second], reloaded);
  assert.equal(added.tracks.length, 3);
  assert.strictEqual(added.tracks[1], ready.tracks[1]);
  assert.strictEqual(added.tracks[2], reloaded[1]);
});
