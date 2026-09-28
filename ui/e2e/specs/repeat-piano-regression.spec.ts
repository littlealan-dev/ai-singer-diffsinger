import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { firebaseIdToken, signInAsE2EUser } from "../auth";
import { getE2EState } from "../support";

// Real local synthesis of the repeat/navigation scores with a piano part, in
// written order and with repeats. The planner is the deterministic regression
// LLM; no real LLM is called. Each take's vocal audio must last as long as the
// instrumental MIDI for the same repeat setting, and both as long as the score
// in that order: every note is a quarter note at the default 120 bpm.

const API_BASE = "http://127.0.0.1:8000";
const FIXTURES = path.resolve(import.meta.dirname, "..", "..", "..", "tests", "fixtures", "repeat_navigation_piano");
const QUARTER_SECONDS = 0.5;
// Played orders pinned by tests/test_repeat_navigation.py.
const CASES = [
  { file: "forward_repeat.musicxml", written: "CDEFG", played: "CDCDEFG" },
  { file: "volta_endings.musicxml", written: "CDEFG", played: "CDCEFG" },
  { file: "da_capo.musicxml", written: "CDEFG", played: "CDEFCDEFG" },
  { file: "da_capo_al_fine.musicxml", written: "CDEFG", played: "CDEFCD" },
  { file: "da_capo_al_coda.xml", written: "CDEFGA", played: "CDEFCDEGA" },
  { file: "dal_segno.musicxml", written: "CDEFG", played: "CDEFDEFG" },
  { file: "dal_segno_al_fine.musicxml", written: "CDEFG", played: "CDEFDE" },
  { file: "dal_segno_al_coda.xml", written: "CDEFGAB", played: "CDEFGEFAB" },
];
const MIDI_TOLERANCE_SECONDS = 0.01;
// Synthesis renders in frames of 512 samples at 44.1 kHz (about 11.6 ms), so
// the audio can differ from the score by rounding. Allow about two frames: far
// below a single quarter note (0.5 s), so a dropped or repeated note fails.
const AUDIO_TOLERANCE_SECONDS = 0.025;

test.describe.configure({ mode: "serial" });

async function api(page: Page, request: APIRequestContext, route: string, init: Parameters<APIRequestContext["fetch"]>[1] = {}) {
  const token = await firebaseIdToken(page);
  const response = await request.fetch(`${API_BASE}${route}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
  });
  expect(response.ok(), `${init.method ?? "GET"} ${route}: ${response.status()} ${await response.text()}`).toBeTruthy();
  return response;
}

/** Playback length of a Standard MIDI File: the last note-off, through the tempo map. */
function midiDurationSeconds(bytes: Buffer): number {
  const division = bytes.readUInt16BE(12);
  const trackCount = bytes.readUInt16BE(10);
  const tempos: Array<[number, number]> = [];
  let lastNoteOff = 0;
  let offset = 14;
  const readVariable = (position: number): [number, number] => {
    let value = 0;
    for (;;) {
      const byte = bytes[position++];
      value = (value << 7) | (byte & 0x7f);
      if (!(byte & 0x80)) return [value, position];
    }
  };
  for (let track = 0; track < trackCount; track++) {
    const length = bytes.readUInt32BE(offset + 4);
    let position = offset + 8;
    const end = position + length;
    offset = end;
    let tick = 0;
    let status = 0;
    while (position < end) {
      const [delta, next] = readVariable(position);
      position = next;
      tick += delta;
      if (bytes[position] === 0xff) {
        const type = bytes[position + 1];
        const [size, dataStart] = readVariable(position + 2);
        if (type === 0x51) tempos.push([tick, bytes.readUIntBE(dataStart, 3)]);
        position = dataStart + size;
      } else if (bytes[position] === 0xf0 || bytes[position] === 0xf7) {
        const [size, dataStart] = readVariable(position + 1);
        position = dataStart + size;
      } else {
        if (bytes[position] & 0x80) status = bytes[position++];
        const kind = status & 0xf0;
        const dataLength = kind === 0xc0 || kind === 0xd0 ? 1 : 2;
        const velocity = bytes[position + 1];
        if (kind === 0x80 || (kind === 0x90 && velocity === 0)) lastNoteOff = Math.max(lastNoteOff, tick);
        position += dataLength;
      }
    }
  }
  tempos.sort((a, b) => a[0] - b[0]);
  if (!tempos.length || tempos[0][0] > 0) tempos.unshift([0, 500_000]);
  let seconds = 0;
  tempos.forEach(([startTick, microsecondsPerQuarter], index) => {
    const nextTick = index + 1 < tempos.length ? tempos[index + 1][0] : lastNoteOff;
    if (startTick >= lastNoteOff) return;
    seconds += ((Math.min(nextTick, lastNoteOff) - startTick) / division) * (microsecondsPerQuarter / 1e6);
  });
  return seconds;
}

async function synthesize(page: Page, request: APIRequestContext, sessionId: string, expandRepeats: boolean) {
  const chat = async (message: string) =>
    (await api(page, request, `/sessions/${sessionId}/chat`, {
      method: "POST",
      data: { message, expand_repeats: expandRepeats },
    })).json();
  await chat("[e2e:repeat-regression] prepare this fixture");
  const confirmed = await chat("[e2e:confirm] Start the quoted synthesis.");
  expect(confirmed.type, JSON.stringify(confirmed)).toBe("chat_progress");
  const jobId = confirmed.job_id as string;
  await expect.poll(async () => {
    const state = await getE2EState(page, request, sessionId);
    return state.job?.id === jobId ? state.job?.status : "pending";
  }, { timeout: 540_000, intervals: [1_000] }).toMatch(/^(completed|failed)$/);
  const state = await getE2EState(page, request, sessionId);
  expect(state.job?.status, `synthesis failed: ${state.job?.error}`).toBe("completed");
  return state.synthesis?.duration_seconds ?? 0;
}

test("vocal audio and instrumental MIDI follow the repeat setting for every navigation form", async ({ page, request }) => {
  test.setTimeout(30 * 60_000);
  await signInAsE2EUser(page, "repeat-piano-regression");
  const rows: string[] = [];
  const failures: string[] = [];

  for (const testCase of CASES) {
    const { session_id: sessionId } = await (await api(page, request, "/sessions", { method: "POST" })).json();
    await api(page, request, `/sessions/${sessionId}/upload`, {
      method: "POST",
      multipart: {
        file: {
          // Uploads accept .xml or .mxl names; .musicxml is the same format.
          name: testCase.file.replace(/\.musicxml$/, ".xml"),
          mimeType: "application/xml",
          buffer: await readFile(path.join(FIXTURES, testCase.file)),
        },
      },
    });

    for (const expandRepeats of [false, true]) {
      const expectedSeconds =
        (expandRepeats ? testCase.played : testCase.written).length * QUARTER_SECONDS;
      const audioSeconds = await synthesize(page, request, sessionId, expandRepeats);
      const midi = await api(
        page,
        request,
        `/sessions/${sessionId}/instrumental-midi?expand_repeats=${expandRepeats}`,
      );
      const midiSeconds = midiDurationSeconds(Buffer.from(await midi.body()));
      const label = `${testCase.file} ${expandRepeats ? "with repeats" : "written order"}`;
      rows.push(
        `${label.padEnd(44)} expected=${expectedSeconds.toFixed(2)}s midi=${midiSeconds.toFixed(3)}s ` +
          `audio=${audioSeconds.toFixed(3)}s audio-midi=${(audioSeconds - midiSeconds).toFixed(3)}s`,
      );
      if (Math.abs(midiSeconds - expectedSeconds) > MIDI_TOLERANCE_SECONDS) {
        failures.push(`${label}: MIDI ${midiSeconds.toFixed(3)}s, expected ${expectedSeconds}s`);
      }
      if (Math.abs(audioSeconds - midiSeconds) > AUDIO_TOLERANCE_SECONDS) {
        failures.push(`${label}: audio ${audioSeconds.toFixed(3)}s vs MIDI ${midiSeconds.toFixed(3)}s`);
      }
    }
  }

  console.log(`REPEAT REGRESSION\n${rows.join("\n")}`);
  expect(failures).toEqual([]);
});
